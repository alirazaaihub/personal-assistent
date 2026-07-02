"""
mcp_loader.py
--------------
Autoagent ke liye MCP tools loader.

Kya karta hai:
  - mcp_config.json (agar exist karti hai) se MCP servers connect karta hai
  - Har server alag se try karta hai — ek fail ho to baaki kaam karte rahein
  - Sab tools ko sync wrapper mein convert karta hai taake
    get_all_tools() (sync codebase) ke saath seamlessly merge ho saken
  - mcp_config.json missing ho to silently empty list return karta hai
    (AutoAgent bina MCP ke bhi normally chalta rahega)

LangGraph create_react_agent yahan use NAHI hota — yeh sirf tools
fetch karta hai jo tera apna cognitive_node consume karega.
"""

import asyncio
import sys
import threading
from pathlib import Path
from langchain_core.tools import Tool, StructuredTool

from mcp_config_loader import load_mcp_config, MCPConfigError
from config import BASE_DIR, log

MCP_CONFIG_PATH = BASE_DIR / "mcp_config.json"

_mcp_tools_cache = None
_mcp_client = None
_loop = None
_loop_thread = None


def _unwrap_exception(exc: BaseException) -> str:
    """Nested TaskGroup/ExceptionGroup errors ko readable banata hai."""
    lines = []

    def _walk(e: BaseException, depth: int = 0):
        prefix = "  " * depth + "-> "
        lines.append(f"{prefix}{type(e).__name__}: {e}")
        sub_excs = getattr(e, "exceptions", None)
        if sub_excs:
            for sub in sub_excs:
                _walk(sub, depth + 1)
        elif e.__cause__ is not None:
            _walk(e.__cause__, depth + 1)

    _walk(exc)
    return "\n".join(lines)


def _get_background_loop():
    """
    Ek persistent background event loop banata hai jo MCP ke async
    operations ko sync code se call karne deta hai.
    """
    global _loop, _loop_thread
    if _loop is None:
        _loop = asyncio.new_event_loop()
        _loop_thread = threading.Thread(target=_loop.run_forever, daemon=True)
        _loop_thread.start()
    return _loop


def _run_async(coro):
    """Async coroutine ko background loop par chalata hai aur result wait karta hai."""
    loop = _get_background_loop()
    future = asyncio.run_coroutine_threadsafe(coro, loop)
    return future.result(timeout=60)


async def _fetch_mcp_tools_async():
    """MCP servers se tools fetch karta hai — async core logic."""
    from langchain_mcp_adapters.client import MultiServerMCPClient

    global _mcp_client

    try:
        connections = load_mcp_config(str(MCP_CONFIG_PATH))
    except MCPConfigError as e:
        log("MCP CONFIG ERROR", str(e))
        return []

    try:
        _mcp_client = MultiServerMCPClient(connections)
    except Exception as e:
        log("MCP CLIENT ERROR", f"MultiServerMCPClient banate waqt error: {e}")
        return []

    all_tools = []
    failed = {}

    for server_name in connections:
        try:
            server_tools = await _mcp_client.get_tools(server_name=server_name)
            all_tools.extend(server_tools)
            tool_names = [t.name for t in server_tools]
            log("MCP SERVER CONNECTED", f"'{server_name}' -> {tool_names}")
            print(f"   [MCP OK] '{server_name}' -> {tool_names}")
        except Exception as e:
            detail = _unwrap_exception(e)
            failed[server_name] = detail
            log("MCP SERVER FAILED", f"'{server_name}':\n{detail}")
            print(f"   [MCP FAILED] '{server_name}': {detail}", file=sys.stderr)

    if failed:
        print(f"\n[MCP WARNING] Yeh server(s) connect nahi ho sake: {list(failed.keys())}\n",
              file=sys.stderr)

    return all_tools


