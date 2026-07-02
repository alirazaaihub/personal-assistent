import os
import uuid
import time
import threading
import subprocess
import schedule
from datetime import datetime
from langchain_core.messages import HumanMessage, ToolMessage

from agent_graph import app, llm
from memory.long_term import process_user_message
from tools_library import _sandbox
from config import BASE_DIR, SCHEDULE_FILE

# ── Helper ────────────────────────────────────────────────────────────────────
def extract_text(msg) -> str:
    content = msg.content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return (content or "").strip()

def cleanup():
    if _sandbox is not None:
        try:
            _sandbox.kill()
            print("🔴 Sandbox closed.")
        except Exception:
            pass

# ── Scheduler ─────────────────────────────────────────────────────────────────
_sched_mtime = 0.0

ACTIONS = {
    "write_file":  lambda a1, a2: (BASE_DIR / a1).write_text(a2, encoding="utf-8"),
    "append_file": lambda a1, a2: open(BASE_DIR / a1, "a").write("\n" + a2),
    "run_script":  lambda a1, a2: subprocess.run(
        f'python "{BASE_DIR / a1}" {a2}', shell=True, cwd=str(BASE_DIR)),
    "notify":      lambda a1, _: subprocess.Popen([
        "powershell", "-Command",
        f'Add-Type -AssemblyName System.Windows.Forms;'
        f'$n=New-Object System.Windows.Forms.NotifyIcon;'
        f'$n.Icon=[System.Drawing.SystemIcons]::Information;'
        f'$n.Visible=$true;'
        f'$n.ShowBalloonTip(5000,"OpenClaw","{a1}",'
        f'[System.Windows.Forms.ToolTipIcon]::Info)'
    ], creationflags=subprocess.CREATE_NO_WINDOW),
}

def _load_schedule():
    global _sched_mtime
    schedule.clear("user")
    if not SCHEDULE_FILE.exists():
        return
    count = 0
    for line in SCHEDULE_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "|" not in line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) < 3:
            continue
        time_str, action = parts[0], parts[1].lower()
        arg1 = parts[2] if len(parts) > 2 else ""
        arg2 = parts[3] if len(parts) > 3 else ""
        try:
            fmt = "%I:%M %p" if ("AM" in time_str.upper() or "PM" in time_str.upper()) else "%H:%M"
            t24 = datetime.strptime(time_str.strip(), fmt).strftime("%H:%M")
        except ValueError:
            continue
        if action not in ACTIONS:
            continue
        fn = ACTIONS[action]
        schedule.every().day.at(t24).do(fn, arg1, arg2).tag("user")
        count += 1
    _sched_mtime = SCHEDULE_FILE.stat().st_mtime if SCHEDULE_FILE.exists() else 0.0
    if count:
        print(f"📅 [Scheduler] {count} task(s) loaded.")

def _run_heartbeat():
    """30-min workspace triage via HEARTBEAT.md."""
    if not HEARTBEAT_FILE.exists():
        return
    hb = HEARTBEAT_FILE.read_text(encoding="utf-8").strip()
    if not hb:
        return
    cron_config = {"configurable": {"thread_id": "openclaw_heartbeat"}}
    try:
        result = app.invoke(
            {"messages": [HumanMessage(content=f"CRON PULSE:\n{hb}")], "route": ""},
            cron_config
        )
        response = extract_text(result["messages"][-1])
        if "HEARTBEAT_OK" not in response:
            log("CRON ALERT", response)
            print(f"\n⚠️  [Heartbeat] Issue:\n{response}\n")
    except Exception as e:
        log("CRON ERROR", str(e))


def _cron_loop():
    _load_schedule()
    schedule.every(30).minutes.do(_run_heartbeat)
    while True:
        try:
            if SCHEDULE_FILE.exists():
                mtime = SCHEDULE_FILE.stat().st_mtime
                if mtime != _sched_mtime:
                    print("\n🔄 [Scheduler] SCHEDULE.md changed — reloading...")
                    _load_schedule()
        except Exception:
            pass
        schedule.run_pending()
        time.sleep(1)

# ── HITL loop ─────────────────────────────────────────────────────────────────
ICONS = {
    "execute_python_in_sandbox": "🐍", "download_from_sandbox": "⬇️",
    "execute_system_command": "💻",    "write_file": "📝",
    "read_file": "📖",                 "append_file": "➕",
    "delete_file": "🗑️",              "list_directory": "📂",
    "get_datetime": "📅",              "safety_rollback": "🔁",
    "tavily_search": "🔍",
}

def hitl_loop(config: dict):
    while True:
        state = app.get_state(config)

        if not state.next:
            final = state.values["messages"][-1]
            text = extract_text(final)
            if text:
                print(f"\n🤖 OpenClaw:\n{text}")
            break

        last = state.values["messages"][-1]
        has_tools = hasattr(last, "tool_calls") and last.tool_calls

        if not has_tools:
            for _ in app.stream(None, config, stream_mode="values"):
                pass
            continue

        print("\n" + "─" * 55)
        print("⚠️  SYSTEM COMMAND — Approval Required")
        for tc in last.tool_calls:
            icon = ICONS.get(tc["name"], "🔧")
            preview = str(tc["args"])[:300]
            print(f"  {icon} {tc['name']}\n     {preview}")
        print("─" * 55)

        if input("Approve? (y/n): ").strip().lower() == "y":
            print("▶ Executing...\n")
            for _ in app.stream(None, config, stream_mode="values"):
                pass
        else:
            print("❌ Rejected.\n")
            rejections = [
                ToolMessage(
                    content="User rejected. Do NOT retry. Explain what you were attempting.",
                    tool_call_id=tc["id"]
                ) for tc in last.tool_calls
            ]
            app.update_state(config, {"messages": rejections}, as_node="tools_hitl")
            for _ in app.stream(None, config, stream_mode="values"):
                pass

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    thread_id = "openclaw_master"
    config = {"configurable": {"thread_id": thread_id}}

    print("\n" + "═" * 60)
    print("  🦅 OpenClaw  —  Autonomous AI Agent")
    print("  Commands: exit | flush | new")
    print("═" * 60 + "\n")

    # Start background scheduler
    threading.Thread(target=_cron_loop, daemon=True).start()
    print("🕐 Scheduler daemon started\n")

    try:
        while True:
            user_input = input("You: ").strip()
            if not user_input:
                continue

            if user_input.lower() == "exit":
                print("Goodbye.")
                break

            if user_input.lower() == "flush":
                print("ℹ️  Flush not needed — memories auto-save to ChromaDB.")
                continue

            if user_input.lower() == "new":
                thread_id = str(uuid.uuid4())
                config = {"configurable": {"thread_id": thread_id}}
                print(f"🆕 New session: {thread_id[:8]}...\n")
                continue

            # Background: extract + save long-term memories from user message
            threading.Thread(
                target=process_user_message,
                args=(user_input, llm),
                daemon=True
            ).start()

            # Stream to graph
            for _ in app.stream(
                {"messages": [HumanMessage(content=user_input)], "route": ""},
                config,
                stream_mode="values"
            ):
                pass

            hitl_loop(config)

    except KeyboardInterrupt:
        print("\n\nInterrupted.")
    finally:
        cleanup()

if __name__ == "__main__":
    main()