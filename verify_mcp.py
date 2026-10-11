r"""
Verify the registered MCP server: real stdio handshake + tool surface + tool call.
Run:  .venv\Scripts\python.exe verify_mcp.py
Exit code 0 means pi will see the same tools this script sees.
"""

import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import TextContent

ROOT = Path(__file__).parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
EXPECTED_TOOLS = 66


async def main() -> int:
    params = StdioServerParameters(
        command=str(PYTHON),
        args=[str(ROOT / "main.py")],
        env={"PYTHONUNBUFFERED": "1"},
    )
    async with (
        stdio_client(params) as (read, write),
        ClientSession(read, write) as session,
    ):
        init = await session.initialize()
        info = getattr(init, "server_info", None)
        print(f"server: {getattr(info, 'name', '?')} (protocol {init.protocol_version})")

        tools = (await session.list_tools()).tools
        print(f"tools: {len(tools)}")
        for t in tools:
            print(f"  - {t.name}")

        if len(tools) != EXPECTED_TOOLS:
            print(f"FAIL: expected {EXPECTED_TOOLS} tools, got {len(tools)}", file=sys.stderr)
            return 1

        incomplete = [
            t.name
            for t in tools
            if t.annotations is None
            or not t.annotations.title
            or any(
                getattr(t.annotations, hint) is None
                for hint in ("read_only_hint", "destructive_hint", "idempotent_hint", "open_world_hint")
            )
        ]
        if incomplete:
            print(f"FAIL: tools missing annotations: {incomplete}", file=sys.stderr)
            return 1
        mutating = [t for t in tools if t.annotations.read_only_hint is False]
        unguarded = [t.name for t in mutating if "confirm" not in (t.input_schema.get("properties") or {})]
        if unguarded:
            print(f"FAIL: mutating tools without a confirm argument: {unguarded}", file=sys.stderr)
            return 1
        print(f"annotations: complete ({len(mutating)} mutating tools, all gated on confirm)")

        result = await session.call_tool("whoami", {})
        text = next((b.text for b in result.content if isinstance(b, TextContent)), "")
        if not text:
            print(f"FAIL: whoami returned no text content: {result.content}", file=sys.stderr)
            return 1
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as e:
            print(f"FAIL: whoami returned non-JSON: {text[:200]} ({e})", file=sys.stderr)
            return 1
        print(f"whoami: {json.dumps(payload)}")
        if not payload.get("success"):
            print("FAIL: whoami did not return success=true", file=sys.stderr)
            return 1

        result = await session.call_tool("delete_file", {"path": "x", "message": "m", "sha": "s"})
        blocked = next((b.text for b in result.content if isinstance(b, TextContent)), "")
        if "confirm=true" not in blocked:
            print(f"FAIL: delete_file ran without confirm: {blocked[:200]}", file=sys.stderr)
            return 1
        print("confirm gate: delete_file refused without confirm")
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))