"""Shared helpers for the Day 2 labs (a helper, not a lab: files starting with "_" are not run by the tests).

What lives here and why:
* the tool catalog - loading `advanced/data/tools/catalog.json`, turning entries into API-ready definitions
  (loaded or deferred, strict or not, with or without `input_examples`), the always-loaded core set, and the three
  tools the labs add outside the catalog (`get_shipment_v2`, `get_recall_status`, `draft_bulletin`);
* `count_prompt` - prompt tokens of a request shape via `count_tokens`, with the documented fallback for endpoints
  that reject server tools;
* a small backend for the catalog - `KestrelOps.run(name, input)` answers the tools the labs call from the ops
  database, the recall files and deterministic synthetic telemetry, with one error envelope for every failure;
* the labelled task sets the labs score - the discovery set (lab 02), the wide-agent tasks (lab 03), the phased
  conversation (lab 04), the recall units (lab 05), the bulletin facts (lab 06) and the 30-task selection eval with
  its two description sets (lab 07);
* one agent loop (`run_agent`) that every lab measures with, so token and cache numbers are comparable;
* printing and pricing helpers.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable

import anthropic
import jsonschema

from labkit import DATA_DIR, MODEL, REPO_ROOT, text_of
from labkit.data import ops_db
from labkit.models import get_spec
from labkit.pricing import _get, cost_usd

DAY_DIR = Path(__file__).resolve().parents[1]
ADV_DATA = REPO_ROOT / "advanced" / "data"
TODAY = "2026-09-15"

# ------------------------------------------------------------------------------------ server tools and betas
SEARCH_REGEX = {"type": "tool_search_tool_regex_20251119", "name": "tool_search_tool_regex"}
SEARCH_BM25 = {"type": "tool_search_tool_bm25_20251119", "name": "tool_search_tool_bm25"}
CODE_EXECUTION = {"type": "code_execution_20260120", "name": "code_execution"}
CODE_CALLER = "code_execution_20260120"
TOOL_CHANGES_BETA = "mid-conversation-tool-changes-2026-07-01"
INLINE_TOOLS_BETA = "inline-tools-2026-09-15"

# Markers at the top of each lab's system prompt: they let the offline mock recognise the lab; Claude ignores them.
MARK_SEARCH = "<adv_day2_tool_search>"
MARK_WIDE = "<adv_day2_wide_agent>"
MARK_PHASES = "<adv_day2_phases>"
MARK_PTC = "<adv_day2_ptc>"
MARK_STREAM = "<adv_day2_stream>"
MARK_SELECT = "<adv_day2_selection>"


# ------------------------------------------------------------------------------------ the catalog
@lru_cache(maxsize=1)
def catalog() -> dict:
    return json.loads((ADV_DATA / "tools" / "catalog.json").read_text(encoding="utf-8"))


def catalog_tools() -> list[dict]:
    return catalog()["tools"]


def all_names() -> list[str]:
    return [t["name"] for t in catalog_tools()]


@lru_cache(maxsize=1)
def _by_name() -> dict[str, dict]:
    return {t["name"]: t for t in catalog_tools()}


def entry(name: str) -> dict:
    return _by_name()[name] if name in _by_name() else EXTRA_TOOLS[name]


def meta(name: str) -> dict:
    return entry(name)["meta"]


def core_names() -> list[str]:
    return [t["name"] for t in catalog_tools() if t["meta"]["core"]]


def domain_names() -> list[str]:
    return [d["name"] for d in catalog()["domains"]]


def domain_tools(domain: str) -> list[str]:
    return next(d["tools"] for d in catalog()["domains"] if d["name"] == domain)


def api_tool(tool_entry: dict, *, defer: bool = False, strict: bool = False, examples: bool = True,
             description: str | None = None, **extra: Any) -> dict:
    """An API-ready tool definition (the catalog's `meta` block is ours, not the API's)."""
    out: dict = {"name": tool_entry["name"], "description": description or tool_entry["description"],
                 "input_schema": copy.deepcopy(tool_entry["input_schema"])}
    if examples and tool_entry.get("input_examples"):
        out["input_examples"] = copy.deepcopy(tool_entry["input_examples"])
    if strict:
        out["strict"] = True
    if defer:
        out["defer_loading"] = True
    out.update(extra)
    return out


def loaded_toolset(names: Iterable[str], **kw: Any) -> list[dict]:
    """Every named tool, loaded (no deferral), in the order given."""
    return [api_tool(entry(n), **kw) for n in names]


def wide_toolset(search: dict | None = SEARCH_BM25, loaded: Iterable[str] | None = None,
                 names: Iterable[str] | None = None, **kw: Any) -> list[dict]:
    """The wide toolset: a search tool, the always-loaded core, and the rest of the catalog deferred (catalog order,
    so the array is byte-identical on every request)."""
    loaded_set = set(core_names() if loaded is None else loaded)
    tools = [dict(search)] if search else []
    for name in (names or all_names()):
        tools.append(api_tool(entry(name), defer=(name not in loaded_set) and search is not None, **kw))
    return tools


def domain_toolset(domain: str, **kw: Any) -> list[dict]:
    """A hand-picked route: the core tools plus one domain, all loaded."""
    names = list(dict.fromkeys(core_names() + domain_tools(domain)))
    return loaded_toolset(names, **kw)


# ------------------------------------------------------------------------------------ tools outside the catalog
def _schema(props: dict, required: list[str]) -> dict:
    return {"type": "object", "properties": props, "required": required, "additionalProperties": False}


EXTRA_TOOLS: dict[str, dict] = {
    # lab 07: the versioned successor of get_shipment (a new name, not an edited definition)
    "get_shipment_v2": {
        "name": "get_shipment_v2",
        "description": "Return one shipment's booking record by shipment ID (SH-50283): carrier, tracking number, ship date, "
                       "promised ETA, delivered date and any exception reason; with include_events=true also the carrier's "
                       "scan events. Use for 'which carrier', 'what ETA did we promise', 'what exception is on it'.",
        "input_schema": _schema({"shipment_id": {"type": "string", "description": "Shipment ID, e.g. SH-50210."},
                                 "include_events": {"type": "boolean", "description": "Also return carrier scan events."}},
                                ["shipment_id"]),
        "meta": {"domain": "logistics", "risk": "read", "side_effect": False, "pii": False, "approval_required": False,
                 "core": False},
    },
    # lab 04: a tool the recall team shipped after the conversation started (defined inline, by value)
    "get_recall_status": {
        "name": "get_recall_status",
        "description": "Recall campaign status of one unit under RC-2026-03: whether it is affected, its remedy, whether a "
                       "remedy visit is already scheduled or done, and the contact deadline for its risk class. Use for "
                       "'is this unit scheduled under the recall', 'what is the recall status of'.",
        "input_schema": _schema({"serial_number": {"type": "string", "description": "Unit serial number, e.g. KC2-2608-0001."}},
                                ["serial_number"]),
        "meta": {"domain": "quality", "risk": "read", "side_effect": False, "pii": False, "approval_required": False,
                 "core": False},
    },
    # lab 06: a client tool whose input is a long document, streamed with eager_input_streaming
    "draft_bulletin": {
        "name": "draft_bulletin",
        "description": "Save a draft technical safety bulletin for review by the quality lead. Use it when asked to draft or "
                       "write a bulletin. Put the complete plain-text bulletin in `body` and the operator actions in "
                       "`actions`. Drafts are not sent to anyone.",
        "input_schema": _schema({
            "bulletin_id": {"type": "string", "pattern": r"^TSB-\d{4}-\d{2}$", "description": "Bulletin ID, e.g. TSB-2026-09."},
            "title": {"type": "string", "description": "One-line title."},
            "audience": {"type": "string", "enum": ["operators", "installers", "distributors"],
                         "description": "Who the bulletin is written for."},
            "lots": {"type": "array", "minItems": 1, "items": {"type": "string", "pattern": r"^[A-Z]{2}-\d{4}-[A-Z]$"},
                     "description": "Affected manufacturing lots, e.g. PS-2608-B."},
            "body": {"type": "string", "minLength": 800, "maxLength": 3000,
                     "description": "The full bulletin text, 800-3,000 characters."},
            "actions": {"type": "array", "minItems": 3, "items": {"type": "string"},
                        "description": "Actions operators must take now, one per item."}},
            ["bulletin_id", "title", "audience", "lots", "body", "actions"]),
        "meta": {"domain": "communications", "risk": "write", "side_effect": True, "pii": False, "approval_required": False,
                 "core": False},
    },
}


# ------------------------------------------------------------------------------------ counting prompts
def count_prompt(client: Any, tools: list[dict], messages: list[dict], *, system: Any = None, model: str = MODEL) -> int:
    """Prompt tokens of a request shape. `count_tokens` is free and exact; where the endpoint rejects a request because
    of a server tool (the tool search or code execution tool), fall back to a `max_tokens=1` request and read what it
    billed - a paid call of a few thousand input tokens."""
    kw: dict = {"model": model, "messages": messages}
    if tools:
        kw["tools"] = tools
    if system:
        kw["system"] = system
    try:
        return client.messages.count_tokens(**kw).input_tokens
    except anthropic.BadRequestError:
        if not any(t.get("type") for t in tools or []):
            raise
        r = client.messages.create(max_tokens=1, **kw)
        return prompt_size(r.usage)


def cached_system(text: str) -> list[dict]:
    """A system prompt with a cache breakpoint at its end: requests that share tools + system (an eval's tasks, a
    batch of discovery queries) read that prefix from the cache instead of paying for it on every call."""
    return [{"type": "text", "text": text, "cache_control": {"type": "ephemeral"}}]


def token_cost(client: Any, text: str, model: str = MODEL) -> int:
    """Tokens a piece of text adds to a user message (count_tokens difference)."""
    base = client.messages.count_tokens(model=model, messages=[{"role": "user", "content": "x"}]).input_tokens
    return client.messages.count_tokens(model=model, messages=[{"role": "user", "content": "x\n" + text}]).input_tokens - base


# ------------------------------------------------------------------------------------ pricing and printing
def input_price(model: str = MODEL) -> float:
    return get_spec(model).input_price / 1_000_000


def output_price(model: str = MODEL) -> float:
    return get_spec(model).output_price / 1_000_000


def money(x: float) -> str:
    return f"${x:,.4f}" if abs(x) < 1 else f"${x:,.2f}"


def prompt_size(usage: Any) -> int:
    return int(_get(usage, "input_tokens") + _get(usage, "cache_creation_input_tokens") + _get(usage, "cache_read_input_tokens"))


def response_cost(response: Any) -> float:
    return cost_usd(response.usage, response.model)


def usage_row(label: str, response: Any) -> list:
    u = response.usage
    return [label, u.input_tokens, u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0,
            u.output_tokens, money(response_cost(response))]


USAGE_HEADERS = ["request", "uncached in", "cache write", "cache read", "out", "cost"]


def _cell(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, int):
        return f"{v:,}"
    if isinstance(v, float):
        return f"{v:,.3f}" if abs(v) < 100 else f"{v:,.1f}"
    return str(v)


def table(rows: list[list], headers: list[str], indent: str = "  ") -> None:
    cells = [[_cell(v) for v in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    numeric = [all(re.fullmatch(r"[-+$\d,.%x]+", r[i]) for r in cells) if cells else False for i in range(len(headers))]

    def fmt(row: list[str]) -> str:
        return (indent + "  ".join((c.rjust(w) if numeric[i] else c.ljust(w))
                                   for i, (c, w) in enumerate(zip(row, widths)))).rstrip()

    print(fmt(headers))
    print((indent + "  ".join(("-" if h else " ") * w for h, w in zip(headers, widths))).rstrip())
    for r in cells:
        print(fmt(r))


def clip(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."


def blocks_of(response: Any) -> list[str]:
    """Compact rendering of a response's content blocks, e.g. ['thinking', 'server_tool_use:tool_search_tool_bm25', ...]."""
    out = []
    for b in response.content:
        kind = b.type
        if kind in ("tool_use", "server_tool_use"):
            kind += ":" + b.name
            if getattr(b, "caller", None) is not None and getattr(b.caller, "type", "direct") != "direct":
                kind += "(from code)"
        elif kind == "tool_search_tool_result":
            kind += f"({len(getattr(b.content, 'tool_references', []) or [])} refs)"
        out.append(kind)
    return out


