"""
Verify the registered MCP server: real stdio handshake + tool call.
Run:  .venv\\Scripts\\python.exe verify_mcp.py
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
        if not payload.get("ok"):
            print("FAIL: whoami did not return ok=true", file=sys.stderr)
            return 1
        return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))