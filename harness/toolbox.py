"""The MCP client side: where a named tool becomes a running one.

The MCP SDK is async and the model SDKs are not, so the session lives on its own
event loop in a background thread and the loop talks to it synchronously. That
keeps the agent loop readable, which matters more here than elegance — it is the
file someone will read to understand the whole harness.

A ToolBox is also the registry: the loop asks it what exists and what shape the
arguments take, and never takes the model's word for either.
"""
import asyncio
import json
import sys
import threading
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import stdio_client, StdioServerParameters

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SERVER = REPO_ROOT / "mcp_server" / "server.py"


class ToolBox:
    """One MCP server, spawned over stdio, callable from a synchronous loop."""

    def __init__(self, command=None, args=None, server_path=None):
        self.command = command or sys.executable
        self.args = args or [str(server_path or DEFAULT_SERVER)]
        self._tools = {}
        self._loop = None
        self._thread = None
        self._session = None
        self._ready = threading.Event()
        self._stop = None
        self._failure = None

    # -- lifecycle ----------------------------------------------------------

    def __enter__(self):
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=30):
            raise RuntimeError("MCP server did not become ready within 30s")
        if self._failure is not None:
            raise self._failure
        return self

    def __exit__(self, *_):
        if self._loop is not None and self._stop is not None:
            self._loop.call_soon_threadsafe(self._stop.set)
        if self._thread is not None:
            self._thread.join(timeout=10)

    def _run_loop(self):
        asyncio.run(self._serve())

    async def _serve(self):
        self._loop = asyncio.get_running_loop()
        self._stop = asyncio.Event()
        try:
            params = StdioServerParameters(command=self.command, args=self.args)
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listing = await session.list_tools()
                    self._tools = {tool.name: tool for tool in listing.tools}
                    self._session = session
                    self._ready.set()
                    await self._stop.wait()
        except Exception as error:            # surfaced to __enter__'s caller
            self._failure = error
            self._ready.set()

    # -- registry -----------------------------------------------------------

    def names(self):
        return sorted(self._tools)

    def __contains__(self, name):
        return name in self._tools

    def schema(self, name):
        return self._tools[name].input_schema

    def definitions(self):
        """Neutral tool definitions, for whichever provider is in use."""
        return [{"name": tool.name,
                 "description": tool.description or "",
                 "input_schema": tool.input_schema}
                for tool in self._tools.values()]

    # -- dispatch -----------------------------------------------------------

    def call(self, name, arguments, timeout_s=60):
        future = asyncio.run_coroutine_threadsafe(
            self._call(name, arguments), self._loop)
        return future.result(timeout=timeout_s)

    async def _call(self, name, arguments):
        result = await self._session.call_tool(name, arguments)
        structured = getattr(result, "structured_content", None)
        if structured:
            return structured
        for block in result.content:
            text = getattr(block, "text", None)
            if text is None:
                continue
            try:
                return json.loads(text)
            except json.JSONDecodeError:
                return {"result": text}
        return {"result": None}
