"""Lab 07 - Long-term memory with the memory tool: remember site knowledge across sessions, safely.

Objective
    Implement a file-backed memory for Claude's memory tool (a BetaAbstractMemoryTool subclass) that is scoped to
    one customer, confined to its directory, refuses customer personal data (Policy PRV-004 §3) and audits every
    operation. Session 1: a technician dictates site notes and preferences; Session 2, a brand-new conversation,
    recalls them; Session 3 shows that another customer's technician sees none of it. Finally, attack the guard
    directly (path traversal, encoded traversal, symlink escape, PII) and read the audit log.

Concepts
    conversation state vs long-term memory; the memory tool {"type": "memory_20250818", "name": "memory"}
    (client-side: you implement view/create/str_replace/insert/delete/rename on /memories paths); the SDK tool runner;
    per-tenant memory roots chosen by the harness, not the model; path confinement; data minimisation
    (preferences and working notes only); size caps; auditability; retention.

Run
    python day3_context_rag_memory/labs/07_memory_tool.py [--keep]    (--keep: don't wipe the demo memory first)

What to observe
    * The model checks /memories before doing anything else (the API adds that instruction automatically).
    * A write containing the site contact's name and phone number is rejected by the guard with an actionable error;
      the model retries without the personal data. In live mode Claude usually leaves PII out on its own - the guard
      exists for the times it doesn't, which is why it lives in code, not in the prompt.
    * Session 2 starts from an empty conversation and still knows the stop window, permit and spares.
    * The Cobalt Chemical session gets an empty memory: tenancy is enforced by the harness.
"""
# test: expect=Session 2
# test: expect=BLOCKED

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
from pathlib import Path
from typing import Callable, Iterable
from urllib.parse import unquote

from anthropic.lib.tools import BetaAbstractMemoryTool, ToolError
from anthropic.types.beta import (BetaMemoryTool20250818CreateCommand, BetaMemoryTool20250818DeleteCommand,
                                  BetaMemoryTool20250818InsertCommand, BetaMemoryTool20250818RenameCommand,
                                  BetaMemoryTool20250818StrReplaceCommand, BetaMemoryTool20250818ViewCommand)

from labkit import MODEL, get_client, header, is_mock, runs_dir, step, text_of, wrap
from labkit.data import ops_db

import _day3 as d3

MEMORY_SYSTEM = """\
<day3_field_memory>
You are Kestrel's field-service assistant for technicians. You have a memory directory for the customer site the \
technician is working at. Keep it useful: site access windows, permits, spares on site, equipment quirks, and the \
technician's working preferences. Never store customer personal data (names, phone numbers, email addresses) - \
Policy PRV-004 §3; note the preference itself instead. When you answer from memory, say so.
</day3_field_memory>"""

PII_PATTERNS = [
    ("email address", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")),
    ("phone number", re.compile(r"(?<![\w-])(?:\+\d{1,3}[ .-]?)?(?:\(\d{2,4}\)[ .-]?)?\d{3}[ .-]\d{4}(?![\w-])")),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){3,7}\b")),
    ("card number", re.compile(r"\b(?:\d[ -]?){13,19}\b")),
]


def known_person_names() -> list[str]:
    """Names of people in our systems of record: a dictionary check catches what regexes can't."""
    with ops_db() as db:
        rows = db.execute("SELECT contact_name, account_manager FROM customers").fetchall()
    return sorted({n for r in rows for n in (r[0], r[1]) if n})


