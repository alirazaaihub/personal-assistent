from pathlib import Path
from datetime import datetime

BASE_DIR       = Path("D:/claw").resolve()
SKILLS_DIR     = BASE_DIR / "skills"
LOGS_DIR       = BASE_DIR / "logs"
DOWNLOADS_DIR  = BASE_DIR / "downloads"
CHROMA_DIR     = BASE_DIR / "chroma_db"
CHECKPOINT_DB  = str(BASE_DIR / "Autoagent_state.db")

SOUL_FILE      = BASE_DIR / "SOUL.md"
HEARTBEAT_FILE = BASE_DIR / "HEARTBEAT.md"
TOOLS_FILE     = BASE_DIR / "TOOLS.md"
SCHEDULE_FILE  = BASE_DIR / "SCHEDULE.md"
MCP_CONFIG_FILE = BASE_DIR / "mcp_config.json"

# Create all required directories
for d in [SKILLS_DIR, LOGS_DIR, DOWNLOADS_DIR, CHROMA_DIR]:
    d.mkdir(parents=True, exist_ok=True)

def get_daily_log_path() -> Path:
    return LOGS_DIR / f"{datetime.now().strftime('%Y-%m-%d')}.md"

def log(event_type: str, content: str):
    """Append timestamped entry to today's log file."""
    ts = datetime.now().strftime("%H:%M:%S")
    entry = f"\n### [{ts}] {event_type}\n{content}\n"
    with open(get_daily_log_path(), "a", encoding="utf-8") as f:
        f.write(entry)

def seed_workspace():
    """Creates required workspace files on first run."""
    if not SOUL_FILE.exists():
        SOUL_FILE.write_text(
            "# SOUL.md — Autoagent Identity\n\n"
            "## Core Persona\n"
            "You are Autoagent, an elite autonomous AI agent built on LangGraph + GPT-4o.\n"
            "You have full access to local file system, terminal, E2B sandbox, and web search.\n\n"
            "## Behavioral Rules\n"
            "- Think before acting. Analyze before running any command.\n"
            "- NEVER hallucinate results. Use tools to fetch real data.\n"
            "- BEFORE writing new Python code, check skills/ folder first.\n"
            "- If new code works, ALWAYS save it as a skill in skills/.\n"
            "- If a terminal command fails, read STDERR and self-correct.\n"
            "- Never run destructive commands without HITL approval.\n"
            "- Be concise. Show results, not process narration.\n",
            encoding="utf-8"
        )
    if not HEARTBEAT_FILE.exists():
        HEARTBEAT_FILE.write_text(
            "# HEARTBEAT.md — Background Cron Tasks\n\n"
            "interval: 30m\n\n"
            "tasks:\n"
            "  - name: workspace-triage\n"
            "    prompt: |\n"
            "      Scan the workspace. Check if skills/ has broken files.\n"
            "      Check logs/ for recent errors. If healthy, reply HEARTBEAT_OK.\n"
            "      If issues found, describe them briefly.\n",
            encoding="utf-8"
        )
    if not TOOLS_FILE.exists():
        TOOLS_FILE.write_text(
            "# TOOLS.md — Active Skills Registry\n\nAuto-generated on startup.\n",
            encoding="utf-8"
        )
    if not SCHEDULE_FILE.exists():
        SCHEDULE_FILE.write_text(
            "# SCHEDULE.md — Task Scheduler\n\n"
            "# Format: TIME | action | arg1 | arg2\n"
            "# Actions: write_file, append_file, run_script, notify\n\n"
            "# Examples:\n"
            "# 10:00 PM | write_file | daily_hello.txt | Hello!\n"
            "# 04:00 AM | notify | Good Morning Ali!\n",
            encoding="utf-8"
        )

seed_workspace()
