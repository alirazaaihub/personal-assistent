"""
mcp_config_loader.py
---------------------
mcp_config.json ko parse karta hai aur har server ko
MultiServerMCPClient ke required format mein convert karta hai.

Supports:
  - Local servers  -> transport: "stdio"
  - Remote servers -> transport: "streamable_http" ya "sse"

Agar "transport" key na ho to khud guess karta hai:
  - "command" hai -> stdio
  - "url" hai     -> streamable_http
"""

from __future__ import annotations
import json
import os
import sys
from typing import Any, Dict


class MCPConfigError(Exception):
    pass


def _normalize_server_entry(name: str, entry: Dict[str, Any]) -> Dict[str, Any]:
    entry = dict(entry)
    transport = entry.get("transport")

    if not transport:
        if "command" in entry:
            transport = "stdio"
        elif "url" in entry:
            transport = "streamable_http"
        else:
            raise MCPConfigError(
                f"Server '{name}': 'transport' specify nahi hai aur "
                f"'command' ya 'url' me se koi bhi field nahi mili."
            )

    entry["transport"] = transport

    if transport == "stdio":
        if "command" not in entry:
            raise MCPConfigError(f"Local server '{name}' me 'command' field zaroori hai.")
        entry.setdefault("args", [])
        entry.setdefault("env", {})

    elif transport in ("streamable_http", "sse", "http"):
        if transport == "http":
            entry["transport"] = "streamable_http"
        if "url" not in entry:
            raise MCPConfigError(f"Remote server '{name}' me 'url' field zaroori hai.")
        entry.setdefault("headers", {})

    else:
        raise MCPConfigError(
            f"Server '{name}': unsupported transport '{transport}'. "
            f"Supported: stdio, streamable_http, sse"
        )

    return entry


def load_mcp_config(path: str) -> Dict[str, Dict[str, Any]]:
    if not os.path.exists(path):
        raise MCPConfigError(f"Config file nahi mili: '{path}'")

    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except json.JSONDecodeError as e:
        raise MCPConfigError(f"Config file '{path}' valid JSON nahi hai: {e}")

    servers = raw.get("mcpServers", raw)

    if not isinstance(servers, dict) or not servers:
        raise MCPConfigError(
            "Config me koi MCP server defined nahi mila. "
            "Expected: {\"mcpServers\": {\"name\": {...}}}"
        )

    normalized: Dict[str, Dict[str, Any]] = {}
    errors = []

    for name, entry in servers.items():
        try:
            normalized[name] = _normalize_server_entry(name, entry)
        except MCPConfigError as e:
            errors.append(str(e))

    if errors:
        print("[WARNING] Kuch MCP servers invalid hain aur skip ho gaye:", file=sys.stderr)
        for err in errors:
            print(f"   - {err}", file=sys.stderr)

    if not normalized:
        raise MCPConfigError("Koi bhi valid MCP server config se load nahi ho saka.")

    return normalized


if __name__ == "__main__":
    cfg_path = sys.argv[1] if len(sys.argv) > 1 else "mcp_config.json"
    try:
        result = load_mcp_config(cfg_path)
        print(json.dumps(result, indent=2))
    except MCPConfigError as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)