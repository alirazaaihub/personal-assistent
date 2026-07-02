
import ast
import os
import re
import subprocess
import datetime
from pathlib import Path
from langchain_core.tools import tool, Tool
from langchain_tavily import TavilySearch
from e2b_code_interpreter import Sandbox
from config import SKILLS_DIR, TOOLS_FILE, BASE_DIR, log
from mcp_loader import get_mcp_tools, reload_mcp_tools, reload_mcp_tools_with_status

# ── E2B Sandbox ───────────────────────────────────────────────────────────────
_sandbox = None

def get_sandbox() -> Sandbox:
    global _sandbox
    try:
        if _sandbox is None:
            _sandbox = Sandbox.create(timeout=300)
        else:
            _sandbox.set_timeout(300)
    except Exception:
        _sandbox = Sandbox.create(timeout=300)
    return _sandbox

# ── Path Safety ───────────────────────────────────────────────────────────────
def _safe(path: str) -> bool:
    try:
        return str(Path(path).resolve()).startswith(str(BASE_DIR))
    except Exception:
        return False

# ── Core Tools ────────────────────────────────────────────────────────────────



@tool
def save_mcp_config(json_content: str) -> str:
    """
    Save MCP server configuration to mcp_config.json.
    Use this when user provides MCP server JSON config in chat.
    Validates JSON syntax, structure, AND tests live connection before
    confirming success — never claims success if the server is unreachable.
    """
    import json as _json
    from mcp_config_loader import load_mcp_config, MCPConfigError

    try:
        parsed = _json.loads(json_content)
    except _json.JSONDecodeError as e:
        return f"❌ Invalid JSON syntax: {e}"

    config_path = BASE_DIR / "mcp_config.json"
    config_path.write_text(_json.dumps(parsed, indent=2), encoding="utf-8")

    # Validate structure using existing loader
    try:
        servers = load_mcp_config(str(config_path))
    except MCPConfigError as e:
        config_path.unlink()
        log("MCP CONFIG INVALID", str(e))
        return f"❌ Config structure invalid, not saved: {e}"

    # ── Actually test the connection — don't just trust the structure ──
    connected, failed = reload_mcp_tools_with_status()

    log("MCP CONFIG SAVED", f"Connected: {connected} | Failed: {failed}")

    if failed and not connected:
        config_path.unlink()  # Remove config that connects to nothing
        return (
            f"❌ Config was structurally valid but connection FAILED for: "
            f"{', '.join(failed)}\n"
            f"The server URL may be down, changed, or using a different "
            f"transport (sse vs streamable_http). Config was NOT kept.\n"
            f"Please verify the server URL is currently live and correct."
        )

    if failed:
        return (
            f"⚠️ Partial success.\n"
            f"✅ Connected: {', '.join(connected)}\n"
            f"❌ Failed: {', '.join(failed)} — these servers are unreachable "
            f"right now. Their tools won't be available."
        )

    return (
        f"✅ Connected successfully to: {', '.join(connected)}\n"
        f"Tools are live and available right now."
    )





@tool
def execute_python_in_sandbox(code: str) -> str:
    """Run Python code in a secure E2B cloud sandbox. Use for API calls, calculations, data processing."""
    try:
        sb = get_sandbox()
        result = sb.run_code(code)
        log("SANDBOX EXEC", f"```python\n{code[:300]}\n```\nResult: {str(result.text)[:200]}")
        if result.error:
            return f"Sandbox Error:\n{result.error}"
        return result.text.strip() if result.text else "(Executed — no output)"
    except Exception as e:
        return f"Sandbox Error: {str(e)}"

@tool
def download_from_sandbox(sandbox_path: str) -> str:
    """Download a file from E2B sandbox to D:/openclaw/downloads/"""
    try:
        sb = get_sandbox()
        content = sb.files.read(sandbox_path)
        dl = BASE_DIR / "downloads"
        dl.mkdir(exist_ok=True)
        local = dl / os.path.basename(sandbox_path)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(local, mode) as f:
            f.write(content)
        return f"✅ Downloaded: {local}"
    except Exception as e:
        return f"❌ Download failed: {e}"

@tool
def execute_system_command(command: str) -> str:
    """Run a terminal command in D:/openclaw workspace. Use 'uv run' for Python."""
    try:
        r = subprocess.run(
            command, shell=True, capture_output=True,
            text=True, timeout=45, cwd=str(BASE_DIR),
            encoding="utf-8", errors="ignore"
        )
        out = r.stdout.strip() or r.stderr.strip() or "Command successful."
        log("SHELL CMD", f"`{command}`\n{out[:400]}")
        return out[:3000]
    except subprocess.TimeoutExpired:
        return "Error: Command timed out (45s)."
    except Exception as e:
        return f"Terminal Error: {e}"

@tool
def read_file(path: str) -> str:
    """Read a file from the workspace."""
    if not _safe(path): return "Error: Path outside workspace."
    try: return Path(path).read_text(encoding="utf-8")
    except Exception as e: return f"Read Error: {e}"

@tool
def write_file(path: str, content: str) -> str:
    """Write or overwrite a file in the workspace."""
    if not _safe(path): return "Error: Path outside workspace."
    try:
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        log("FILE WRITE", f"Written: {path}")
        return f"✅ Written: {path}"
    except Exception as e: return f"Write Error: {e}"

