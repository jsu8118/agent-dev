"""Course-wide configuration, resolved from environment variables (and an optional .env file).

    LABKIT_MODE        auto | live | mock    (default: auto)
                       auto -> live when ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN is set, else mock
    LABKIT_MODEL       main model for the labs          (default: claude-opus-5)
    LABKIT_MID_MODEL   balanced model                   (default: claude-sonnet-5)
    LABKIT_FAST_MODEL  fast/cheap model (routing, judges, bulk extraction) (default: claude-haiku-4-5)
    LABKIT_QUIET       1 -> suppress banners and the end-of-run cost summary
    LABKIT_MOCK_STRICT 0 -> relax the mock API's request validation (default: strict)
"""

from __future__ import annotations

import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = REPO_ROOT / "data"
RUNS_DIR = REPO_ROOT / ".runs"          # scratch output: traces, copies of databases, memory files


def _load_dotenv(path: Path) -> None:
    """Tiny .env loader (KEY=VALUE lines). Existing environment variables win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and value and key not in os.environ:
            os.environ[key] = value


_load_dotenv(REPO_ROOT / ".env")

MODEL = os.environ.get("LABKIT_MODEL", "claude-opus-5")
MID_MODEL = os.environ.get("LABKIT_MID_MODEL", "claude-sonnet-5")
FAST_MODEL = os.environ.get("LABKIT_FAST_MODEL", "claude-haiku-4-5")


def mode() -> str:
    """Return "live" or "mock"."""
    requested = os.environ.get("LABKIT_MODE", "auto").strip().lower()
    if requested in ("live", "mock"):
        return requested
    has_credentials = bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))
    return "live" if has_credentials else "mock"


def is_mock() -> bool:
    return mode() == "mock"


def quiet() -> bool:
    return os.environ.get("LABKIT_QUIET", "0") == "1"


def runs_dir(*parts: str) -> Path:
    """A writable scratch directory under .runs/ (created on demand)."""
    path = RUNS_DIR.joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path
