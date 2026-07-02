"""
whatsapp_server.py
--------------------
OpenClaw ko WhatsApp se connect karta hai.
Includes built-in scheduler — main.py ki zaroorat nahi.

Flow:
  WhatsApp user → Meta webhook → yeh server → agent_graph.app.invoke()
  → response wapis WhatsApp pe

Scheduler flow:
  SCHEDULE.md → whatsapp_agent action → agent invoke → WhatsApp pe send

Run: uv run uvicorn whatsapp_server:app --host 0.0.0.0 --port 8000
"""

import os
import io
import time
import threading
import tempfile
import schedule
import requests
from datetime import datetime
from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import PlainTextResponse
from dotenv import load_dotenv
from groq import Groq
from langchain_core.messages import HumanMessage, ToolMessage

from agent_graph import app as openclaw_app
from config import BASE_DIR, SCHEDULE_FILE, log

load_dotenv(override=True)

app = FastAPI()

WHATSAPP_TOKEN  = os.getenv("WHATSAPP_TOKEN")
PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
VERIFY_TOKEN    = os.getenv("VERIFY_TOKEN")
GROQ_API_KEY    = os.getenv("GROQ_API_KEY")
# Default number jisko scheduled messages jaayenge (agar task mein specify na ho)
DEFAULT_WHATSAPP_NUMBER = os.getenv("MY_WHATSAPP_NUMBER", "")

groq_client = Groq(api_key=GROQ_API_KEY) if GROQ_API_KEY else None

DANGEROUS_TOOLS = {"execute_system_command", "safety_rollback"}


# ── Text extract helper ────────────────────────────────────────────────────────
def extract_text(msg) -> str:
    content = msg.content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        ).strip()
    return (content or "").strip()


# ── Core: Run OpenClaw agent ───────────────────────────────────────────────────
def run_openclaw(user_number: str, user_text: str) -> str:
    """
    WhatsApp number ko thread_id ki tarah use karta hai.
    HITL: safe tools auto-approve, dangerous auto-deny.
    """
    thread_id = f"whatsapp_{user_number}"
    config = {"configurable": {"thread_id": thread_id}}

    try:
        openclaw_app.invoke(
            {"messages": [HumanMessage(content=user_text)], "route": ""},
            config
        )

        for _ in range(10):
            state = openclaw_app.get_state(config)

            if not state.next:
                final = state.values["messages"][-1]
                return extract_text(final) or "Done."

            last = state.values["messages"][-1]
            has_tools = hasattr(last, "tool_calls") and last.tool_calls

            if not has_tools:
                openclaw_app.invoke(None, config)
                continue

            dangerous = [tc for tc in last.tool_calls if tc["name"] in DANGEROUS_TOOLS]

            if dangerous:
                rejections = [
                    ToolMessage(
                        content="Denied: system commands not allowed via WhatsApp.",
                        tool_call_id=tc["id"]
                    )
                    for tc in last.tool_calls
                ]
                openclaw_app.update_state(config, {"messages": rejections}, as_node="tools_hitl")
                openclaw_app.invoke(None, config)
            else:
                openclaw_app.invoke(None, config)

        return "Request took too long. Please try a simpler query."

    except Exception as e:
        log("WHATSAPP AGENT ERROR", str(e))
        return f"Error: {str(e)[:200]}"


# ── Send Text Message ──────────────────────────────────────────────────────────
def send_text_message(to: str, text: str):
    if not to or not WHATSAPP_TOKEN or not PHONE_NUMBER_ID:
        log("WHATSAPP SEND SKIPPED", "Missing token/phone_id/to number")
        return

    url = f"https://graph.facebook.com/v19.0/{PHONE_NUMBER_ID}/messages"
    headers = {
        "Authorization": f"Bearer {WHATSAPP_TOKEN}",
        "Content-Type": "application/json"
    }
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text[:4000]}
    }
    r = requests.post(url, headers=headers, json=payload)
    log("WHATSAPP OUT", f"To {to} | Status: {r.status_code}")


# ── Download WhatsApp Media ────────────────────────────────────────────────────
def download_whatsapp_media(media_id: str) -> bytes | None:
    url = f"https://graph.facebook.com/v19.0/{media_id}"
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    r = requests.get(url, headers=headers)
    if r.status_code != 200:
        return None
    media_url = r.json().get("url")
    r2 = requests.get(media_url, headers=headers)
    return r2.content if r2.status_code == 200 else None


