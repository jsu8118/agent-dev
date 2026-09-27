"""Run every lab, exercise-starter and solution script in mock mode and require a clean exit.

This is the course's promise to learners: every script in the repo runs.  Scripts are
discovered automatically:

    day*/labs/*.py        day*/solutions/*.py        day7_capstone/**/run_*.py

Per-script directives (in the first 40 lines, as comments):

    # test: skip                      - not run automatically (e.g. an interactive REPL); say why next to it
    # test: args=--quick --limit 5    - extra command-line arguments for the test run
    # test: timeout=240               - seconds (default 120)
    # test: expect=Some text          - stdout must contain this text (may repeat)

Files whose name starts with "_" are helpers, not scripts, and are skipped.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PATTERNS = ["day*/labs/*.py", "day*/solutions/*.py", "day7_capstone/**/run_*.py"]


def _scripts() -> list[Path]:
    found: set[Path] = set()
    for pattern in PATTERNS:
        for path in ROOT.glob(pattern):
            if path.name.startswith("_") or "__pycache__" in path.parts:
                continue
            found.add(path)
    return sorted(found)


def _directives(path: Path) -> dict:
    out: dict = {"skip": False, "args": [], "timeout": 120, "expect": []}
    for line in path.read_text(encoding="utf-8").splitlines()[:40]:
        m = re.match(r"\s*#\s*test:\s*(\w+)(?:=(.*))?", line)
        if not m:
            continue
        key, value = m.group(1), (m.group(2) or "").strip()
        if key == "skip":
            out["skip"] = True
        elif key == "args":
            out["args"] = shlex.split(value)
        elif key == "timeout":
            out["timeout"] = int(value)
        elif key == "expect":
            out["expect"].append(value)
    return out


SCRIPTS = _scripts()


@pytest.mark.parametrize("script", SCRIPTS, ids=[str(p.relative_to(ROOT)) for p in SCRIPTS])
def test_script_runs_in_mock_mode(script: Path) -> None:
    directives = _directives(script)
    if directives["skip"]:
        pytest.skip("marked '# test: skip'")
    env = {**os.environ, "LABKIT_MODE": "mock", "LABKIT_QUIET": "0", "PYTHONIOENCODING": "utf-8"}
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    proc = subprocess.run([sys.executable, str(script), *directives["args"]], cwd=script.parent, env=env,
                          capture_output=True, text=True, timeout=directives["timeout"])
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"{script} exited with {proc.returncode}\n--- output ---\n{output[-6000:]}"
    for needle in directives["expect"]:
        assert needle in proc.stdout, f"{script}: expected output to contain {needle!r}\n{proc.stdout[-3000:]}"
