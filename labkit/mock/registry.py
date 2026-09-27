"""Scenario registry: rule-based stand-in "models" for each lab.

A scenario = a `match(req)` predicate + a `respond(req) -> Reply` function.  Scenario
modules live in labkit/mock/scenarios/ and are imported automatically, so lab scripts
never contain mock-specific code: they just call the real SDK.

    from labkit.mock import scenario, say, use_tools, tool

    @scenario("day2.support", match=lambda r: r.has_tool("lookup_order"))
    def support(req):
        if not req.called("lookup_order"):
            return use_tools(tool("lookup_order", order_id="SO-10045"))
        return say("Your order shipped yesterday.")
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
import threading
from dataclasses import dataclass
from typing import Callable

from .generic import generic_reply
from .reply import Reply
from .request import MockRequest


@dataclass
class Scenario:
    name: str
    match: Callable[[MockRequest], bool]
    respond: Callable[[MockRequest], Reply]
    priority: int = 0


_SCENARIOS: list[Scenario] = []
_loaded = False
_load_lock = threading.Lock()


class ScenarioError(RuntimeError):
    pass


def scenario(name: str, *, match: Callable[[MockRequest], bool], priority: int = 0):
    """Register a scenario policy (decorator)."""
    def decorator(fn: Callable[[MockRequest], Reply]) -> Callable[[MockRequest], Reply]:
        _SCENARIOS[:] = [s for s in _SCENARIOS if s.name != name]
        _SCENARIOS.append(Scenario(name=name, match=match, respond=fn, priority=priority))
        return fn
    return decorator


def load_scenarios() -> None:
    global _loaded
    with _load_lock:
        if _loaded:
            return
        from . import scenarios as package
        for module in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
            try:
                importlib.import_module(f"{package.__name__}.{module.name}")
            except Exception as exc:  # one broken scenario file must not take down every other lab
                print(f"[labkit] WARNING: could not load mock scenario module {module.name!r}: {exc!r}",
                      file=sys.stderr)
        _loaded = True


def dispatch(req: MockRequest) -> tuple[str, Reply]:
    load_scenarios()
    for sc in sorted(_SCENARIOS, key=lambda s: -s.priority):
        try:
            matched = sc.match(req)
        except Exception as exc:  # a broken matcher is a bug in the course, surface it loudly
            raise ScenarioError(f"scenario {sc.name!r} match() crashed: {exc!r}") from exc
        if matched:
            try:
                return sc.name, sc.respond(req)
            except Exception as exc:
                raise ScenarioError(f"scenario {sc.name!r} respond() crashed: {exc!r}") from exc
    return "generic", generic_reply(req)


def registered() -> list[str]:
    load_scenarios()
    return [s.name for s in _SCENARIOS]
