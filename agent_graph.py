import os
import re
import json
import sqlite3
import concurrent.futures
from typing import Annotated, TypedDict, Sequence, Literal
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage, HumanMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.sqlite import SqliteSaver
from dotenv import load_dotenv

from tools_library import get_all_tools
from short_term import run_working_memory
from long_term import retrieve_memories, process_user_message
from config import BASE_DIR, CHECKPOINT_DB, SOUL_FILE, log

load_dotenv(override=True)

# ── Model ─────────────────────────────────────────────────────────────────────
API_KEY = os.environ.get("GITHUB_TOKEN")
if not API_KEY:
    raise ValueError("GITHUB_TOKEN not found in .env")

llm = ChatOpenAI(
    base_url="https://models.inference.ai.azure.com",
    api_key=API_KEY,
    model="gpt-4o",
    temperature=0.1
)

# ── State ─────────────────────────────────────────────────────────────────────
class AgentState(TypedDict):
    messages:  Annotated[Sequence[BaseMessage], add_messages]
    route:     str

class RouteDecision(BaseModel):
    destination: Literal["direct", "agent"] = Field(
        description=(
            "'direct' for simple greetings or general knowledge. "
            "'agent' for anything needing tools, files, APIs, code, or planning."
        )
    )

# ── Helper ────────────────────────────────────────────────────────────────────
def _read(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""

def _build_base_prompt(query: str = "") -> str:
    """Assembles system prompt: SOUL + relevant ChromaDB memories + skills list."""
    soul      = _read(SOUL_FILE)
    memories  = retrieve_memories(query, k=5) if query else ""

    if not soul:
        soul = (
            "You are OpenClaw, an autonomous AI agent.\n"
            "Use tools to solve tasks. Never hallucinate results.\n"
            "Check skills/ before writing new code. Save working code as a skill.\n"
            "Be concise — show results, not process narration.\n"
        )

    parts = [soul]
    if memories:
        parts.append(memories)

    skills = sorted(f.name for f in (BASE_DIR / "skills").glob("*.py"))
    if skills:
        parts.append(
            "## Available Skills (check before writing new code)\n"
            + "\n".join(f"- {s}" for s in skills)
        )

    return "\n\n".join(parts)

# ── Nodes ─────────────────────────────────────────────────────────────────────

def router_node(state: AgentState) -> dict:
    router_llm = llm.with_structured_output(RouteDecision)
    last = state["messages"][-1].content
    decision = router_llm.invoke(
        f"Classify as 'direct' or 'agent':\n\n{last}"
    )
    return {"route": decision.destination}


def working_memory_node(state: AgentState) -> dict:
    """
    Short-term memory: summarize old messages, keep recent window.
    Runs before every cognitive call.
    """
    return run_working_memory(state, llm)


def direct_node(state: AgentState) -> dict:
    """Simple queries — inject SOUL + USER profile + memories."""
    last_human = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), ""
    )
    system = _build_base_prompt(query=last_human)
    filtered = [m for m in state["messages"] if not isinstance(m, SystemMessage)]
    response = llm.invoke([SystemMessage(content=system)] + filtered)
    log("DIRECT ANSWER", response.content[:300])
    return {"messages": [response]}


def cognitive_node(state: AgentState) -> dict:
    """
    Core agent brain:
    - Builds full context (SOUL + USER + MEMORY + relevant memories + skills list)
    - Hot-loads all tools
    - Calls LLM
    """
    last_human = next(
        (m.content for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), ""
    )
    system = _build_base_prompt(query=last_human)

    active_tools = get_all_tools()
    bound_llm = llm.bind_tools(active_tools)

    filtered = [m for m in state["messages"] if not isinstance(m, SystemMessage)]
    response = bound_llm.invoke([SystemMessage(content=system)] + filtered)
    log("AGENT RESPONSE", response.content[:300] if response.content else "[tool call]")
    return {"messages": [response]}


