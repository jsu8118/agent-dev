"""The code-execution container, simulated locally (code execution + programmatic tool calling).

What the real feature does: Claude writes a Python cell (or a bash command / file edit); the API runs it in a
sandboxed container and returns the output.  With programmatic tool calling, the cell can `await` your
client tools: the container pauses, the API returns a `tool_use` block whose `caller` names the code cell,
you answer with a normal `tool_result`, and the cell resumes with your text as the function's return value.

What the mock does: the cell is Python written by a *scenario policy* (not by a model), so running it in
this process is safe.  Client tools are exposed as `async` functions that return recorded results, or -
when no result has been recorded yet - register the pending call and pause the cell.  A container keeps its
files, variables and pending calls for the process lifetime, keyed by the `container` id the API returns.

Bash commands run in a per-container temporary directory with the real shell (timeouts, no network use in
practice because nothing is installed); files written under `outputs/` are registered with the Files API
and returned as `bash_code_execution_output` blocks, exactly like the API's output files.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import datetime as dt
import io
import json
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..config import runs_dir
from .files import get_file_store
from .render import next_id

CALLER_TYPE = "code_execution_20260120"
CELL_TIMEOUT_S = 20


class _Pause(Exception):
    """Raised inside a cell when it awaits a client tool whose result has not arrived yet."""


@dataclass
class Container:
    id: str
    created_at: float = field(default_factory=time.time)
    namespace: dict[str, Any] = field(default_factory=dict)        # REPL state persists across cells
    results: dict[str, list[str]] = field(default_factory=dict)    # (tool, args) key -> queued result texts
    pending: list[dict] = field(default_factory=list)              # calls the paused cell is waiting for
    paused_cell: dict | None = None                                # {"id", "code", "tools"} while paused
    workdir: Path | None = None

    @property
    def expires_at(self) -> str:
        return dt.datetime.fromtimestamp(self.created_at + 3600, dt.timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z")

    def info(self) -> dict:
        return {"id": self.id, "expires_at": self.expires_at}

    def dir(self) -> Path:
        if self.workdir is None:
            self.workdir = runs_dir("mock_containers", self.id)
            (self.workdir / "outputs").mkdir(exist_ok=True)
        return self.workdir


@dataclass
class CellOutcome:
    """Either a finished cell (stdout/stderr/return_code) or a pause with the calls the cell is waiting for."""
    paused: bool
    stdout: str = ""
    stderr: str = ""
    return_code: int = 0
    calls: list[dict] = field(default_factory=list)


def _result_key(name: str, args: Any) -> str:
    return name + ":" + json.dumps(args, sort_keys=True, default=str)


class Sandbox:
    def __init__(self) -> None:
        self._containers: dict[str, Container] = {}
        self._lock = threading.Lock()

    def container(self, container_id: str | None) -> Container:
        with self._lock:
            if container_id:
                if container_id not in self._containers:
                    from .errors import bad_request
                    raise bad_request(f"container: unknown or expired container id {container_id!r}; send the request "
                                      "again without the container parameter to get a new one")
                return self._containers[container_id]
            new = Container(id=next_id("container_mock_"))
            self._containers[new.id] = new
            return new

    def reset(self) -> None:
        with self._lock:
            for c in self._containers.values():
                if c.workdir and c.workdir.exists():
                    shutil.rmtree(c.workdir, ignore_errors=True)
            self._containers.clear()

    # ------------------------------------------------------------------ python cells (+ programmatic tool calling)
    def run_cell(self, container: Container, cell_id: str, code: str, tools: dict[str, dict]) -> CellOutcome:
        """Run a Python cell; `tools` maps the names callable from code to their definitions."""
        container.pending = []
        pending: list[dict] = []
        results = {k: list(v) for k, v in container.results.items()}   # consume a copy: a re-run replays them

        def make_tool(name: str):
            async def call(args: Any = None) -> str:
                key = _result_key(name, args or {})
                queue = results.get(key)
                if queue:
                    return queue.pop(0)
                pending.append({"name": name, "input": args or {}})
                raise _Pause(name)
            call.__name__ = name
            return call

        namespace = container.namespace
        namespace["__builtins__"] = __builtins__
        for name in tools:
            namespace[name] = make_tool(name)
        namespace.setdefault("json", json)
        namespace.setdefault("asyncio", asyncio)

        stdout, stderr = io.StringIO(), io.StringIO()
        return_code = 0
        paused = False
        cwd = Path.cwd()
        try:
            compiled = compile(code, f"<cell {cell_id}>", "exec", flags=ast.PyCF_ALLOW_TOP_LEVEL_AWAIT)
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                try:
                    import os
                    os.chdir(container.dir())
                    result = eval(compiled, namespace)
                    if asyncio.iscoroutine(result):
                        asyncio.run(asyncio.wait_for(result, timeout=CELL_TIMEOUT_S))
                finally:
                    os.chdir(cwd)
        except _Pause:
            paused = True
        except BaseException as exc:                     # a real error inside the cell: report it, don't raise
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            if _pause_inside(exc):
                paused = True
            else:
                stderr.write(f"{type(exc).__name__}: {exc}\n")
                return_code = 1
        if paused:
            container.pending = pending
            container.paused_cell = {"id": cell_id, "code": code, "tools": tools}
            calls = [{"name": p["name"], "input": p["input"]} for p in pending]
            return CellOutcome(paused=True, calls=calls)
        container.paused_cell = None
        container.namespace.pop("__builtins__", None)
        return CellOutcome(paused=False, stdout=stdout.getvalue(), stderr=stderr.getvalue(), return_code=return_code)

    def provide_results(self, container: Container, results: list[tuple[str, dict, str]]) -> None:
        """Record client tool results (name, input, text) so the paused cell can resume."""
        for name, args, text in results:
            container.results.setdefault(_result_key(name, args), []).append(text)

    def resume(self, container: Container) -> CellOutcome:
        cell = container.paused_cell
        if cell is None:
            return CellOutcome(paused=False, stderr="no paused cell in this container", return_code=1)
        outcome = self.run_cell(container, cell["id"], cell["code"], cell["tools"])
        if not outcome.paused:
            container.results.clear()
        return outcome

    # ------------------------------------------------------------------ bash and the text editor
    def run_bash(self, container: Container, command: str) -> dict:
        workdir = container.dir()
        before = {p for p in (workdir / "outputs").glob("*")}
        try:
            proc = subprocess.run(["bash", "-c", command], cwd=workdir, capture_output=True, text=True, timeout=30)
            stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
        except subprocess.TimeoutExpired:
            stdout, stderr, code = "", "command timed out after 30 s", 124
        outputs = []
        for path in sorted((workdir / "outputs").glob("*")):
            if path not in before and path.is_file():
                stored = get_file_store().add(path.name, path.read_bytes())
                outputs.append({"type": "bash_code_execution_output", "file_id": stored.id})
        return {"type": "bash_code_execution_result", "stdout": stdout[-20_000:], "stderr": stderr[-4_000:],
                "return_code": code, "content": outputs}

    def run_editor(self, container: Container, command: str, path: str, **kw: Any) -> dict:
        workdir = container.dir()
        target = (workdir / path.lstrip("/")).resolve() if not Path(path).is_absolute() or path.startswith("/") \
            else Path(path)
        target = workdir / Path(path).name if not str(target).startswith(str(workdir.resolve())) else target
        if command == "create":
            existed = target.exists()
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(kw.get("file_text", ""), encoding="utf-8")
            return {"type": "text_editor_code_execution_create_result", "is_file_update": existed}
        if command == "view":
            if not target.exists():
                return {"type": "text_editor_code_execution_tool_result_error", "error_code": "file_not_found",
                        "error_message": f"{path} does not exist"}
            text = target.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            return {"type": "text_editor_code_execution_view_result", "content": text, "file_type": "text",
                    "num_lines": len(lines), "start_line": 1, "total_lines": len(lines)}
        if command == "str_replace":
            if not target.exists():
                return {"type": "text_editor_code_execution_tool_result_error", "error_code": "file_not_found",
                        "error_message": f"{path} does not exist"}
            text = target.read_text(encoding="utf-8")
            old, new = kw.get("old_str", ""), kw.get("new_str", "")
            if old not in text:
                return {"type": "text_editor_code_execution_tool_result_error", "error_code": "invalid_tool_input",
                        "error_message": "old_str was not found in the file"}
            before = text[: text.index(old)].count("\n") + 1
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            return {"type": "text_editor_code_execution_str_replace_result", "lines": new.splitlines(),
                    "new_lines": len(new.splitlines()), "new_start": before,
                    "old_lines": len(old.splitlines()), "old_start": before}
        return {"type": "text_editor_code_execution_tool_result_error", "error_code": "invalid_tool_input",
                "error_message": f"unknown command {command!r}"}

    def mount_files(self, container: Container, file_ids: list[str]) -> None:
        """Make uploaded files available in the container (container_upload blocks)."""
        store = get_file_store()
        inputs = container.dir() / "inputs"
        inputs.mkdir(exist_ok=True)
        for file_id in file_ids:
            stored = store.get(file_id)
            (inputs / stored.filename).write_bytes(stored.content)


def _pause_inside(exc: BaseException) -> bool:
    """A pause raised inside asyncio.gather / a task surfaces wrapped; find it."""
    seen: set[int] = set()
    stack = [exc]
    while stack:
        e = stack.pop()
        if id(e) in seen:
            continue
        seen.add(id(e))
        if isinstance(e, _Pause):
            return True
        if isinstance(e, BaseExceptionGroup):
            stack.extend(e.exceptions)
        if e.__cause__:
            stack.append(e.__cause__)
        if e.__context__:
            stack.append(e.__context__)
    return False


_SANDBOX: Sandbox | None = None
_SANDBOX_LOCK = threading.Lock()


def get_sandbox() -> Sandbox:
    global _SANDBOX
    with _SANDBOX_LOCK:
        if _SANDBOX is None:
            _SANDBOX = Sandbox()
        return _SANDBOX