# ── Transcribe with Groq Whisper ───────────────────────────────────────────────
def transcribe_audio(audio_bytes: bytes) -> str | None:
    if not groq_client:
        return None
    try:
        with tempfile.NamedTemporaryFile(suffix=".ogg", delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name
        with open(tmp_path, "rb") as f:
            result = groq_client.audio.transcriptions.create(
                model="whisper-large-v3",
                file=("audio.ogg", f, "audio/ogg"),
                language="en"
            )
        os.unlink(tmp_path)
        return result.text.strip()
    except Exception as e:
        log("TRANSCRIPTION ERROR", str(e))
        return None


# ── Webhook Verification ───────────────────────────────────────────────────────
@app.get("/webhook")
async def verify(request: Request):
    params    = dict(request.query_params)
    mode      = params.get("hub.mode")
    token     = params.get("hub.verify_token")
    challenge = params.get("hub.challenge")
    if mode == "subscribe" and token == VERIFY_TOKEN:
        return PlainTextResponse(content=challenge, status_code=200)
    raise HTTPException(status_code=403, detail="Verification failed")


# ── Incoming Messages ──────────────────────────────────────────────────────────
@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    try:
        entry   = data["entry"][0]
        changes = entry["changes"][0]["value"]
        msg     = changes["messages"][0]
        user_number = msg["from"]

        if msg["type"] == "text":
            user_text = msg["text"]["body"]
            log("WHATSAPP IN", f"From {user_number}: {user_text}")
            answer = run_openclaw(user_number, user_text)
            send_text_message(user_number, answer)

        elif msg["type"] == "audio":
            audio_id = msg["audio"]["id"]
            audio_bytes = download_whatsapp_media(audio_id)
            if not audio_bytes:
                send_text_message(user_number, "Audio download fail. Dobara try karo.")
                return {"status": "ok"}
            user_text = transcribe_audio(audio_bytes)
            if not user_text:
                send_text_message(user_number, "Samajh nahi aaya. Dobara bolein.")
                return {"status": "ok"}
            log("WHATSAPP TRANSCRIBED", user_text)
            answer = run_openclaw(user_number, user_text)
            send_text_message(user_number, answer)

    except (KeyError, IndexError) as e:
        log("WHATSAPP PARSE ERROR", str(e))

    return {"status": "ok"}


# ── SCHEDULER ─────────────────────────────────────────────────────────────────
_sched_mtime = 0.0

def _dispatch_scheduled_task(action: str, arg1: str, arg2: str):
    """
    Scheduled task execute karo.
    whatsapp_agent action: agent invoke karo aur result WhatsApp pe bhejo.
    """
    log("SCHEDULER TASK", f"action={action} | arg1={arg1[:50]} | arg2={arg2[:50]}")

    if action == "whatsapp_agent":
        # arg1 = WhatsApp number (ya "default"), arg2 = task prompt
        to_number = arg1 if arg1 and arg1 != "default" else DEFAULT_WHATSAPP_NUMBER
        prompt    = arg2

        if not to_number:
            log("SCHEDULER ERROR", "MY_WHATSAPP_NUMBER .env mein set nahi — number specify karo SCHEDULE.md mein")
            return
        if not prompt:
            log("SCHEDULER ERROR", "Task prompt empty hai")
            return

        log("SCHEDULER AGENT", f"Running task for {to_number}: {prompt}")
        result = run_openclaw(to_number, prompt)
        send_text_message(to_number, f"⏰ Scheduled Task:\n\n{result}")

    elif action == "write_file":
        path = BASE_DIR / arg1
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(arg2, encoding="utf-8")
        log("SCHEDULER WRITE", str(path))

    elif action == "append_file":
        path = BASE_DIR / arg1
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{arg2}")
        log("SCHEDULER APPEND", str(path))

    elif action == "notify":
        import subprocess
        ps = (
            f'Add-Type -AssemblyName System.Windows.Forms;'
            f'$n=New-Object System.Windows.Forms.NotifyIcon;'
            f'$n.Icon=[System.Drawing.SystemIcons]::Information;'
            f'$n.Visible=$true;'
            f'$n.ShowBalloonTip(5000,"OpenClaw","{arg1}",'
            f'[System.Windows.Forms.ToolTipIcon]::Info)'
        )
        subprocess.Popen(["powershell", "-Command", ps],
                        creationflags=subprocess.CREATE_NO_WINDOW)
        log("SCHEDULER NOTIFY", arg1)

    elif action == "run_script":
        import subprocess
        full = BASE_DIR / arg1
        if full.exists():
            subprocess.run(f'python "{full}" {arg2}', shell=True, cwd=str(BASE_DIR))
            log("SCHEDULER SCRIPT", str(full))


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

        time_str = parts[0]
        action   = parts[1].lower()
        arg1     = parts[2] if len(parts) > 2 else ""
        arg2     = parts[3] if len(parts) > 3 else ""

        try:
            fmt = "%I:%M %p" if ("AM" in time_str.upper() or "PM" in time_str.upper()) else "%H:%M"
            t24 = datetime.strptime(time_str.strip(), fmt).strftime("%H:%M")
        except ValueError:
            log("SCHEDULER PARSE ERROR", f"Bad time format: {time_str}")
            continue

        supported = {"whatsapp_agent", "write_file", "append_file", "notify", "run_script"}
        if action not in supported:
            log("SCHEDULER PARSE ERROR", f"Unknown action: {action}")
            continue

        schedule.every().day.at(t24).do(
            _dispatch_scheduled_task, action, arg1, arg2
        ).tag("user")
        count += 1
        log("SCHEDULER REGISTERED", f"[{t24}] {action} → {arg1[:40]}")

    _sched_mtime = SCHEDULE_FILE.stat().st_mtime if SCHEDULE_FILE.exists() else 0.0
    print(f"📅 [Scheduler] {count} task(s) loaded from SCHEDULE.md")


def _scheduler_loop():
    _load_schedule()
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


# ── Startup: launch scheduler thread ──────────────────────────────────────────
@app.on_event("startup")
async def startup():
    t = threading.Thread(target=_scheduler_loop, daemon=True)
    t.start()
    print("🕐 Scheduler daemon started inside WhatsApp server")


@app.get("/")
async def root():
    return {"status": "OpenClaw WhatsApp bridge running with scheduler"}