class GuardedMemoryTool(BetaAbstractMemoryTool):
    """Memory for ONE customer: confined to its directory, PII-guarded, size-capped and audited."""

    MAX_FILE_CHARS = 4000
    MAX_VIEW_CHARS = 16000

    def __init__(self, customer_id: str, *, person_names: Iterable[str] = (),
                 on_event: Callable[[str], None] | None = None) -> None:
        super().__init__()
        if not re.fullmatch(r"C-\d{4}", customer_id):
            raise ValueError(f"invalid customer id {customer_id!r}")
        self.customer_id = customer_id
        self.root = runs_dir("day3_memories", customer_id).resolve()
        os.chmod(self.root, 0o700)
        self.audit_file = runs_dir("day3_memories", "_audit") / f"{customer_id}.jsonl"   # outside the model's reach
        self.person_names = [n for n in person_names if n]
        self.on_event = on_event

    # ---------------------------------------------------------------- safety rails
    def _audit(self, command: str, path: str, ok: bool, detail: str = "") -> None:
        record = {"ts": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "customer": self.customer_id,
                  "command": command, "path": path, "ok": ok, "detail": detail}   # never the content itself
        with self.audit_file.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
        if self.on_event:
            self.on_event(f"memory.{command} {path} -> {'ok' if ok else 'BLOCKED: ' + detail}")

    def _resolve(self, path: str, command: str) -> Path:
        reason = None
        if not isinstance(path, str) or not (path == "/memories" or path.startswith("/memories/")):
            reason = f"Path must start with /memories, got: {path!r}"
        elif unquote(path) != path or "\\" in path or "\x00" in path or ".." in path.split("/"):
            reason = f"Path {path!r} contains traversal or encoded sequences"
        else:
            target = (self.root / path[len("/memories"):].lstrip("/")).resolve()   # resolve() follows symlinks
            try:
                target.relative_to(self.root)
                return target
            except ValueError:
                reason = f"Path {path!r} would escape the memory directory"
        self._audit(command, str(path), False, "path rejected")
        raise ToolError(reason)

    def _write(self, target: Path, text: str) -> None:
        """Owner-only permissions: memory files are data about customers' sites, not world-readable scratch."""
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        target.write_text(text, encoding="utf-8")
        os.chmod(target, 0o600)

    def _check_content(self, text: str, command: str, path: str) -> None:
        findings = [label for label, rx in PII_PATTERNS if rx.search(text or "")]
        flat = re.sub(r"[_\-.]+", " ", text or "").lower()          # "Jane_Doe.md" must not slip through
        findings += [f"person name '{n}'" for n in self.person_names if n.lower() in flat]
        if findings:
            self._audit(command, path, False, "personal data: " + ", ".join(f.split(" '")[0] for f in findings))
            raise ToolError("Rejected: long-term memory may not contain customer personal data (Policy PRV-004 §3). "
                            f"Found: {', '.join(findings)}. Store the preference or working note without the "
                            "personal details (they stay in the CRM).")
        if len(text or "") > self.MAX_FILE_CHARS:
            self._audit(command, path, False, "too large")
            raise ToolError(f"Rejected: memory files are capped at {self.MAX_FILE_CHARS} characters; summarise.")

    # ---------------------------------------------------------------- commands
    def view(self, command: BetaMemoryTool20250818ViewCommand) -> str:
        target = self._resolve(command.path, "view")
        if not target.exists():
            self._audit("view", command.path, False, "missing")
            raise ToolError(f"The path {command.path} does not exist. Please provide a valid path.")
        if target.is_dir():
            lines = [f"{_size(target)}\t{command.path}"]
            for item in sorted(target.rglob("*")):
                rel = item.relative_to(target)
                if len(rel.parts) > 2 or any(p.startswith(".") for p in rel.parts):
                    continue
                lines.append(f"{_size(item)}\t{command.path.rstrip('/')}/{rel.as_posix()}{'/' if item.is_dir() else ''}")
            self._audit("view", command.path, True, "directory")
            return (f"Here're the files and directories up to 2 levels deep in {command.path}, excluding hidden items "
                    "and node_modules:\n" + "\n".join(lines))
        content = target.read_text(encoding="utf-8")
        lines = content.split("\n")
        start = 1
        if command.view_range and len(command.view_range) == 2:
            start = max(1, command.view_range[0])
            end = len(lines) if command.view_range[1] == -1 else command.view_range[1]
            lines = lines[start - 1:end]
        body = "\n".join(f"{i:>6}\t{line}" for i, line in enumerate(lines, start))
        if len(body) > self.MAX_VIEW_CHARS:
            body = body[:self.MAX_VIEW_CHARS] + "\n... (truncated; use view_range)"
        self._audit("view", command.path, True, "file")
        return f"Here's the content of {command.path} with line numbers:\n{body}"

    def create(self, command: BetaMemoryTool20250818CreateCommand) -> str:
        target = self._resolve(command.path, "create")
        self._check_content(command.file_text, "create", command.path)
        existed = target.exists()
        self._write(target, command.file_text)
        self._audit("create", command.path, True, "overwrote" if existed else "created")
        return f"File created successfully at: {command.path}"

    def str_replace(self, command: BetaMemoryTool20250818StrReplaceCommand) -> str:
        target = self._resolve(command.path, "str_replace")
        if not target.is_file():
            raise ToolError(f"Error: The path {command.path} does not exist. Please provide a valid path.")
        new_str = command.new_str or ""
        self._check_content(new_str, "str_replace", command.path)
        content = target.read_text(encoding="utf-8")
        count = content.count(command.old_str)
        if count == 0:
            raise ToolError(f"No replacement was performed, old_str `{command.old_str}` did not appear verbatim in "
                            f"{command.path}.")
        if count > 1:
            raise ToolError(f"No replacement was performed. Multiple occurrences of old_str `{command.old_str}`. "
                            "Please ensure it is unique")
        updated = content.replace(command.old_str, new_str)
        self._check_content(updated, "str_replace", command.path)
        self._write(target, updated)
        self._audit("str_replace", command.path, True)
        return "The memory file has been edited."

    def insert(self, command: BetaMemoryTool20250818InsertCommand) -> str:
        target = self._resolve(command.path, "insert")
        if not target.is_file():
            raise ToolError(f"Error: The path {command.path} does not exist")
        self._check_content(command.insert_text, "insert", command.path)
        lines = target.read_text(encoding="utf-8").split("\n")
        if not 0 <= command.insert_line <= len(lines):
            raise ToolError(f"Error: Invalid `insert_line` parameter: {command.insert_line}. It should be within the "
                            f"range of lines of the file: [0, {len(lines)}]")
        lines.insert(command.insert_line, command.insert_text.rstrip("\n"))
        updated = "\n".join(lines)
        self._check_content(updated, "insert", command.path)
        self._write(target, updated)
        self._audit("insert", command.path, True)
        return f"The file {command.path} has been edited."

    def delete(self, command: BetaMemoryTool20250818DeleteCommand) -> str:
        target = self._resolve(command.path, "delete")
        if target == self.root:
            raise ToolError("Cannot delete the /memories directory itself")
        if not target.exists():
            raise ToolError(f"Error: The path {command.path} does not exist")
        shutil.rmtree(target) if target.is_dir() else target.unlink()
        self._audit("delete", command.path, True)
        return f"Successfully deleted {command.path}"

    def rename(self, command: BetaMemoryTool20250818RenameCommand) -> str:
        source = self._resolve(command.old_path, "rename")
        dest = self._resolve(command.new_path, "rename")
        # names leak through file names too - and the audit log must not become a PII store, so redact the path
        self._check_content(command.new_path, "rename", "[new path redacted]")
        if source == self.root:
            raise ToolError("Cannot rename the /memories directory itself")
        if not source.exists():
            raise ToolError(f"Error: The path {command.old_path} does not exist")
        if dest.exists():
            raise ToolError(f"Error: The destination {command.new_path} already exists")
        dest.parent.mkdir(parents=True, exist_ok=True)
        source.rename(dest)
        self._audit("rename", f"{command.old_path} -> {command.new_path}", True)
        return f"Successfully renamed {command.old_path} to {command.new_path}"

    # ---------------------------------------------------------------- governance helpers
    def purge_older_than(self, days: int) -> list[str]:
        """Retention: delete memory files not modified for `days` days (run from a scheduled job)."""
        cutoff = dt.datetime.now().timestamp() - days * 86400
        removed = []
        for f in self.root.rglob("*"):
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
                removed.append(f.relative_to(self.root).as_posix())
        return removed