def search_input(block: Any) -> str:
    """The search a tool-search server_tool_use block carries: `pattern` for the regex variant, `query` for BM25."""
    data = block.input or {}
    return str(data.get("query") or data.get("pattern") or "")


# ------------------------------------------------------------------------------------ the backend
class ToolError(Exception):
    def __init__(self, code: str, message: str, next_action: str | None = None) -> None:
        super().__init__(message)
        self.code, self.next_action = code, next_action


def _seed(key: str) -> int:
    return int(hashlib.sha256(key.encode()).hexdigest()[:8], 16)


def _load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


FAULT_CODES = {
    "F17": {"meaning": "DC-bus undervoltage: the power stage lost supply margin during a dip.",
            "likely_causes": ["undersized bulk capacitor on power-stage boards from lot VD-2607-C",
                              "supply sag or brown-out on the incoming feed"],
            "recommended_action": "Enable auto-restart after F17 (manual section 9.1) where the process allows; replace the "
                                  "power-stage board (KC-2-PSB, firmware 2.4.1) under recall RC-2026-03.", "families": ["KC-2"]},
    "F05": {"meaning": "Seal-chamber over-temperature.", "likely_causes": ["seal weeping", "low flow", "blocked flush line"],
            "recommended_action": "Check flush line and flow; inspect seal; on lot PS-2608-B units treat as recall symptom.",
            "families": ["KC-1", "KC-2"]},
    "F03": {"meaning": "Dry-run protection tripped.", "likely_causes": ["suction loss", "closed valve"],
            "recommended_action": "Restore suction, reset the controller, inspect the seal for damage.", "families": ["KC-1", "KC-2"]},
    "E42": {"meaning": "Telemetry link lost for more than 10 minutes.", "likely_causes": ["network outage", "gateway reboot"],
            "recommended_action": "Check the site gateway; the pump keeps running on local control.", "families": ["KC-2"]},
}


DEFAULT_NEXT_ACTION = {
    "not_found": "check the ID with the user; do not guess another",
    "unknown_tool": "use one of your available tools",
    "invalid_input": "fix the arguments and call again",
}