def _wrap_async_tool_sync(async_tool) -> Tool:
    """
    LangChain MCP tools async hote hain. Autoagent ka parallel_tool_node
    sync `.invoke()` call karta hai — isliye ek sync wrapper banate hain
    jo background event loop par async tool ko run karta hai.
    """
    def sync_func(**kwargs):
        try:
            result = _run_async(async_tool.ainvoke(kwargs))
            return str(result)
        except Exception as e:
            log("MCP TOOL ERROR", f"{async_tool.name}: {e}")
            return f"MCP Tool Error ({async_tool.name}): {e}"

    return StructuredTool(
        name=async_tool.name,
        description=async_tool.description or f"MCP tool: {async_tool.name}",
        func=sync_func,
        args_schema=getattr(async_tool, "args_schema", None),
    )


def get_mcp_tools() -> list:
    """
    Main entry point — Autoagent ke get_all_tools() se yahi call hoga.
    Cached rehta hai taake har query pe dobara connect na karna pade.
    Agar mcp_config.json exist nahi karti, khaali list return hoti hai.
    """
    global _mcp_tools_cache

    if not MCP_CONFIG_PATH.exists():
        return []

    if _mcp_tools_cache is not None:
        return _mcp_tools_cache

    try:
        raw_tools = _run_async(_fetch_mcp_tools_async())
        wrapped = [_wrap_async_tool_sync(t) for t in raw_tools]
        _mcp_tools_cache = wrapped
        log("MCP TOOLS LOADED", f"{len(wrapped)} tool(s): " +
            ", ".join(t.name for t in wrapped))
        return wrapped
    except Exception as e:
        log("MCP LOAD ERROR", str(e))
        print(f"[MCP ERROR] Tools load nahi ho sake: {e}", file=sys.stderr)
        return []


async def _fetch_mcp_tools_with_status_async():
    """
    MCP servers se tools fetch karta hai aur connected/failed dono
    server names explicitly return karta hai — taake caller ko
    pata chale exactly kaunsa server kaam kar raha hai.
    """
    from langchain_mcp_adapters.client import MultiServerMCPClient

    global _mcp_client

    try:
        connections = load_mcp_config(str(MCP_CONFIG_PATH))
    except MCPConfigError as e:
        log("MCP CONFIG ERROR", str(e))
        return [], [], list(connections.keys()) if 'connections' in dir() else []

    try:
        _mcp_client = MultiServerMCPClient(connections)
    except Exception as e:
        log("MCP CLIENT ERROR", f"MultiServerMCPClient banate waqt error: {e}")
        return [], [], list(connections.keys())

    all_tools = []
    connected_servers = []
    failed_servers = []

    for server_name in connections:
        try:
            server_tools = await _mcp_client.get_tools(server_name=server_name)
            all_tools.extend(server_tools)
            connected_servers.append(server_name)
            tool_names = [t.name for t in server_tools]
            log("MCP SERVER CONNECTED", f"'{server_name}' -> {tool_names}")
            print(f"   [MCP OK] '{server_name}' -> {tool_names}")
        except Exception as e:
            detail = _unwrap_exception(e)
            failed_servers.append(server_name)
            log("MCP SERVER FAILED", f"'{server_name}':\n{detail}")
            print(f"   [MCP FAILED] '{server_name}': {detail}", file=sys.stderr)

    return all_tools, connected_servers, failed_servers


def reload_mcp_tools_with_status() -> tuple[list, list]:
    """
    Cache clear karke MCP servers se dobara connect karta hai.
    Returns: (connected_server_names, failed_server_names)
    Caller ko pata chalta hai EXACTLY kaunsa server live hai.
    """
    global _mcp_tools_cache

    if not MCP_CONFIG_PATH.exists():
        return [], []

    try:
        raw_tools, connected, failed = _run_async(_fetch_mcp_tools_with_status_async())
        wrapped = [_wrap_async_tool_sync(t) for t in raw_tools]
        _mcp_tools_cache = wrapped
        log("MCP TOOLS LOADED",
            f"{len(wrapped)} tool(s) from {connected} | failed: {failed}")
        return connected, failed
    except Exception as e:
        log("MCP LOAD ERROR", str(e))
        print(f"[MCP ERROR] Tools load nahi ho sake: {e}", file=sys.stderr)
        return [], ["unknown (load crashed)"]


def reload_mcp_tools() -> list:
    """Cache clear karke MCP servers se dobara connect karta hai."""
    global _mcp_tools_cache
    _mcp_tools_cache = None
    return get_mcp_tools()
