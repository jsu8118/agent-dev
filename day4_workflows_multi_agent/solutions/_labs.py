"""Helper for the Day 4 solutions: make the lab modules importable (labs/ is not a package).

    import _labs
    lab04 = _labs.load("04_orchestrator_workers")      # a numbered lab module
    from _ap import three_way_match                     # helper modules import normally after this
"""

from __future__ import annotations

import importlib
import sys
from pathlib import Path
from types import ModuleType

LABS = Path(__file__).resolve().parents[1] / "labs"
if str(LABS) not in sys.path:
    sys.path.insert(0, str(LABS))


def load(name: str) -> ModuleType:
    """Import a lab script by file name (without .py). Its main() does not run on import."""
    return importlib.import_module(name)