@tool
def append_file(path: str, content: str) -> str:
    """Append content to a file in the workspace."""
    if not _safe(path): return "Error: Path outside workspace."
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n" + content)
        return f"✅ Appended: {path}"
    except Exception as e: return f"Append Error: {e}"

@tool
def delete_file(path: str) -> str:
    """Delete a file from the workspace."""
    if not _safe(path): return "Error: Path outside workspace."
    try:
        p = Path(path)
        if p.is_file():
            p.unlink()
            return f"🗑️ Deleted: {path}"
        return "Error: Not a file."
    except Exception as e: return f"Delete Error: {e}"

@tool
def list_directory(path: str = str(BASE_DIR)) -> str:
    """List files and folders in a workspace directory."""
    if not _safe(path): return "Error: Path outside workspace."
    try:
        items = sorted(Path(path).iterdir())
        if not items: return "Empty directory."
        return "\n".join(
            f"[DIR]  {i.name}" if i.is_dir() else f"[FILE] {i.name}"
            for i in items
        )
    except Exception as e: return f"List Error: {e}"


@tool
def get_datetime() -> str:
    """Get current system date and time."""
    return datetime.datetime.now().strftime("Today is %A, %Y-%m-%d %H:%M:%S")

@tool
def safety_rollback() -> str:
    """Rollback workspace to last Git checkpoint."""
    try:
        if not (BASE_DIR / ".git").exists():
            subprocess.run(
                'git init && git add . && git commit -m "OpenClaw Base"',
                shell=True, capture_output=True, cwd=str(BASE_DIR)
            )
            return "✅ Git initialized and base checkpoint created."
        subprocess.run("git reset --hard HEAD", shell=True,
                       capture_output=True, cwd=str(BASE_DIR))
        return "✅ Rolled back to last safe checkpoint."
    except Exception as e: return f"Rollback Error: {e}"

_tavily = TavilySearch(max_results=5)

@tool
def web_search(query: str) -> str:
    """
    Search the web for current information, news, facts, or any topic.
    Use this when you need up-to-date data that may not be in your training.
    Returns top 5 relevant results with titles, URLs, and snippets.
    """
    try:
        log("WEB SEARCH", f"Query: {query}")
        results = _tavily.invoke(query)
        if not results:
            log("WEB SEARCH RESULT", "No results found.")
            return "No results found."
        if isinstance(results, list):
            out = []
            for i, r in enumerate(results, 1):
                title   = r.get("title", "No title")
                url     = r.get("url", "")
                content = r.get("content", "")[:300]
                out.append(f"{i}. {title}\n   {url}\n   {content}")
            formatted = "\n\n".join(out)
            log("WEB SEARCH RESULT", f"{len(results)} results for: {query}\n{formatted[:400]}")
            return formatted
        log("WEB SEARCH RESULT", str(results)[:300])
        return str(results)
    except Exception as e:
        log("WEB SEARCH ERROR", str(e))
        return f"Search Error: {e}"

# ── Dynamic Skill Loader ──────────────────────────────────────────────────────

def _load_skill(path: Path) -> Tool:
    name = path.stem
    desc = f"Custom skill: {name}"
    try:
        src = path.read_text(encoding="utf-8")
        ast.parse(src)
        if '"""' in src:
            doc = src.split('"""')[1].strip()
            if doc: desc = doc
    except SyntaxError as se:
        def broken(*a, **k): return f"❌ Syntax error in '{name}': {se}"
        return Tool(name=name, func=broken, description=f"[BROKEN] {desc}")
    except Exception:
        pass

    def run(skill_args: str = "") -> str:
        cmd = f'python "{path}" {skill_args}'
        out = execute_system_command.invoke({"command": cmd})
        if "ModuleNotFoundError" in out or "No module named" in out:
            m = re.search(r"No module named '([^']+)'", out)
            if m:
                subprocess.run(f"uv pip install {m.group(1)}",
                               shell=True, capture_output=True)
                out = execute_system_command.invoke({"command": cmd})
        return out

    return Tool(name=name, func=run, description=desc)


def get_all_tools() -> list:
    core = [
        execute_python_in_sandbox, download_from_sandbox,
        execute_system_command, read_file, write_file,
        append_file, delete_file, list_directory,
        get_datetime, safety_rollback, web_search, save_mcp_config,
    ]
    skills = [_load_skill(f) for f in sorted(SKILLS_DIR.glob("*.py"))]

    # MCP tools — agar mcp_config.json maujood hai to attach hote hain
    mcp_tools = get_mcp_tools()

    all_tools = core + skills + mcp_tools

    # Auto-regenerate TOOLS.md
    registry = "# TOOLS.md — Active Skills Registry\n\nAuto-generated. Do not edit manually.\n\n"
    registry += "## Core Tools\n"
    for t in core:
        registry += f"- **`{t.name}`**: {t.description}\n"
    if skills:
        registry += "\n## Custom Skills\n"
        for t in skills:
            registry += f"- **`{t.name}`**: {t.description}\n"
    if mcp_tools:
        registry += "\n## MCP Tools (external servers)\n"
        for t in mcp_tools:
            registry += f"- **`{t.name}`**: {t.description}\n"
    TOOLS_FILE.write_text(registry, encoding="utf-8")

    return all_tools