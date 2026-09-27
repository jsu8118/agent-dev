"""Helpers shared by the Day 5 MCP labs (02-04) and solutions.  Not a lab: files starting with "_" are skipped.

* `load_lab("01_mcp_server")` imports a lab module by file name (lab names start with a digit,
  so a plain `import` statement cannot load them).
* `plant_ops_stdio_params()` describes how an MCP host launches lab 01 as a stdio server.
* `tapped(transport, log)` wraps any MCP client transport and records every JSON-RPC message that
  crosses it - the MCP equivalent of watching the wire with tcpdump.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Any, AsyncIterator

from mcp import StdioServerParameters

LABS_DIR = Path(__file__).resolve().parent
SERVER_SCRIPT = LABS_DIR / "01_mcp_server.py"


def load_lab(stem: str) -> ModuleType:
    """Import labs/<stem>.py once and return the module."""
    name = f"day5_{stem}"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, LABS_DIR / f"{stem}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def plant_ops_stdio_params() -> StdioServerParameters:
    """How a host starts lab 01 as a subprocess.

    The MCP SDK does NOT pass your environment to the child - only a small allow-list (HOME, PATH,
    USER, ...). Anything the server needs must be given explicitly: least privilege by default.
    """
    return StdioServerParameters(command=sys.executable, args=[str(SERVER_SCRIPT), "--serve"],
                                 env={"LABKIT_QUIET": "1", "PYTHONUNBUFFERED": "1"}, cwd=str(LABS_DIR))


# --------------------------------------------------------------------------- wire tap
class _TapRead:
    """Wraps the transport's read stream (server -> client) and logs each JSON-RPC message."""

    def __init__(self, inner: Any, log: list[tuple[str, float, dict]]) -> None:
        self._inner, self._log = inner, log

    def _record(self, item: Any) -> Any:
        message = getattr(item, "message", None)
        if message is not None:
            self._log.append(("<-", time.perf_counter(), message.model_dump(by_alias=True, mode="json",
                                                                             exclude_none=True)))
        return item

    async def receive(self) -> Any:
        return self._record(await self._inner.receive())

    def __aiter__(self) -> "_TapRead":
        return self

    async def __anext__(self) -> Any:
        return self._record(await self._inner.__anext__())

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> "_TapRead":
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)

    def __getattr__(self, name: str) -> Any:        # e.g. `last_context`, read by the dispatcher
        return getattr(self._inner, name)


class _TapWrite:
    """Wraps the transport's write stream (client -> server)."""

    def __init__(self, inner: Any, log: list[tuple[str, float, dict]]) -> None:
        self._inner, self._log = inner, log

    async def send(self, item: Any) -> None:
        self._log.append(("->", time.perf_counter(), item.message.model_dump(by_alias=True, mode="json",
                                                                            exclude_none=True)))
        await self._inner.send(item)

    async def aclose(self) -> None:
        await self._inner.aclose()

    async def __aenter__(self) -> "_TapWrite":
        await self._inner.__aenter__()
        return self

    async def __aexit__(self, *exc: Any) -> Any:
        return await self._inner.__aexit__(*exc)


@asynccontextmanager
async def tapped(transport: Any, log: list[tuple[str, float, dict]]) -> AsyncIterator[tuple[Any, Any]]:
    """A Transport that behaves like `transport` but records (direction, time, message) tuples."""
    async with transport as (read_stream, write_stream):
        yield _TapRead(read_stream, log), _TapWrite(write_stream, log)


def summarize_jsonrpc(message: dict, width: int = 150) -> str:
    """One line per JSON-RPC message: its kind, id, method and a compact preview."""
    if "method" in message:
        kind = "request" if "id" in message else "notification"
        head = f"{kind:<12} id={message.get('id', '-'):<3} {message['method']}"
        body = message.get("params", {})
    elif "error" in message:
        head = f"{'error':<12} id={message.get('id'):<3}"
        body = message["error"]
    else:
        head = f"{'response':<12} id={message.get('id'):<3}"
        body = message.get("result", {})
    preview = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    room = max(20, width - len(head) - 1)
    return f"{head} {preview[:room]}{'...' if len(preview) > room else ''}"


def tool_result_text(result: Any, limit: int = 300) -> str:
    """Text of an MCP CallToolResult (text blocks joined), shortened for printing."""
    text = "\n".join(getattr(block, "text", f"<{getattr(block, 'type', '?')}>") for block in result.content)
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."