def parallel_tool_node(state: AgentState) -> dict:
    """Execute tool calls in parallel using ThreadPoolExecutor."""
    last_msg = state["messages"][-1]
    tools_map = {t.name: t for t in get_all_tools()}

    def run_one(tc):
        try:
            out = tools_map[tc["name"]].invoke(tc["args"]) if tc["name"] in tools_map \
                  else f"Tool '{tc['name']}' not found."
        except Exception as e:
            out = f"Tool Error: {e}"
        return ToolMessage(content=str(out), tool_call_id=tc["id"])

    with concurrent.futures.ThreadPoolExecutor() as pool:
        results = list(pool.map(run_one, last_msg.tool_calls))

    return {"messages": results}


def skill_saver_node(state: AgentState) -> dict:
    """
    After agent finishes:
    - Auto-saves new Python code to skills/ if not already saved
    """
    msgs = state["messages"]
    code_executed = None
    skill_saved = False

    for msg in msgs:
        if hasattr(msg, "tool_calls"):
            for tc in msg.tool_calls:
                if tc["name"] == "execute_python_in_sandbox":
                    code_executed = tc["args"].get("code", "")
                if tc["name"] == "write_file":
                    p = tc["args"].get("path", "")
                    if "skills/" in p or "skills\\" in p:
                        skill_saved = True

    if code_executed and not skill_saved:
        try:
            resp = llm.invoke([
                SystemMessage(content=(
                    "Given a Python script, return ONLY a JSON object:\n"
                    '{"filename": "descriptive_name.py", "code": "cleaned script"}\n'
                    "Pure JSON only, no markdown."
                )),
                HumanMessage(content=f"Script:\n{code_executed}")
            ])
            raw = re.sub(r"^```json|^```|```$", "", resp.content.strip(), flags=re.MULTILINE).strip()
            data = json.loads(raw)
            fname = data.get("filename", "saved_skill.py")
            code  = data.get("code", code_executed)
            (BASE_DIR / "skills" / fname).write_text(code, encoding="utf-8")
            print(f"\n💾 [SkillSaver] Saved: skills/{fname}")
        except Exception as e:
            print(f"\n⚠️  [SkillSaver] Failed: {e}")

    return state

# ── Routing functions ─────────────────────────────────────────────────────────

def route_after_router(state: AgentState) -> str:
    return "direct" if state["route"] == "direct" else "working_memory"

def route_after_cognitive(state: AgentState) -> str:
    last = state["messages"][-1]
    if hasattr(last, "tool_calls") and last.tool_calls:
        for tc in last.tool_calls:
            if tc["name"] == "execute_system_command":
                return "tools_hitl"
        return "tools"
    return "skill_saver"

# ── Graph ─────────────────────────────────────────────────────────────────────
workflow = StateGraph(AgentState)

workflow.add_node("router",         router_node)
workflow.add_node("direct",         direct_node)
workflow.add_node("working_memory", working_memory_node)
workflow.add_node("cognitive",      cognitive_node)
workflow.add_node("tools",          parallel_tool_node)
workflow.add_node("tools_hitl",     parallel_tool_node)
workflow.add_node("skill_saver",    skill_saver_node)

workflow.add_edge(START, "router")
workflow.add_conditional_edges("router", route_after_router,
    {"direct": "direct", "working_memory": "working_memory"})
workflow.add_edge("direct",         END)
workflow.add_edge("working_memory", "cognitive")
workflow.add_conditional_edges("cognitive", route_after_cognitive,
    {"tools": "tools", "tools_hitl": "tools_hitl", "skill_saver": "skill_saver"})
workflow.add_edge("tools",          "cognitive")
workflow.add_edge("tools_hitl",     "cognitive")
workflow.add_edge("skill_saver",    END)

db_conn     = sqlite3.connect(CHECKPOINT_DB, check_same_thread=False)
checkpointer = SqliteSaver(db_conn)

app = workflow.compile(
    checkpointer=checkpointer,
    interrupt_before=["tools_hitl"]
)