class KestrelOps:
    """A small backend for the catalog: reads from the ops database and the recall files, deterministic synthetic
    telemetry, in-memory writes. `run(name, input)` returns (tool_result content, is_error) like kestrel.SupportDesk.

    `allowed` (a set of tool names) makes it refuse everything else - the executor-side half of a scoped toolset;
    `retired` ({old: new}) makes a retired tool answer with an error that names its successor."""

    def __init__(self, *, allowed: Iterable[str] | None = None, retired: dict[str, str] | None = None) -> None:
        self.allowed = set(allowed) if allowed is not None else None
        self.retired = dict(retired or {})
        self.drafts: list[dict] = []
        self.db = ops_db()
        self.calls: list[dict] = []
        self.tickets: dict[str, dict] = {"SVC-4021": {"ticket_id": "SVC-4021", "customer_id": "C-1005", "site_id": "SITE-1005-A",
                                                      "priority": "P2", "status": "open", "serial_number": "KP250-2608-0004",
                                                      "description": "Seal weeping on KP250-2608-0004 (recall RC-2026-03)",
                                                      "engineer_id": None, "history": [{"at": "2026-09-14", "event": "opened"}]}}
        self.visits: list[dict] = []
        self.audit: list[dict] = []
        self._next = {"SVC": 4030, "ESC": 4101, "APR": 501, "TASK": 7781, "RSV": 20081, "RMA": 7023, "VIS": 9001, "MSG": 1}
        self.approved: set[str] = set()

    # -- plumbing ------------------------------------------------------------------------------------------------
    def _id(self, prefix: str) -> str:
        n = self._next[prefix]
        self._next[prefix] += 1
        return f"{prefix}-{n}"

    def _q(self, sql: str, *args: Any) -> list[dict]:
        return [dict(r) for r in self.db.execute(sql, args).fetchall()]

    def _one(self, sql: str, *args: Any) -> dict | None:
        rows = self._q(sql, *args)
        return rows[0] if rows else None

    def run(self, name: str, tool_input: dict | None) -> tuple[str, bool]:
        tool_input = dict(tool_input or {})
        handler = getattr(self, f"t_{name}", None)
        try:
            if name not in _by_name() and name not in EXTRA_TOOLS:
                raise ToolError("unknown_tool", f"{name!r} is not a Kestrel tool.")
            if self.allowed is not None and name not in self.allowed:
                raise ToolError("not_available", f"{name} is not available in this session.",
                                "use one of your available tools, or escalate_to_human")
            if name in self.retired:
                raise ToolError("retired", f"{name} has been retired; use {self.retired[name]} with the same "
                                "arguments.", self.retired[name])
            schema = entry(name)["input_schema"]
            errors = sorted(jsonschema.Draft202012Validator(schema).iter_errors(tool_input), key=lambda e: list(e.path))
            if errors:
                raise ToolError("invalid_input", f"{name}: " + "; ".join(e.message for e in errors[:3]),
                                "fix the arguments and call again")
            if handler is None:
                raise ToolError("not_implemented", f"{name} exists in the catalog but is not wired to a backend in this lab.",
                                "escalate_to_human")
            result = handler(**tool_input)
            content, is_error = json.dumps(result, default=str), False
        except ToolError as exc:
            envelope: dict = {"error": {"code": exc.code, "message": str(exc)}}
            next_action = exc.next_action or DEFAULT_NEXT_ACTION.get(exc.code)
            if next_action:
                envelope["error"]["next_action"] = next_action
            content, is_error = json.dumps(envelope), True
        except TypeError as exc:
            content, is_error = json.dumps({"error": {"code": "invalid_input", "message": f"{name}: {exc}",
                                                      "next_action": DEFAULT_NEXT_ACTION["invalid_input"]}}), True
        self.calls.append({"name": name, "input": tool_input, "is_error": is_error})
        return content, is_error

    # -- orders ---------------------------------------------------------------------------------------------------
    def _order(self, order_id: str) -> dict:
        row = self._one("SELECT * FROM orders WHERE order_id = ?", order_id.upper())
        if row is None:
            raise ToolError("not_found", f"Order {order_id} not found. Order IDs look like SO-10248; do not guess another number.",
                            "ask for the correct order ID")
        return row

    def t_get_order(self, order_id: str) -> dict:
        o = self._order(order_id)
        lines = self._q("SELECT sku, qty, unit_price_usd, configured, special_order FROM order_lines WHERE order_id = ? ORDER BY line_no", o["order_id"])
        ship = self._q("SELECT shipment_id, status FROM shipments WHERE order_id = ?", o["order_id"])
        inv = self._one("SELECT invoice_id FROM invoices WHERE order_id = ?", o["order_id"])
        return {"order_id": o["order_id"], "customer_id": o["customer_id"], "status": o["status"], "order_date": o["order_date"],
                "promised_date": o["promised_date"], "customer_po": o["customer_po"], "total_usd": o["total_usd"],
                "lines": lines, "shipments": ship, "invoice_id": inv["invoice_id"] if inv else None}

    def t_get_order_lines(self, order_id: str) -> dict:
        o = self._order(order_id)
        return {"order_id": o["order_id"], "lines": self._q("SELECT line_no, sku, qty, unit_price_usd, line_total_usd, configured, "
                                                           "special_order FROM order_lines WHERE order_id = ? ORDER BY line_no", o["order_id"])}

    def t_get_order_status_history(self, order_id: str) -> dict:
        o = self._order(order_id)
        ship = self._one("SELECT * FROM shipments WHERE order_id = ? ORDER BY ship_date", o["order_id"])
        events = [{"at": o["order_date"], "status": "confirmed", "actor": "order-portal"}]
        if ship:
            events.append({"at": ship["ship_date"], "status": "shipped", "actor": f"{ship['warehouse']} dispatch"})
            if ship["delivered_date"]:
                events.append({"at": ship["delivered_date"], "status": "delivered", "actor": ship["carrier"]})
        if o["status"] in ("cancelled", "on_hold"):
            events.append({"at": TODAY, "status": o["status"], "actor": "support"})
        return {"order_id": o["order_id"], "events": events}

    def t_list_orders(self, customer_id: str, status: str | None = None, limit: int | None = 10) -> dict:
        rows = self._q("SELECT order_id, status, order_date, promised_date, total_usd FROM orders WHERE customer_id = ? "
                       + ("AND status = ? " if status else "") + "ORDER BY order_date DESC LIMIT ?",
                       *([customer_id, status, limit or 10] if status else [customer_id, limit or 10]))
        return {"customer_id": customer_id, "orders": rows}

    def t_search_orders(self, query: str, limit: int | None = 10) -> dict:
        rows = self._q("SELECT order_id, customer_id, status, customer_po FROM orders WHERE customer_po LIKE ? OR order_id LIKE ? "
                       "LIMIT ?", f"%{query}%", f"%{query}%", limit or 10)
        return {"query": query, "orders": rows}

    def t_update_order_notes(self, order_id: str, note: str) -> dict:
        o = self._order(order_id)
        self.audit.append({"action": "update_order_notes", "order_id": o["order_id"], "note": note[:200]})
        return {"order_id": o["order_id"], "note_added": True}

    # -- logistics ------------------------------------------------------------------------------------------------
    def _shipment_view(self, s: dict) -> dict:
        return {"shipment_id": s["shipment_id"], "order_id": s["order_id"], "warehouse": s["warehouse"], "carrier": s["carrier"],
                "tracking_number": s["tracking_number"], "ship_date": s["ship_date"], "eta_date": s["eta_date"],
                "delivered_date": s["delivered_date"], "status": s["status"], "exception_reason": s["exception_reason"]}

    def t_get_shipment(self, shipment_id: str) -> dict:
        s = self._one("SELECT * FROM shipments WHERE shipment_id = ?", shipment_id.upper())
        if s is None:
            raise ToolError("not_found", f"Shipment {shipment_id} not found (IDs look like SH-50210).")
        return self._shipment_view(s)

    def t_get_shipment_v2(self, shipment_id: str, include_events: bool | None = False) -> dict:
        view = self.t_get_shipment(shipment_id)
        if include_events:
            view["events"] = self.t_track_shipment(view["tracking_number"])["events"]
        return view

    def t_list_shipments_for_order(self, order_id: str) -> dict:
        o = self._order(order_id)
        rows = self._q("SELECT * FROM shipments WHERE order_id = ? ORDER BY ship_date", o["order_id"])
        return {"order_id": o["order_id"], "shipments": [self._shipment_view(s) for s in rows]}

    def t_track_shipment(self, tracking_number: str) -> dict:
        s = self._one("SELECT * FROM shipments WHERE tracking_number = ?", tracking_number.upper())
        if s is None:
            raise ToolError("not_found", f"No shipment carries tracking number {tracking_number}. Get the number from "
                            "list_shipments_for_order or get_shipment; do not guess.")
        ship = dt.date.fromisoformat(s["ship_date"])
        today = dt.date.fromisoformat(TODAY)
        hubs = ["origin depot", "regional hub", "destination hub", "local delivery station"]
        events = [{"at": f"{ship}T09:10:00Z", "event": "picked up", "location": f"{s['warehouse']} dock"}]
        last_day = dt.date.fromisoformat(s["delivered_date"]) if s["delivered_date"] else min(today, ship + dt.timedelta(days=4))
        day, i = ship + dt.timedelta(days=1), 0
        while day < last_day and i < len(hubs):
            events.append({"at": f"{day}T{7 + 3 * i:02d}:40:00Z", "event": "in transit", "location": hubs[i]})
            day, i = day + dt.timedelta(days=1), i + 1
        if s["delivered_date"]:
            events.append({"at": f"{s['delivered_date']}T14:05:00Z", "event": "delivered", "location": "consignee dock, signed"})
            status = "delivered"
        elif s["status"] == "exception":
            events.append({"at": f"{last_day}T08:00:00Z", "event": "exception", "location": hubs[min(i, 3)],
                           "detail": s["exception_reason"]})
            status = "exception"
        else:
            status = "in_transit"
        return {"tracking_number": s["tracking_number"], "carrier": s["carrier"], "status": status, "eta_date": s["eta_date"],
                "last_scan": events[-1], "events": events}

    def t_get_customs_status(self, shipment_id: str) -> dict:
        s = self.t_get_shipment(shipment_id)
        o = self._order(s["order_id"])
        if o["ship_to_country"] == "US":
            return {"shipment_id": s["shipment_id"], "international": False, "status": "not applicable (domestic)"}
        return {"shipment_id": s["shipment_id"], "international": True, "status": "cleared" if s["delivered_date"] else "in clearance",
                "duties_usd": round(o["total_usd"] * 0.04, 2)}

    def t_get_carrier_rates(self, warehouse: str, country: str, weight_kg: float, service: str | None = "standard") -> dict:
        base = {"standard": 1.0, "express": 1.9, "freight": 0.7}.get(service or "standard", 1.0)
        intl = 1.0 if country.upper() in ("US", "CA") else 1.6
        return {"lane": f"{warehouse} -> {country}", "service": service or "standard", "quotes": [
            {"carrier": "NorthLine Freight", "usd": round((95 + 0.9 * weight_kg) * base * intl, 2), "days": 3 if service == "express" else 5},
            {"carrier": "BlueRiver Logistics", "usd": round((110 + 0.8 * weight_kg) * base * intl, 2), "days": 2 if service == "express" else 4}]}

    def t_schedule_pickup(self, location: str, date: str, pieces: int) -> dict:
        self.audit.append({"action": "schedule_pickup", "location": location, "date": date})
        return {"pickup_id": f"PU-{_seed(location + date) % 9000 + 1000}", "location": location, "date": date, "pieces": pieces,
                "carrier": "NorthLine Freight", "window": "08:00-12:00"}

    # -- billing --------------------------------------------------------------------------------------------------
    def _invoice(self, invoice_id: str) -> dict:
        row = self._one("SELECT * FROM invoices WHERE invoice_id = ?", invoice_id.upper())
        if row is None:
            raise ToolError("not_found", f"Invoice {invoice_id} not found (IDs look like AR-90210).")
        return row

    def t_get_invoice(self, invoice_id: str) -> dict:
        i = self._invoice(invoice_id)
        return {"invoice_id": i["invoice_id"], "order_id": i["order_id"], "customer_id": i["customer_id"], "amount_usd": i["amount_usd"],
                "paid_amount_usd": i["paid_amount_usd"], "open_amount_usd": round(i["amount_usd"] - i["paid_amount_usd"], 2),
                "due_date": i["due_date"], "status": i["status"], "days_past_due": max(0, (dt.date.fromisoformat(TODAY) -
                                                                                          dt.date.fromisoformat(i["due_date"])).days) if i["status"] != "paid" else 0}

    def t_list_invoices(self, customer_id: str, status: str | None = None, limit: int | None = 10) -> dict:
        rows = self._q("SELECT invoice_id, order_id, amount_usd, paid_amount_usd, due_date, status FROM invoices WHERE customer_id = ? "
                       + ("AND status = ? " if status else "") + "ORDER BY due_date DESC LIMIT ?",
                       *([customer_id, status, limit or 10] if status else [customer_id, limit or 10]))
        return {"customer_id": customer_id, "invoices": rows}

    def t_get_account_balance(self, customer_id: str) -> dict:
        rows = self._q("SELECT amount_usd, paid_amount_usd, due_date, status FROM invoices WHERE customer_id = ? AND status != 'paid'", customer_id)
        overdue = [r for r in rows if r["due_date"] < TODAY]
        return {"customer_id": customer_id, "outstanding_usd": round(sum(r["amount_usd"] - r["paid_amount_usd"] for r in rows), 2),
                "overdue_usd": round(sum(r["amount_usd"] - r["paid_amount_usd"] for r in overdue), 2),
                "days_past_due": max([(dt.date.fromisoformat(TODAY) - dt.date.fromisoformat(r["due_date"])).days for r in overdue] or [0]),
                "open_invoices": len(rows)}

    def t_get_credit_limit(self, customer_id: str) -> dict:
        c = self._customer(customer_id)
        used = self.t_get_account_balance(customer_id)["outstanding_usd"]
        return {"customer_id": customer_id, "credit_limit_usd": c["credit_limit_usd"], "used_usd": used,
                "available_usd": round(c["credit_limit_usd"] - used, 2)}

    def t_get_payment_terms(self, customer_id: str) -> dict:
        c = self._customer(customer_id)
        return {"customer_id": customer_id, "net_days": {"strategic": 45, "key": 30}.get(c["tier"], 30), "early_payment_discount_pct": 1.0}

    def t_record_payment(self, invoice_id: str, amount_usd: float, reference: str) -> dict:
        i = self._invoice(invoice_id)
        self.audit.append({"action": "record_payment", "invoice_id": i["invoice_id"], "amount_usd": amount_usd})
        return {"invoice_id": i["invoice_id"], "recorded_usd": amount_usd, "reference": reference,
                "open_amount_usd": round(i["amount_usd"] - i["paid_amount_usd"] - amount_usd, 2)}

    def t_apply_late_fee_waiver(self, invoice_id: str, reason: str) -> dict:
        i = self._invoice(invoice_id)
        self.audit.append({"action": "apply_late_fee_waiver", "invoice_id": i["invoice_id"], "reason": reason[:200]})
        return {"invoice_id": i["invoice_id"], "late_fees_waived_usd": round(i["amount_usd"] * 0.015, 2), "reason": reason[:200]}

    def _needs_approval(self, action: str, role: str, amount: float | None = None) -> None:
        if action not in self.approved:
            detail = f" ({money(amount)})" if amount is not None else ""
            raise ToolError("approval_required", f"{action}{detail} needs approval by {role} before it runs. The action was NOT performed.",
                            f"request_approval(approver_role='{role}') and wait, or escalate_to_human")

    def t_issue_credit_note(self, invoice_id: str, amount_usd: float, reason: str) -> dict:
        i = self._invoice(invoice_id)
        self._needs_approval("issue_credit_note", "support_manager", amount_usd)
        return {"credit_note_id": f"CN-{_seed(invoice_id) % 9000 + 1000}", "invoice_id": i["invoice_id"], "amount_usd": amount_usd}

    def t_issue_refund(self, order_id: str, amount_usd: float, reason: str, rma_id: str | None = None) -> dict:
        o = self._order(order_id)
        if amount_usd > 2_500:                            # the agent's limit; above it a person decides
            self._needs_approval("issue_refund", "finance_director" if amount_usd > 10_000 else "support_manager", amount_usd)
        return {"refund_id": f"RF-{_seed(order_id) % 9000 + 1000}", "order_id": o["order_id"], "amount_usd": amount_usd}

    # -- returns --------------------------------------------------------------------------------------------------
    def t_get_rma(self, rma_id: str) -> dict:
        r = self._one("SELECT * FROM rmas WHERE rma_id = ?", rma_id.upper())
        if r is None:
            raise ToolError("not_found", f"RMA {rma_id} not found (IDs look like RMA-7001).")
        return {k: r[k] for k in ("rma_id", "order_id", "sku", "qty", "reason_code", "status", "refund_due_usd", "notes")}

    def t_check_return_eligibility(self, order_id: str, sku: str, qty: int, reason_code: str) -> dict:
        o = self._order(order_id)
        line = self._one("SELECT * FROM order_lines WHERE order_id = ? AND sku = ?", o["order_id"], sku.upper())
        if line is None:
            raise ToolError("not_found", f"{sku} is not a line of {o['order_id']}.", "check the SKU with get_order")
        product = self._one("SELECT returnable, product_line FROM products WHERE sku = ?", sku.upper()) or {"returnable": 1}
        ship = self._one("SELECT delivered_date FROM shipments WHERE order_id = ?", o["order_id"])
        delivered = ship["delivered_date"] if ship else None
        days = (dt.date.fromisoformat(TODAY) - dt.date.fromisoformat(delivered)).days if delivered else None
        eligible = bool(product["returnable"]) and not line["configured"] and not line["special_order"] and days is not None and days <= 30
        fee_pct = 15 if reason_code == "no_longer_needed" else 0
        return {"order_id": o["order_id"], "sku": sku.upper(), "qty": qty, "delivered_date": delivered, "days_since_delivery": days,
                "eligible": eligible, "reason_code": reason_code, "restocking_fee_pct": fee_pct if eligible else None,
                "refund_after_fee_usd": round(line["unit_price_usd"] * qty * (1 - fee_pct / 100), 2) if eligible else 0.0,
                "exclusions": [x for x, on in (("configured", line["configured"]), ("special_order", line["special_order"]),
                                               ("outside_30_day_window", days is not None and days > 30)) if on]}

    def t_get_restocking_fee(self, sku: str, qty: int, reason_code: str) -> dict:
        p = self._one("SELECT list_price_usd FROM products WHERE sku = ?", sku.upper())
        pct = 15 if reason_code == "no_longer_needed" else 0
        return {"sku": sku.upper(), "qty": qty, "restocking_fee_pct": pct, "fee_usd": round((p["list_price_usd"] if p else 0) * qty * pct / 100, 2)}

    def t_create_rma(self, order_id: str, sku: str, qty: int, reason_code: str, note: str | None = None) -> dict:
        check = self.t_check_return_eligibility(order_id, sku, qty, reason_code)
        if not check["eligible"]:
            raise ToolError("not_eligible", f"{sku} on {order_id} is not returnable: {', '.join(check['exclusions']) or 'policy'}.",
                            "explain the policy; escalate_to_human for exceptions")
        rma = self._id("RMA")
        self.audit.append({"action": "create_rma", "rma_id": rma, "order_id": order_id})
        return {"rma_id": rma, "order_id": order_id.upper(), "sku": sku.upper(), "qty": qty, "reason_code": reason_code, "status": "approved",
                "restocking_fee_pct": check["restocking_fee_pct"]}

    def t_get_rma_inspection(self, rma_id: str) -> dict:
        r = self.t_get_rma(rma_id)
        return {"rma_id": r["rma_id"], "received": r["status"] in ("received", "inspected", "closed"),
                "findings": "unit unused, packaging intact" if r["status"] == "received" else "not yet received"}

    # -- field service --------------------------------------------------------------------------------------------
    @lru_cache(maxsize=1)
    def _engineers(self) -> list[dict]:
        return _load(ADV_DATA / "recall" / "engineers.json")["engineers"]

    def _build(self, serial_number: str) -> dict:
        row = self._one("SELECT * FROM build_records WHERE serial_number = ?", serial_number.upper())
        if row is None:
            raise ToolError("not_found", f"Serial {serial_number} has no build record (serials look like KP250-2608-0002).")
        return row

    def t_check_warranty(self, serial_number: str | None = None, sku: str | None = None, ship_date: str | None = None) -> dict:
        if serial_number:
            b = self._build(serial_number)
            sku = b["sku"]
            ship = self._one("SELECT ship_date FROM shipments WHERE order_id = ?", b["order_id"])
            ship_date = ship["ship_date"] if ship else b["build_date"]
            order = self._one("SELECT customer_id FROM orders WHERE order_id = ?", b["order_id"])
            tier = (self._customer(order["customer_id"])["tier"] if order else "standard")
        elif sku and ship_date:
            tier = "standard"
        else:
            raise ToolError("invalid_input", "Give a serial_number, or a sku together with its ship_date.")
        p = self._one("SELECT warranty_months FROM products WHERE sku = ?", sku.upper()) or {"warranty_months": 12}
        start = dt.date.fromisoformat(ship_date)
        end = dt.date(start.year + (start.month - 1 + p["warranty_months"]) // 12, (start.month - 1 + p["warranty_months"]) % 12 + 1, min(start.day, 28))
        return {"serial_number": serial_number, "sku": sku.upper(), "warranty_start": str(start), "warranty_end": str(end),
                "covered": end >= dt.date.fromisoformat(TODAY), "advance_replacement": tier == "strategic",
                "note": "coverage is subject to inspection; recall RC-2026-03 remedies are free regardless of warranty"}

    def t_register_warranty(self, serial_number: str, site_id: str, date: str) -> dict:
        b = self._build(serial_number)
        self.audit.append({"action": "register_warranty", "serial_number": b["serial_number"], "site_id": site_id, "date": date})
        return {"serial_number": b["serial_number"], "site_id": site_id, "installed": date, "warranty_registered": True}

    def t_create_service_ticket(self, customer_id: str, site_id: str, priority: str, description: str, serial_number: str | None = None) -> dict:
        tid = self._id("SVC")
        self.tickets[tid] = {"ticket_id": tid, "customer_id": customer_id, "site_id": site_id, "priority": priority, "status": "open",
                             "serial_number": serial_number, "description": description[:300], "engineer_id": None,
                             "history": [{"at": TODAY, "event": "opened"}]}
        self.audit.append({"action": "create_service_ticket", "ticket_id": tid})
        return {"ticket_id": tid, "status": "open", "priority": priority, "site_id": site_id, "customer_id": customer_id}

    def t_get_service_ticket(self, ticket_id: str) -> dict:
        t = self.tickets.get(ticket_id.upper())
        if t is None:
            raise ToolError("not_found", f"Ticket {ticket_id} not found (IDs look like SVC-4021).")
        return t

    def t_list_service_tickets(self, customer_id: str | None = None, site_id: str | None = None, status: str | None = None) -> dict:
        rows = [t for t in self.tickets.values() if (not customer_id or t["customer_id"] == customer_id)
                and (not site_id or t["site_id"] == site_id) and (not status or t["status"] == status)]
        return {"tickets": [{k: t[k] for k in ("ticket_id", "customer_id", "site_id", "priority", "status")} for t in rows]}

    def t_get_engineer_availability(self, region: str, from_date: str, to_date: str, skill: str | None = None) -> dict:
        out = []
        for e in self._engineers():
            if e["region"] != region or (skill and skill not in e["skills"]):
                continue
            free = [{"slot_start": s["start"], "slot_end": s["end"]} for s in e["slots"]
                    if s["status"] == "free" and from_date <= s["start"][:10] <= to_date]
            if free:
                out.append({"engineer_id": e["engineer_id"], "name": e["name"], "skills": e["skills"], "home_base": e["home_base"],
                            "free_slots": free[:3], "free_slot_count": len(free)})
        return {"region": region, "from_date": from_date, "to_date": to_date, "skill": skill, "engineers": out}

    def t_schedule_field_visit(self, ticket_id: str, engineer_id: str, slot_start: str) -> dict:
        t = self.t_get_service_ticket(ticket_id)
        eng = next((e for e in self._engineers() if e["engineer_id"] == engineer_id), None)
        if eng is None:
            raise ToolError("not_found", f"Engineer {engineer_id} not found (IDs look like FSE-03).")
        slot = next((s for s in eng["slots"] if s["start"] == slot_start), None)
        if slot is None or slot["status"] != "free" or any(v["slot_start"] == slot_start and v["engineer_id"] == engineer_id for v in self.visits):
            raise ToolError("slot_unavailable", f"{engineer_id} has no free slot starting {slot_start}.",
                            "pick another slot from get_engineer_availability")
        visit = {"visit_id": self._id("VIS"), "ticket_id": t["ticket_id"], "engineer_id": engineer_id, "engineer": eng["name"],
                 "slot_start": slot_start, "site_id": t["site_id"]}
        self.visits.append(visit)
        t["engineer_id"], t["status"] = engineer_id, "scheduled"
        t["history"].append({"at": TODAY, "event": f"visit {visit['visit_id']} booked"})
        self.audit.append({"action": "schedule_field_visit", **{k: visit[k] for k in ("visit_id", "ticket_id", "engineer_id")}})
        return visit

    def t_assign_engineer(self, ticket_id: str, engineer_id: str) -> dict:
        t = self.t_get_service_ticket(ticket_id)
        t["engineer_id"] = engineer_id
        return {"ticket_id": t["ticket_id"], "engineer_id": engineer_id}

    def t_get_service_history(self, serial_number: str) -> dict:
        b = self._build(serial_number)
        n = _seed(serial_number) % 3
        visits = [{"date": f"2026-0{3 + i}-1{i}", "work": ["alignment check", "seal flush line cleaned", "firmware 2.3.0 loaded"][i],
                   "parts_used": [[], ["MS-250"], []][i]} for i in range(n)]
        return {"serial_number": b["serial_number"], "sku": b["sku"], "visits": visits}

    # -- inventory ------------------------------------------------------------------------------------------------
    @lru_cache(maxsize=1)
    def _kits(self) -> dict[str, dict]:
        return {k["sku"]: k for k in _load(ADV_DATA / "recall" / "parts.json")["kits"]}

    def t_get_stock(self, sku: str, warehouse: str | None = None) -> dict:
        sku = sku.upper()
        if sku in self._kits():
            rows = [{"warehouse": w, "on_hand": n, "reserved": 0, "available": n} for w, n in self._kits()[sku]["stock"].items()]
        else:
            rows = [{"warehouse": r["warehouse"], "on_hand": r["on_hand"], "reserved": r["reserved"], "available": r["on_hand"] - r["reserved"]}
                    for r in self._q("SELECT * FROM inventory WHERE sku = ?", sku)]
        if not rows:
            raise ToolError("not_found", f"No inventory rows for SKU {sku}.")
        if warehouse:
            rows = [r for r in rows if r["warehouse"] == warehouse]
        return {"sku": sku, "stock": rows}

    def t_get_lead_time(self, sku: str) -> dict:
        sku = sku.upper()
        if sku in self._kits():
            return {"sku": sku, "lead_time_days": self._kits()[sku]["lead_time_days"], "source": "supplier"}
        p = self._one("SELECT lead_time_days FROM products WHERE sku = ?", sku)
        if p is None:
            raise ToolError("not_found", f"SKU {sku} not found.")
        return {"sku": sku, "lead_time_days": p["lead_time_days"], "source": "plant"}

    def t_reserve_stock(self, sku: str, warehouse: str, qty: int, reference: str) -> dict:
        rid = self._id("RSV")
        self.audit.append({"action": "reserve_stock", "reservation_id": rid, "sku": sku, "qty": qty})
        return {"reservation_id": rid, "sku": sku.upper(), "warehouse": warehouse, "qty": qty, "reference": reference}

    def t_list_warehouses(self) -> dict:
        return {"warehouses": [{"warehouse": "WH-EAST", "region": "US-EAST"}, {"warehouse": "WH-WEST", "region": "US-WEST"},
                               {"warehouse": "WH-EU", "region": "EU"}]}

    # -- products -------------------------------------------------------------------------------------------------
    def t_get_product(self, sku: str) -> dict:
        p = self._one("SELECT * FROM products WHERE sku = ?", sku.upper())
        if p is None:
            raise ToolError("not_found", f"SKU {sku} not found (SKUs look like KP-250-S).")
        return {k: p[k] for k in ("sku", "name", "product_line", "family", "list_price_usd", "warranty_months", "lead_time_days",
                                  "configurable", "returnable")}

    def t_get_bom(self, sku: str) -> dict:
        p = self.t_get_product(sku)
        fam = p["family"]
        parts = {"KP-250": [("MS-250", "seal cartridge", 1), ("BRG-6310", "bearing set", 2), ("IMP-250", "impeller", 1)],
                 "KP-100": [("MS-100", "seal cartridge", 1), ("BRG-6205", "bearing set", 2)],
                 "KC-2": [("KC-2-PSB", "power-stage board", 1), ("KC-2-CPU", "controller board", 1), ("FAN-92", "fan", 2)]}.get(fam, [])
        return {"sku": p["sku"], "family": fam, "parts": [{"sku": s, "name": n, "qty": q} for s, n, q in parts]}

    def t_get_datasheet(self, sku: str) -> dict:
        p = self.t_get_product(sku)
        ratings = {"KP-250": {"flow_m3h": 120, "head_m": 45, "power_kw": 22}, "KP-100": {"flow_m3h": 40, "head_m": 32, "power_kw": 5.5},
                   "KP-400": {"flow_m3h": 400, "head_m": 60, "power_kw": 90}}.get(p["family"], {})
        return {"sku": p["sku"], "datasheet_url": f"https://docs.kestrel-pumps.example/datasheets/{p['sku']}.pdf", "ratings": ratings}

    def t_get_compatible_parts(self, sku: str | None = None, serial_number: str | None = None) -> dict:
        if serial_number:
            sku = self._build(serial_number)["sku"]
        return {"sku": sku, "compatible": self.t_get_bom(sku)["parts"]}

    # -- quality --------------------------------------------------------------------------------------------------
    def t_get_build_record(self, serial_number: str) -> dict:
        b = self._build(serial_number)
        return {k: b[k] for k in ("serial_number", "sku", "order_id", "build_date", "plant", "seal_lot", "board_lot", "test_result")}

    def t_list_units_by_lot(self, lot: str) -> dict:
        rows = self._q("SELECT b.serial_number, b.sku, b.order_id, b.build_date, b.plant, o.customer_id, c.name AS customer, "
                       "s.delivered_date FROM build_records b JOIN orders o ON o.order_id = b.order_id JOIN customers c ON "
                       "c.customer_id = o.customer_id LEFT JOIN shipments s ON s.order_id = o.order_id WHERE b.seal_lot = ? OR "
                       "b.board_lot = ? ORDER BY b.serial_number", lot.upper(), lot.upper())
        if not rows:
            raise ToolError("not_found", f"Lot {lot} has no build records (lots look like PS-2608-B or VD-2607-C).")
        return {"lot": lot.upper(), "count": len(rows), "units": rows}

    def t_get_lot_summary(self, lot: str) -> dict:
        units = self.t_list_units_by_lot(lot)["units"]
        return {"lot": lot.upper(), "units_built": len(units), "plants": sorted({u["plant"] for u in units}),
                "build_dates": [min(u["build_date"] for u in units), max(u["build_date"] for u in units)],
                "customers": len({u["customer_id"] for u in units}), "open_incidents": 1, "recall": "RC-2026-03"}

    def t_get_test_results(self, serial_number: str) -> dict:
        b = self._build(serial_number)
        s = _seed(serial_number)
        return {"serial_number": b["serial_number"], "test_date": b["build_date"], "result": b["test_result"],
                "pressure_bar": round(10 + s % 5 / 10, 1), "vibration_mm_s": round(1.2 + (s >> 3) % 9 / 10, 1), "leak": "none"}

    def t_open_quality_hold(self, reason: str, lot: str | None = None, sku: str | None = None) -> dict:
        self._needs_approval("open_quality_hold", "quality_lead")
        return {"hold_id": "QH-2026-015", "lot": lot, "sku": sku, "reason": reason[:200]}

    # -- customers ------------------------------------------------------------------------------------------------
    @lru_cache(maxsize=1)
    def _contacts(self) -> dict[str, dict]:
        return {c["customer_id"]: c for c in _load(ADV_DATA / "recall" / "contacts.json")["contacts"]}

    def _customer(self, customer_id: str) -> dict:
        row = self._one("SELECT * FROM customers WHERE customer_id = ?", customer_id.upper())
        if row is None:
            raise ToolError("not_found", f"Customer {customer_id} not found (IDs look like C-1005).")
        return row

    def t_get_customer(self, customer_id: str) -> dict:
        c = self._customer(customer_id)
        return {k: c[k] for k in ("customer_id", "name", "industry", "tier", "country", "account_manager", "credit_limit_usd")}

    def t_search_customers(self, query: str, limit: int | None = 10) -> dict:
        rows = self._q("SELECT customer_id, name, industry, tier FROM customers WHERE name LIKE ? OR email_domain LIKE ? OR industry LIKE ? LIMIT ?",
                       f"%{query}%", f"%{query}%", f"%{query}%", limit or 10)
        return {"query": query, "customers": rows}

    def t_list_contacts(self, customer_id: str) -> dict:
        c = self._customer(customer_id)
        rc = self._contacts().get(c["customer_id"])
        if rc:
            contacts = [dict(rc["primary"], customer_id=c["customer_id"]), dict(rc["site"], customer_id=c["customer_id"])]
        else:
            contacts = [{"contact_id": f"CT-{c['customer_id'][2:]}-1", "name": c["contact_name"], "email": c["contact_email"],
                         "phone": c["phone"], "role": "primary"}]
        return {"customer_id": c["customer_id"], "contacts": contacts}

    def t_get_account_manager(self, customer_id: str) -> dict:
        c = self._customer(customer_id)
        name = c["account_manager"]
        return {"customer_id": c["customer_id"], "account_manager": name,
                "email": name.lower().replace(" ", ".") + "@kestrel-pumps.example"}

    def t_get_customer_tier(self, customer_id: str) -> dict:
        c = self._customer(customer_id)
        ent = {"strategic": "advance warranty replacement, 4h P1 response, named engineer",
               "key": "next-business-day P1 response, priority spares", "standard": "standard SLA"}
        return {"customer_id": c["customer_id"], "tier": c["tier"], "entitlements": ent.get(c["tier"], "standard SLA")}

    def t_get_sla_terms(self, customer_id: str) -> dict:
        c = self._customer(customer_id)
        hours = {"strategic": {"P1": 4, "P2": 8, "P3": 24}, "key": {"P1": 8, "P2": 24, "P3": 48}}.get(c["tier"], {"P1": 24, "P2": 48, "P3": 72})
        return {"customer_id": c["customer_id"], "tier": c["tier"], "response_hours": hours, "on_site_hours": "08:00-18:00 local"}

    def t_list_customer_sites(self, customer_id: str) -> dict:
        c = self._customer(customer_id)
        rc = self._contacts().get(c["customer_id"])
        site_id = rc["site"]["site_id"] if rc else f"SITE-{c['customer_id'][2:]}-A"
        return {"customer_id": c["customer_id"], "sites": [{"site_id": site_id, "name": f"{c['name']} main plant", "country": c["country"],
                                                            "timezone": rc["timezone"] if rc else "America/New_York"}]}

    def t_get_site(self, site_id: str) -> dict:
        cid = "C-" + site_id.split("-")[1] if site_id.upper().startswith("SITE-") else site_id
        c = self._customer(cid)
        units = self._q("SELECT b.serial_number, b.sku FROM build_records b JOIN orders o ON o.order_id = b.order_id WHERE o.customer_id = ?", c["customer_id"])
        rc = self._contacts().get(c["customer_id"])
        return {"site_id": site_id.upper(), "customer_id": c["customer_id"], "name": f"{c['name']} main plant", "country": c["country"],
                "contact": rc["site"]["name"] if rc else c["contact_name"], "installed_units": units[:12],
                "access": "badge at gate 2; PPE required in pump hall"}

    # -- fleet (deterministic synthetic telemetry) ------------------------------------------------------------------
    def _telemetry(self, serial_number: str) -> dict:
        b = self._build(serial_number)
        s = _seed(serial_number)
        nominal = {"KP-250": (120, 45, 22.0), "KP-100": (40, 32, 5.5), "KP-400": (400, 60, 90.0)}
        fam = b["sku"].rsplit("-", 1)[0] if b["sku"].startswith("KP") else b["sku"]
        flow, head, power = nominal.get(fam, (0, 0, 0.8))
        return {"serial_number": b["serial_number"], "sku": b["sku"], "as_of": f"{TODAY}T08:00:00Z", "running": True,
                "flow_m3h": round(flow * (0.9 + (s % 20) / 100), 1), "head_m": round(head * (0.95 + (s % 7) / 100), 1),
                "power_kw": round(power * (0.9 + (s % 13) / 100), 1), "vibration_mm_s": round(1.8 + ((s >> 4) % 40) / 10, 1),
                "seal_temp_c": 56 + s % 32}

    def t_get_pump_telemetry(self, serial_number: str) -> dict:
        return self._telemetry(serial_number)

    def t_get_vibration_trend(self, serial_number: str, days: int) -> dict:
        t = self._telemetry(serial_number)
        s = _seed("trend:" + serial_number)
        slope = round(((s % 9) - 3) / 20, 3)          # -0.15 .. +0.25 mm/s per day
        series = [round(max(0.5, t["vibration_mm_s"] - slope * (days - 1 - i) + ((s >> (i % 8)) % 3 - 1) / 20), 2) for i in range(days)]
        return {"serial_number": t["serial_number"], "days": days, "rms_mm_s": series, "slope_per_day": slope,
                "assessment": "rising" if slope > 0.05 else "falling" if slope < -0.05 else "stable"}

    def t_get_runtime_hours(self, serial_number: str) -> dict:
        b = self._build(serial_number)
        s = _seed("hours:" + serial_number)
        return {"serial_number": b["serial_number"], "total_hours": 400 + s % 900, "since_last_service_hours": 100 + s % 300}

    def t_get_fault_codes(self, serial_number: str, days: int) -> dict:
        b = self._build(serial_number)
        codes = []
        if b["board_lot"] == "VD-2607-C":
            for d in range(min(days, 7)):
                day = dt.date.fromisoformat(TODAY) - dt.timedelta(days=d)
                codes += [{"at": f"{day}T13:2{d}:00Z", "code": "F17", "detail": "DC bus 412 V < 430 V threshold"},
                          {"at": f"{day}T04:0{d}:00Z", "code": "F17", "detail": "DC bus 408 V < 430 V threshold"}]
        elif b["seal_lot"] == "PS-2608-B" and _seed(serial_number) % 2:
            codes.append({"at": f"{TODAY}T06:15:00Z", "code": "F05", "detail": "seal chamber 79 C"})
        return {"serial_number": b["serial_number"], "days": days, "count": len(codes), "codes": codes}

    def t_decode_fault_code(self, fault_code: str, family: str | None = None) -> dict:
        info = FAULT_CODES.get(fault_code.upper())
        if info is None:
            raise ToolError("not_found", f"Fault code {fault_code} is not in the KC-1/KC-2 code table (codes look like F17 or E42).")
        return {"fault_code": fault_code.upper(), "family": family or "/".join(info["families"]), **info}

    def t_list_fleet_alerts(self, customer_id: str | None = None, site_id: str | None = None, severity: str | None = None) -> dict:
        alerts = [{"alert_id": "ALR-88012", "serial_number": "KC2-2608-0001", "site_id": "SITE-1012-A", "customer_id": "C-1012",
                   "severity": "critical", "summary": "F17 x14 in 7 days"},
                  {"alert_id": "ALR-88017", "serial_number": "KP250-2608-0008", "site_id": "SITE-1014-A", "customer_id": "C-1014",
                   "severity": "warning", "summary": "seal temperature 81 C"}]
        return {"alerts": [a for a in alerts if (not customer_id or a["customer_id"] == customer_id) and (not site_id or a["site_id"] == site_id)
                           and (not severity or a["severity"] == severity)]}

    def t_get_alert(self, alert_id: str) -> dict:
        a = next((x for x in self.t_list_fleet_alerts()["alerts"] if x["alert_id"] == alert_id.upper()), None)
        if a is None:
            raise ToolError("not_found", f"Alert {alert_id} not found (IDs look like ALR-88012).")
        return dict(a, readings=self._telemetry(a["serial_number"]))

    def t_acknowledge_alert(self, alert_id: str, note: str) -> dict:
        a = self.t_get_alert(alert_id)
        self.audit.append({"action": "acknowledge_alert", "alert_id": a["alert_id"]})
        return {"alert_id": a["alert_id"], "acknowledged": True, "note": note[:200]}

    def t_get_site_health(self, site_id: str) -> dict:
        site = self.t_get_site(site_id)
        units = [self._telemetry(u["serial_number"]) for u in site["installed_units"][:6]]
        return {"site_id": site["site_id"], "units": len(units), "open_alerts": len(self.t_list_fleet_alerts(site_id=site["site_id"])["alerts"]),
                "hottest_seal_c": max((u["seal_temp_c"] for u in units), default=None),
                "worst_vibration_mm_s": max((u["vibration_mm_s"] for u in units), default=None)}

    # -- knowledge ------------------------------------------------------------------------------------------------
    def t_search_knowledge_base(self, query: str, limit: int | None = 4) -> dict:
        from kestrel.kb import default_kb
        hits = default_kb().search(query, top_k=max(1, min(int(limit or 4), 8)))
        return {"results": [{"citation": c.cite(), "score": round(s, 2), "text": c.text[:600]} for s, c in hits]}

    @lru_cache(maxsize=1)
    def _campaign(self) -> dict:
        return _load(ADV_DATA / "recall" / "campaign.json")

    def t_get_bulletin(self, bulletin_id: str) -> dict:
        c = self._campaign()
        if bulletin_id.upper() != "TSB-2026-09":
            raise ToolError("not_found", f"Bulletin {bulletin_id} not found (IDs look like TSB-2026-09).")
        return {"bulletin_id": "TSB-2026-09", "title": c["title"], "issued": c["issued"], "status": c["status"],
                "lots": {k: {"affects": v["affects"], "hazard": v["hazard"], "remedy": v["remedy"], "interim": v["interim"]} for k, v in c["lots"].items()}}

    def t_list_bulletins(self, since: str | None = None) -> dict:
        return {"bulletins": [{"bulletin_id": "TSB-2026-09", "title": self._campaign()["title"], "issued": self._campaign()["issued"]}]}

    def t_get_runbook(self, name: str) -> dict:
        books = {"seal failure triage": ["1. Pull telemetry: seal temperature and vibration.", "2. Above 70 C or rising vibration: P2, stop unattended running.",
                                         "3. Check lot via get_build_record; PS-2608-B is under recall RC-2026-03.", "4. Book seal_replacement visit."],
                 "recall outreach": ["1. Contact within SLA by risk class.", "2. Offer interim measure.", "3. Schedule remedy visit.", "4. Log every contact."]}
        key = next((k for k in books if k in name.lower()), None)
        if key is None:
            raise ToolError("not_found", f"No runbook named {name!r}. Known: {', '.join(books)}.")
        return {"name": key, "steps": books[key]}

    def t_get_policy(self, name: str) -> dict:
        files = {"returns": "returns_and_refunds.md", "warranty": "warranty.md", "shipping": "shipping.md", "refund_approval": "returns_and_refunds.md",
                 "data_handling": "data_privacy.md"}
        fname = files.get(name.lower())
        path = DATA_DIR / "company" / "policies" / (fname or "")
        if not fname or not path.exists():
            candidates = sorted(p.name for p in (DATA_DIR / "company" / "policies").glob("*.md"))
            raise ToolError("not_found", f"No policy named {name!r}. Known: {', '.join(files)}. Files: {', '.join(candidates)}")
        return {"name": name, "text": path.read_text(encoding="utf-8")[:1500]}

    def t_get_faq(self, topic: str) -> dict:
        return {"topic": topic, "faq": [{"q": f"What is Kestrel's policy on {topic}?", "a": "See get_policy for the approved wording."}]}

    # -- communications -------------------------------------------------------------------------------------------
    def t_escalate_to_human(self, queue: str, priority: str, summary: str) -> dict:
        esc = self._id("ESC")
        self.audit.append({"action": "escalate_to_human", "escalation_id": esc, "queue": queue, "priority": priority})
        sla = {"P1": "response within 1 hour", "P2": "response within 4 hours", "P3": "response within 1 business day", "P4": "next weekly review"}
        return {"escalation_id": esc, "queue": queue, "priority": priority, "sla": sla.get(priority, "response within 1 business day")}

    def t_request_approval(self, approver_role: str, action: str, justification: str) -> dict:
        apr = self._id("APR")
        self.audit.append({"action": "request_approval", "approval_id": apr, "approver_role": approver_role})
        return {"approval_id": apr, "approver_role": approver_role, "status": "pending", "poll": "get back to the requester when decided"}

    def t_create_task(self, team: str, title: str, due_date: str, note: str | None = None) -> dict:
        tid = self._id("TASK")
        return {"task_id": tid, "team": team, "title": title[:120], "due_date": due_date}

    def t_post_teams_message(self, channel: str, text: str) -> dict:
        mid = self._id("MSG")
        self.audit.append({"action": "post_teams_message", "channel": channel})
        return {"message_id": f"teams-{channel}-{mid}", "channel": channel, "posted": True, "chars": len(text)}

    def t_create_ticket_comment(self, ticket_id: str, text: str) -> dict:
        t = self.t_get_service_ticket(ticket_id)
        t["history"].append({"at": TODAY, "event": "comment: " + text[:120]})
        return {"ticket_id": t["ticket_id"], "comment_added": True}

    def t_notify_account_manager(self, customer_id: str, summary: str) -> dict:
        am = self.t_get_account_manager(customer_id)
        self.audit.append({"action": "notify_account_manager", "customer_id": customer_id})
        return {"notified": am["account_manager"], "email": am["email"], "customer_id": customer_id, "summary_chars": len(summary)}

    def t_log_call(self, contact_id: str, summary: str) -> dict:
        return {"contact_id": contact_id, "logged": True}

    # -- tools outside the catalog (labs 04 and 06) ----------------------------------------------------------------
    @lru_cache(maxsize=1)
    def _recall_units(self) -> dict[str, dict]:
        return {u["serial_number"]: u for u in _load(ADV_DATA / "recall" / "affected_units.json")["units"]}

    def t_get_recall_status(self, serial_number: str) -> dict:
        u = self._recall_units().get(serial_number.upper())
        if u is None:
            self._build(serial_number)                # a real unit that is simply not in the campaign
            return {"serial_number": serial_number.upper(), "recall": "RC-2026-03", "affected": False}
        rules = {r["risk_class"]: r for r in self._campaign()["priority_rules"]}[u["risk_class"]]
        visit = next((v for v in self.visits if self.tickets.get(v["ticket_id"], {}).get("serial_number") == u["serial_number"]
                      or self.tickets.get(v["ticket_id"], {}).get("site_id") == u["site_id"]), None)
        return {"serial_number": u["serial_number"], "recall": "RC-2026-03", "affected": True,
                "scheduled": visit is not None,
                "remedy_visit": f"{visit['visit_id']} with {visit['engineer']} at {visit['slot_start']}" if visit else None,
                "remedy": u["remedy"], "lot": u["lot"], "risk_class": u["risk_class"],
                "contact_within_business_days": rules["contact_within_business_days"]}

    def t_draft_bulletin(self, bulletin_id: str, title: str, audience: str, lots: list, body: str, actions: list) -> dict:
        draft = {"draft_id": f"DRAFT-{len(self.drafts) + 1:03d}", "bulletin_id": bulletin_id, "title": title, "audience": audience,
                 "lots": lots, "words": len(body.split()), "actions": len(actions), "review_by": "the quality lead"}
        self.drafts.append({**draft, "body": body, "action_list": list(actions)})
        self.audit.append({"action": "draft_bulletin", "draft_id": draft["draft_id"]})
        return draft

    def t_send_email(self, to: str, subject: str, body: str) -> dict:
        self._needs_approval("send_email", "support_manager")
        return {"message_id": f"mail-{self._id('MSG')}", "to": to, "subject": subject}




# ------------------------------------------------------------------------------------ the agent loop
@dataclass
class Turn:
    n: int
    stop_reason: str
    input_tokens: int
    cache_write: int
    cache_read: int
    output_tokens: int
    cost: float
    calls: list[str] = field(default_factory=list)
    searches: list[str] = field(default_factory=list)
    discovered: list[str] = field(default_factory=list)
    code: list[str] = field(default_factory=list)
    blocks: list[str] = field(default_factory=list)

    @property
    def prompt(self) -> int:
        return self.input_tokens + self.cache_write + self.cache_read


@dataclass
class RunResult:
    status: str
    reply: str
    messages: list[dict]
    turns: list[Turn]
    container_id: str | None = None
    responses: list[Any] = field(default_factory=list)

    @property
    def model_calls(self) -> int:
        return len(self.turns)

    def total(self, attr: str) -> int | float:
        return sum(getattr(t, attr) for t in self.turns)

    @property
    def calls(self) -> list[str]:
        return [c for t in self.turns for c in t.calls]

    @property
    def called(self) -> list[str]:
        return [c.split("(", 1)[0] for c in self.calls]

    @property
    def searches(self) -> list[str]:
        return [s for t in self.turns for s in t.searches]

    @property
    def discovered(self) -> list[str]:
        return list(dict.fromkeys(d for t in self.turns for d in t.discovered))


def _short(value: Any, n: int = 60) -> str:
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return text if len(text) <= n else text[: n - 3] + "..."


def call_label(block: Any, width: int = 28) -> str:
    args = ", ".join(f"{k}={_short(v, width)}" for k, v in (block.input or {}).items())
    tag = " [from code]" if getattr(block, "caller", None) and getattr(block.caller, "type", "direct") != "direct" else ""
    return f"{block.name}({args}){tag}"


def print_turn(turn: Turn, indent: str = "    ") -> None:
    print(f"{indent}turn {turn.n}  {turn.stop_reason:<9} prompt {turn.prompt:>6,} = read {turn.cache_read:>6,} + write "
          f"{turn.cache_write:>5,} + uncached {turn.input_tokens:>4,}   out {turn.output_tokens:>4,}")
    for q, found in zip(turn.searches, [turn.discovered] + [[]] * len(turn.searches)):
        print(f"{indent}        search {q!r} -> {', '.join(found) or 'nothing'}")
    for c in turn.calls:
        print(f"{indent}        call   {c}")


def run_agent(client: Any, *, system: str | list, tools: list[dict], messages: list[dict],
              execute: Callable[[str, dict], tuple[str, bool]], model: str = MODEL, max_turns: int = 8,
              max_tokens: int = 4000, betas: list[str] | None = None, cache: bool = True, container: str | None = None,
              trace: bool = True, extra: dict | None = None) -> RunResult:
    """One loop for every lab: direct tool calls, tool search, programmatic calls (results-only messages with the
    container id), pause_turn, max_tokens retries and refusals - and a per-turn trace of tokens and cache reads.

    Caching: an explicit breakpoint on the system prompt (tools + system, shared by every conversation) plus top-level
    automatic caching for the conversation tail."""
    if cache and isinstance(system, str):
        system = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    api = client.beta.messages if betas else client.messages
    turns: list[Turn] = []
    responses: list[Any] = []
    budget = max_tokens
    status, reply = "max_turns", ""
    for n in range(1, max_turns + 1):
        kw: dict = {"model": model, "max_tokens": budget, "system": system, "tools": tools, "messages": messages}
        if cache:
            kw["cache_control"] = {"type": "ephemeral"}
        if betas:
            kw["betas"] = betas
        if container:
            kw["container"] = container
        if extra:
            kw.update(extra)
        r = api.create(**kw)
        responses.append(r)
        if getattr(r, "container", None):
            container = r.container.id
        u = r.usage
        turn = Turn(n, r.stop_reason or "", u.input_tokens, u.cache_creation_input_tokens or 0, u.cache_read_input_tokens or 0,
                    u.output_tokens, response_cost(r), blocks=blocks_of(r))
        for b in r.content:
            if b.type == "tool_use":
                turn.calls.append(call_label(b))
            elif b.type == "server_tool_use" and b.name.startswith("tool_search"):
                turn.searches.append(search_input(b))
            elif b.type == "server_tool_use" and b.name == "code_execution":
                turn.code.append(b.input.get("code", ""))
            elif b.type == "tool_search_tool_result":
                refs = getattr(b.content, "tool_references", None) or []
                turn.discovered += [ref.tool_name for ref in refs]
        turns.append(turn)
        if trace:
            print_turn(turn)
        if r.stop_reason == "refusal":
            status = "refused"
            break
        if r.stop_reason == "max_tokens":
            if any(b.type == "tool_use" for b in r.content) or not text_of(r).strip():
                if budget >= 16_000:
                    status = "truncated"
                    break
                budget = min(budget * 2, 16_000)       # the truncated turn is dropped, never executed
                continue
            status, reply = "truncated", text_of(r)
            break
        messages.append({"role": "assistant", "content": r.content})
        if r.stop_reason == "pause_turn":
            continue
        if r.stop_reason != "tool_use":
            status, reply = "done", text_of(r)
            break
        results = []
        for b in r.content:
            if b.type != "tool_use":
                continue
            content, is_error = execute(b.name, b.input or {})
            block = {"type": "tool_result", "tool_use_id": b.id, "content": content}
            if is_error:
                block["is_error"] = True
            results.append(block)
        messages.append({"role": "user", "content": results})     # results only: valid for direct and programmatic calls
    return RunResult(status, reply, messages, turns, container, responses)


def outcome(run: RunResult, needs: Iterable[str]) -> str:
    """done: every needed tool was called and nothing escalated; escalated; partial: something needed was never called."""
    called = set(run.called)
    if "escalate_to_human" in called:
        return "escalated"
    if run.status != "done":
        return run.status
    return "done" if set(needs) <= called else "partial"


# ------------------------------------------------------------------------------------ task sets
# Lab 02 - discovery: natural requests -> the one catalog tool that answers them (none of them is a core tool).
DISCOVERY_TASKS: list[tuple[str, str]] = [
    ("Where is order SO-10303 right now? Give me the carrier scan history for tracking NLF9028150109.", "track_shipment"),
    ("Which carrier and ETA are recorded on shipment SH-50283?", "get_shipment"),
    ("What does controller fault code F17 on a KC-2 mean, and what should the site do about it?", "decode_fault_code"),
    ("Show me the vibration trend of KP250-2608-0004 over the last 14 days.", "get_vibration_trend"),
    ("Which engineers with the seal_replacement skill are free in US-EAST between 2026-09-21 and 2026-09-25?", "get_engineer_availability"),
    ("Every serial number built with seal lot PS-2608-B, and the customer each one shipped to.", "list_units_by_lot"),
    ("Reserve 4 MS-250-R kits in WH-EAST for ticket SVC-4021 so nobody sells them.", "reserve_stock"),
    ("How much stock of KC-2-PSB do we have in each warehouse?", "get_stock"),
    ("What is the replenishment lead time for MS-100-R?", "get_lead_time"),
    ("Quote carriers for 380 kg from WH-EU to ES with express service.", "get_carrier_rates"),
    ("Book a carrier pickup at SITE-1005-A on 2026-09-22 for 2 pieces.", "schedule_pickup"),
    ("What is the outstanding balance of C-1014 and how many days past due are they?", "get_account_balance"),
    ("Waive the late fees on invoice AR-90244; the delay was ours.", "apply_late_fee_waiver"),
    ("Record a payment of $8,188 received against AR-90257, bank reference BW-5521.", "record_payment"),
    ("Open a return authorisation for 1 KP-250-S on SO-10248, reason code defective.", "create_rma"),
    ("What restocking fee applies to returning 2 MS-250 with reason code no_longer_needed?", "get_restocking_fee"),
    ("Show the inspection findings for the units received under RMA-7001.", "get_rma_inspection"),
    ("Register KP100-2608-0001 as installed at SITE-1025-A on 2026-09-01 so its warranty clock starts.", "register_warranty"),
    ("List the past service visits and the parts used on KP250-2608-0002.", "get_service_history"),
    ("Datasheet ratings - flow, head and power - for KP-250-S.", "get_datasheet"),
    ("Bill of materials with the spare parts of the KC-2 controller.", "get_bom"),
    ("Block shipment of lot PS-2608-B pending the seal investigation.", "open_quality_hold"),
    ("Factory acceptance test results for KC2-2608-0001.", "get_test_results"),
    ("Latest telemetry readings - flow, head and seal temperature - for KP250-2608-0006.", "get_pump_telemetry"),
    ("Acknowledge alert ALR-88012 with a note so it stops paging.", "acknowledge_alert"),
    ("Roll-up of unit health and open alerts for site SITE-1014-A.", "get_site_health"),
    ("Get bulletin TSB-2026-09 about the seal lot recall.", "get_bulletin"),
    ("Find the runbook for seal failure triage.", "get_runbook"),
    ("Post to the quality Teams channel that the PS-2608-B hold is in place.", "post_teams_message"),
    ("Contacts at C-1002 with their roles, emails and phone numbers.", "list_contacts"),
    ("Configurable options - voltage, protocol, enclosure - of the KC-2 controller SKU.", "get_configuration_options"),
    ("What credit limit does C-1014 have, and how much of it is still available?", "get_credit_limit"),
]

# Lab 03 - the wide agent: each task needs at least one tool outside the nine core tools.
WIDE_TASKS: list[dict] = [
    {"id": "W1", "text": "Where is the shipment for SO-10290? The customer says the carrier has not scanned it since Friday.",
     "needs": ["list_shipments_for_order", "track_shipment"]},
    {"id": "W2", "text": "KC2-2608-0001 at Lumen Data Centers keeps logging F17. What does F17 mean on a KC-2, and is the unit "
                         "still under warranty?",
     "needs": ["decode_fault_code", "check_warranty"]},
    {"id": "W3", "text": "List every unit built with seal lot PS-2608-B and the customer each one shipped to.",
     "needs": ["list_units_by_lot"]},
    {"id": "W4", "text": "Ticket SVC-4021 needs a seal_replacement visit. Find engineers free in US-EAST between 2026-09-21 and "
                         "2026-09-25 with that skill; then book the first free slot for the ticket.",
     "needs": ["get_engineer_availability", "schedule_field_visit"]},
    {"id": "W5", "text": "Invoice AR-90244 is overdue because our delivery was late. Waive the late fees on it, then tell me the "
                         "open amount and the due date.",
     "needs": ["apply_late_fee_waiver", "get_invoice"]},
]

# Lab 04 - one conversation in phases; the application (not the model) decides which tools each phase exposes.
PHASES: list[dict] = [
    {"name": "diagnose", "tools": ["get_fault_codes", "decode_fault_code", "get_pump_telemetry"],
     "turns": ["KC2-2608-0001 at Lumen Data Centers keeps logging F17. Pull its fault codes for the last 7 days and decode F17 "
               "for the KC-2 family."]},
    {"name": "schedule", "tools": ["create_service_ticket", "get_engineer_availability", "schedule_field_visit"],
     "turns": ["Open a P2 service ticket for site SITE-1012-A, customer C-1012: KC-2 board fault F17 under recall RC-2026-03. "
               "Then find engineers in US-EAST with the controller_firmware skill free between 2026-09-21 and 2026-09-25.",
               "Book the first free slot with that engineer for the ticket."]},
    {"name": "communicate", "tools": ["notify_account_manager", "post_teams_message"],
     "turns": ["Notify the account manager of C-1012 with a summary of the plan, and post a short update to the field-service "
               "Teams channel."]},
    {"name": "diagnose", "tools": ["get_fault_codes", "decode_fault_code", "get_pump_telemetry"],
     "turns": ["One more thing before I call them: decode fault code F05 for the KC-1 family."]},
]
RECALL_STATUS_TURN = "Is KC2-2608-0001 already scheduled under recall RC-2026-03?"


# Lab 05 - the recall campaign's 11 affected units.
@lru_cache(maxsize=1)
def campaign() -> dict:
    return _load(ADV_DATA / "recall" / "campaign.json")


def recall_units() -> list[dict]:
    return _load(ADV_DATA / "recall" / "affected_units.json")["units"]


TRIAGE_RULE = ("Triage rule: P1 = seal chamber above 70 C and (vibration trend rising or safety duty); P2 = seal chamber above "
               "70 C, or a KC-2 controller with 5 or more F17 faults in the last 7 days; P3 = everything else. Look at the 7-day "
               "vibration trend only for units above 70 C.")


def triage_question() -> str:
    lines = [f"{u['serial_number']:<16} {u['sku']:<9} {u['region']:<8} {u['risk_class']}" for u in recall_units()]
    return (f"Recall RC-2026-03 covers these {len(lines)} units (serial, SKU, region, risk class):\n" + "\n".join(lines) + "\n\n"
            "Check every one: the latest seal-chamber temperature for the pumps (KP-...), the fault codes of the last 7 days "
            "for the KC-2 controllers. " + TRIAGE_RULE + " Give me the triage table.")


KITS_QUESTION = ("Now the remedy kits: MS-250-R for the KP-250-S, MS-100-R for the KP-100-S, KC-2-PSB for the KC-2. Regions ship "
                 "from US-EAST -> WH-EAST, US-WEST -> WH-WEST, EU -> WH-EU. How many kits of each do we need per warehouse "
                 "for all the units, and where is stock short?")


# Lab 06 - the bulletin request (facts from the campaign notice).
def bulletin_request() -> str:
    lots = campaign()["lots"]
    facts = [f"{lot}: hazard: {v['hazard']} | remedy: {v['remedy']} | interim: {v['interim']}" for lot, v in lots.items()]
    return ("Draft technical safety bulletin TSB-2026-09 for operators about recall RC-2026-03 and save it with "
            "draft_bulletin. Facts per lot:\n" + "\n".join(facts))


# Lab 07 - selection eval: 12 tools with near-duplicates, 30 tasks, two description sets.
SELECTION_TOOLS = ["get_order", "get_order_status_history", "list_shipments_for_order", "get_shipment", "track_shipment", "get_invoice",
                   "issue_refund", "issue_credit_note", "apply_late_fee_waiver", "check_return_eligibility", "create_rma",
                   "escalate_to_human"]

SELECTION_TASKS: list[tuple[str, str]] = [
    ("Tracking number NLF9028150109 has not had a scan since Friday. Where is the pallet right now?", "track_shipment"),
    ("Harbor Foods asks whether NLF9463425003 is out for delivery today.", "track_shipment"),
    ("Give me the latest carrier scan for BRL8107534831.", "track_shipment"),
    ("Is NLF7945896356 delayed? They were promised Monday.", "track_shipment"),
    ("Which carrier did we use on shipment SH-50283 and what ETA did we promise?", "get_shipment"),
    ("Pull the ship date and the delivered date of SH-50237.", "get_shipment"),
    ("What exception reason is recorded on SH-50271?", "get_shipment"),
    ("Did SO-10303 go out as one shipment or as several partials?", "list_shipments_for_order"),
    ("List every shipment we created for SO-10248.", "list_shipments_for_order"),
    ("How many separate shipments were sent for order SO-10272?", "list_shipments_for_order"),
    ("What is on SO-10248 and what promised date did we give?", "get_order"),
    ("Show me order SO-10257 with its lines and the customer PO.", "get_order"),
    ("Which PO number did Midland Oil put on SO-10272?", "get_order"),
    ("When did SO-10303 move from confirmed to shipped, and who released it?", "get_order_status_history"),
    ("Give me the full status timeline of SO-10248 with timestamps.", "get_order_status_history"),
    ("Is AR-90257 paid, and when is it due?", "get_invoice"),
    ("What is the open amount on invoice AR-90267?", "get_invoice"),
    ("Send $2,100 back to Harbor Foods' card for the returned unit on SO-10248 (RMA-7002).", "issue_refund"),
    ("The customer paid twice for SO-10272; return the duplicate $24,564 to their payment method.", "issue_refund"),
    ("Pay back $640 to the original payment method for order SO-10303.", "issue_refund"),
    ("Knock $500 off what Midland owes on AR-90257 for the late delivery - no cash back, just reduce the balance.", "issue_credit_note"),
    ("Issue a credit against AR-90267 for the damaged seal kit, $640, reducing what they owe.", "issue_credit_note"),
    ("Reduce invoice AR-90257 by 5% as a goodwill credit; they will pay the rest.", "get_invoice"),   # 5% of what? look it up first
    ("Waive the late fees on AR-90244 - the delay was our fault.", "apply_late_fee_waiver"),
    ("Meridian asks us to drop the late-payment charge on invoice AR-90250.", "apply_late_fee_waiver"),
    ("Can Harbor Foods return 1 KP-250-S from SO-10248? They no longer need it; what restocking fee applies?", "check_return_eligibility"),
    ("Is a return still possible for 2 x KV-50-F on SO-10303 (wrong item)?", "check_return_eligibility"),
    ("Open a return authorisation for 1 KP-250-S on SO-10248, reason defective.", "create_rma"),
    ("Create the RMA for 2 KV-50-F valves on SO-10303, wrong item ordered.", "create_rma"),
    ("There is a smell of solvent and a wet seal on KP250-2608-0005 at Keystone - get a person on this now.", "escalate_to_human"),
]

# Description set B: the same 12 tools, rewritten as contracts - trigger phrases in the users' words, ID formats,
# what the tool is not for (and which neighbour to use), units and side effects.
DESCRIPTIONS_B: dict[str, str] = {
    "get_order": "Return one sales order: its lines (SKU, qty, price), current status, promised date, customer PO number, total and the "
                 "IDs of its shipments and invoice. Use for 'what is on this order', 'what did we promise', 'which PO is this'. "
                 "Order IDs look like SO-10248. For where a delivery is, use track_shipment; for the status timeline, "
                 "get_order_status_history.",
    "get_order_status_history": "The dated status timeline of one order: every transition (confirmed, shipped, delivered, on hold, "
                                "cancelled) with timestamps and the actor who made it. Use when the question is 'when did it move to X' "
                                "or 'who released/held it'. Order IDs look like SO-10248.",
    "list_shipments_for_order": "All shipments created for one order, including partial shipments: how many there are, their shipment "
                                "IDs, carriers and tracking numbers. Use for 'one shipment or several', 'list the shipments of an order', "
                                "and to find the tracking number before tracking. Order IDs look like SO-10248.",
    "get_shipment": "One shipment's booking record by shipment ID (SH-50283): the carrier we booked, the ship date, the promised ETA, "
                    "the delivered date and the exception reason we recorded. Use for 'which carrier did we use', 'what ETA did we "
                    "promise', 'what exception is recorded'. Takes a shipment ID, not a tracking number.",
    "track_shipment": "Live carrier tracking for a tracking number (e.g. NLF9028150109): the latest scan, scan history, current "
                      "location, out for delivery, delayed, delivered or stuck at a hub. Use when someone asks where a pallet or "
                      "delivery is right now, whether it has moved, is out for delivery or is delayed. Takes the carrier tracking "
                      "number.",
    "get_invoice": "One invoice by ID (AR-90257): amount, paid amount, open amount, due date, days past due and status. Use for "
                   "'is it paid', 'when is it due', 'how much is open'. Read-only.",
    "issue_refund": "Send money back to the customer's card or bank - the original payment method - for an order: 'refund', 'pay back', "
                    "'send back', 'return the money', 'they paid twice'. Amount in USD; over $2,500 needs a support manager and over "
                    "$10,000 the finance director. Irreversible: money leaves Kestrel.",
    "issue_credit_note": "Reduce what a customer owes on an invoice (AR-90257) without moving money: 'knock $500 off', 'goodwill "
                         "credit', 'credit them', 'reduce the balance or invoice by 5%'. Amount in USD. Needs approval. No cash "
                         "goes back to the customer.",
    "apply_late_fee_waiver": "Cancel the late-payment fees or charges on an overdue invoice (AR-90244) when the delay was ours or as "
                             "goodwill: 'waive the late fee', 'drop the late-payment charge'. Only touches the fees.",
    "check_return_eligibility": "Apply the returns policy to a delivered order line before promising anything: return window, "
                                "restocking fee, exclusions (configured or special-order items). Use for 'can they return it', 'what "
                                "fee applies', 'is a return still possible'. Needs order ID (SO-10248), SKU (KP-250-S), quantity and "
                                "a reason code: no_longer_needed, wrong_item, damaged_in_transit, defective, other. Read-only.",
    "create_rma": "Open a return authorisation (RMA) for delivered units once the return is decided: 'open a return', 'create the "
                  "RMA'. Needs order ID, SKU, quantity and a reason code. Write action, idempotent per order line.",
    "escalate_to_human": "Hand the conversation to a human queue (billing, logistics, security, quality, field_service, "
                         "account_management) with a priority and a summary; this ends the agent's involvement. Use for safety "
                         "incidents (leak, smell, smoke, fire, injury: P1), for anything no tool can do, and for approvals above "
                         "your limits: 'get a person on this'.",
}


# ------------------------------------------------------------------------------------ role scoping (tenants.json)
@lru_cache(maxsize=1)
def tenants() -> dict:
    return _load(ADV_DATA / "security" / "tenants.json")


RISK_RANK = {"read": 0, "write": 1, "irreversible": 2}


def scoped_names(role: str, *, plus: Iterable[str] = ()) -> list[str]:
    """Catalog tools a role may see: its domains, at most its risk level, minus its denied tools and the set denied to
    every agent. `plus` adds tools the harness grants every session (for example escalation)."""
    spec = tenants()["roles"][role]
    denied = set(spec.get("denied_tools", [])) | set(tenants()["always_denied_to_agents"])
    domains = spec["domains"]
    out = []
    for t in catalog_tools():
        m = t["meta"]
        if t["name"] in plus:
            out.append(t["name"])
            continue
        if domains != "*" and m["domain"] not in domains:
            continue
        if RISK_RANK[m["risk"]] > RISK_RANK[spec["max_risk"]] or t["name"] in denied:
            continue
        out.append(t["name"])
    return out