def _size(p: Path) -> str:
    n = p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    return f"{n / 1024:.1f}K" if n >= 1024 else f"{n}B"


def run_session(client, memory: GuardedMemoryTool, text: str) -> str:
    runner = client.beta.messages.tool_runner(model=MODEL, max_tokens=4000, system=MEMORY_SYSTEM, tools=[memory],
                                              messages=[{"role": "user", "content": text}], max_iterations=10)
    final = None
    for message in runner:          # each yielded message is one model turn; the runner executes the tool calls
        final = message
    return text_of(final) if final else ""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--keep", action="store_true", help="keep memories from earlier runs")
    args = parser.parse_args()
    client = get_client()
    header("Lab 07 - Long-term memory with the memory tool")
    if is_mock():
        print("(mock mode: the stand-in model deliberately dictates the contact's details into its first memory write, "
              "to exercise the guard; live Claude usually won't - the guard must work either way)")

    with ops_db() as db:
        site = db.execute("SELECT customer_id, name, contact_name, contact_email, phone FROM customers "
                          "WHERE name LIKE 'Harbor Foods%'").fetchone()
        other = db.execute("SELECT customer_id, name FROM customers WHERE name LIKE 'Cobalt Chemical%'").fetchone()
    pump = next(a for a in d3.assets().values() if a["customer_id"] == site["customer_id"] and "Chilled" in a["notes"])
    names = known_person_names()
    events: list[str] = []
    memory = GuardedMemoryTool(site["customer_id"], person_names=names, on_event=lambda e: print("   ", e))
    if not args.keep:
        shutil.rmtree(memory.root, ignore_errors=True)
        memory.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        memory.audit_file.unlink(missing_ok=True)
    print(f"Memory root for {site['customer_id']} ({site['name']}): {memory.root}")

    step(1, "Session 1 - the technician dictates site notes")
    dictation = (f"I'm finishing up at {pump['site']} today - please remember a few things for next time. "
                 f"{pump['asset_id']}, the chilled-water pump, can only be stopped on Sundays 06:00-10:00. "
                 "The site requires a hot-work permit from the shift supervisor before any grinding. "
                 "We keep two BRG-6309 bearings in the site store. "
                 f"The plant contact, {site['contact_name']} ({site['phone']}), wants an SMS before we arrive. "
                 "I prefer torque values in N·m and short checklists.")
    print(wrap("Technician: " + dictation))
    reply = run_session(client, memory, dictation)
    print("Assistant:\n" + wrap(reply))

    step(2, "Session 2 - a brand-new conversation recalls the notes")
    question = (f"Back at {pump['site']} next week to replace the bearings on {pump['asset_id']}. When can I take it "
                "down, and what should I sort out before I go?")
    print(wrap("Technician: " + question))
    reply = run_session(client, memory, question)
    print("Assistant:\n" + wrap(reply))

    step(3, f"Session 3 - a technician at {other['name']} gets that customer's (empty) memory")
    other_memory = GuardedMemoryTool(other["customer_id"], person_names=names, on_event=lambda e: print("   ", e))
    if not args.keep:
        shutil.rmtree(other_memory.root, ignore_errors=True)
        other_memory.root.mkdir(parents=True, exist_ok=True, mode=0o700)
    reply = run_session(client, other_memory, "When can I take the chilled-water pump down, and which permits do I need?")
    print("Assistant:\n" + wrap(reply))

    step(4, "What is stored, and the guard under attack")
    for f in sorted(memory.root.rglob("*.md")):
        print(f"--- /memories/{f.relative_to(memory.root).as_posix()} ({f.stat().st_size} bytes)")
        print(wrap(f.read_text(encoding="utf-8")))
    outside = runs_dir("day3_memories", "_outside")
    (outside / "secrets.env").write_text("API_KEY=do-not-leak\n", encoding="utf-8")
    link = memory.root / "shortcut"
    try:
        if not link.exists():
            os.symlink(outside, link, target_is_directory=True)
    except OSError:
        link = None                                   # e.g. Windows without symlink privilege
    attacks = [
        ("view /memories/../../_outside/secrets.env", lambda: memory.view(
            BetaMemoryTool20250818ViewCommand(command="view", path="/memories/../../_outside/secrets.env"))),
        ("view /memories/%2e%2e/%2e%2e/_outside/secrets.env", lambda: memory.view(
            BetaMemoryTool20250818ViewCommand(command="view", path="/memories/%2e%2e/%2e%2e/_outside/secrets.env"))),
        ("create /etc/cron.d/job", lambda: memory.create(
            BetaMemoryTool20250818CreateCommand(command="create", path="/etc/cron.d/job", file_text="x"))),
        ("create /memories/contacts.md with an email address", lambda: memory.create(
            BetaMemoryTool20250818CreateCommand(command="create", path="/memories/contacts.md",
                                                file_text=f"Site contact: {site['contact_email']}"))),
        ("rename a note to /memories/<contact name>.md", lambda: memory.rename(BetaMemoryTool20250818RenameCommand(
            command="rename", old_path="/memories/site_notes.md",
            new_path=f"/memories/{site['contact_name'].replace(' ', '_')}.md"))),
    ]
    if link is not None:
        attacks.append(("view /memories/shortcut/secrets.env (symlink out)", lambda: memory.view(
            BetaMemoryTool20250818ViewCommand(command="view", path="/memories/shortcut/secrets.env"))))
    rows = []
    for label, attempt in attacks:
        try:
            attempt()
            rows.append([label, "ALLOWED (bug!)"])
        except ToolError as exc:
            rows.append([label, "BLOCKED - " + str(exc)[:70]])
    d3.table(rows, ["attempt", "result"])
    if link is not None:
        link.unlink()

    step(5, "Governance: audit log and retention")
    lines = memory.audit_file.read_text(encoding="utf-8").splitlines()
    print(f"{len(lines)} audited operations for {site['customer_id']} (content is never logged); last five:")
    for line in lines[-5:]:
        print("   ", line)
    print(f"Retention job (delete notes untouched for 365 days) removed: {memory.purge_older_than(365) or 'nothing'}")


if __name__ == "__main__":
    main()
