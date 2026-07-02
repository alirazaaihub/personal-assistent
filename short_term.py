"""
Short-Term (Working) Memory — Fixed
-------------------------------------
Token budget check → old messages summarize → recent window rakho.

Protection rules:
1. ToolMessage hamesha protected
2. AIMessage with tool_calls protected (agar uska ToolMessage baad mein hai)
3. Sirf complete, "safe" older messages summarize hote hain
"""

import tiktoken
from langchain_core.messages import (
    SystemMessage, RemoveMessage,
    HumanMessage, AIMessage, ToolMessage, BaseMessage
)

MAX_CONTEXT_TOKENS   = 6000
SUMMARIZE_THRESHOLD  = 0.80
MAX_SINGLE_MSG_CHARS = 15000


def count_tokens(text: str) -> int:
    try:
        enc = tiktoken.encoding_for_model("gpt-4o")
        return len(enc.encode(text))
    except Exception:
        return len(text.split()) * 4 // 3


def msg_to_text(msg: BaseMessage) -> str:
    if isinstance(msg.content, str):
        return msg.content
    return str(msg.content)


def _get_answered_tool_call_ids(msgs: list) -> set:
    """
    Returns tool_call_ids jo ToolMessage se cover ho chuke hain.
    Yeh IDs wale AIMessage safe hain delete karne ke liye.
    """
    answered = set()
    for msg in msgs:
        if isinstance(msg, ToolMessage) and msg.tool_call_id:
            answered.add(msg.tool_call_id)
    return answered


def _is_safe_to_remove(msg: BaseMessage, answered_ids: set) -> bool:
    """
    Check karo kya yeh message safely remove ho sakta hai.
    AIMessage with unanswered tool_calls → NEVER remove.
    """
    if isinstance(msg, ToolMessage):
        return False  # ToolMessage khud protected hai

    if isinstance(msg, AIMessage):
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            # Sirf tab remove karo jab sab tool_calls answer ho chuke hon
            for tc in msg.tool_calls:
                if tc.get("id") not in answered_ids:
                    return False  # Unanswered tool_call — protect karo
    return True


def run_working_memory(state: dict, llm) -> dict:
    """
    Sync working memory node.
    Returns RemoveMessage ops + summary + kept messages, or {} if not needed.
    """
    msgs = list(state["messages"])
    if not msgs:
        return {}

    total_tokens = sum(count_tokens(msg_to_text(m)) for m in msgs)
    threshold    = int(MAX_CONTEXT_TOKENS * SUMMARIZE_THRESHOLD)

    if total_tokens <= threshold:
        return {}

    answered_ids = _get_answered_tool_call_ids(msgs)

    # Step 1: Truncate oversized messages safely (immutable copy)
    safe_msgs = []
    for msg in msgs:
        text = msg_to_text(msg)
        if len(text) > MAX_SINGLE_MSG_CHARS:
            try:
                msg = msg.__class__(
                    content=text[:MAX_SINGLE_MSG_CHARS] + "\n...[truncated]",
                    id=getattr(msg, "id", None)
                )
            except Exception:
                pass  # Agar copy fail ho, original rakhte hain
        safe_msgs.append(msg)

    # Step 2: Bucket messages
    budget_used  = 0
    recent_msgs  = []
    older_msgs   = []
    protected    = []  # ToolMessages + unanswered tool_calls

    for msg in reversed(safe_msgs):
        # Protected messages — never touch
        if isinstance(msg, ToolMessage):
            protected.insert(0, msg)
            continue

        if isinstance(msg, AIMessage) and hasattr(msg, "tool_calls") and msg.tool_calls:
            all_answered = all(
                tc.get("id") in answered_ids for tc in msg.tool_calls
            )
            if not all_answered:
                protected.insert(0, msg)
                continue

        tokens = count_tokens(msg_to_text(msg))
        if budget_used + tokens <= MAX_CONTEXT_TOKENS:
            recent_msgs.insert(0, msg)
            budget_used += tokens
        else:
            if _is_safe_to_remove(msg, answered_ids):
                older_msgs.insert(0, msg)
            else:
                recent_msgs.insert(0, msg)  # Protect karo — budget se bahar bhi

    if not older_msgs:
        return {}

    # Step 3: Summarize older safe messages
    try:
        summary_resp = llm.invoke(
            [SystemMessage(content=(
                "Summarize this conversation in 3-4 lines. "
                "Keep: key decisions, important facts, user goals. "
                "Skip: greetings, filler, repeated content."
            ))] + older_msgs
        )
        summary_content = summary_resp.content
    except Exception:
        summary_content = "Previous conversation summarized."

    summary_msg = SystemMessage(
        content=f"[Conversation Summary]\n{summary_content}"
    )

    # Step 4: Delete ops — only safe older messages
    delete_ops = [
        RemoveMessage(id=m.id)
        for m in older_msgs
        if hasattr(m, "id") and m.id
    ]

    # Final order: summary → recent chat → protected tool pairs
    return {
        "messages": delete_ops + [summary_msg] + recent_msgs + protected
    }