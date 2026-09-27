"""Mock policies for Day 5 (MCP & the Claude Agent SDK).

Two families of requests reach these policies:

1. **Anthropic-SDK requests** from labs 03/solutions that bridge the lab-01 MCP server into
   Claude (tools named check_stock / get_order_status / list_work_orders, possibly with an
   `mcp__<server>__` prefix when the Agent SDK mounts the same server).
2. **Claude Code CLI requests** from the Agent SDK labs 05-07: the bundled CLI talks to the mock
   over HTTP (labkit.mock.server).  These carry Claude Code's built-in tools (Read, Grep, Glob, Bash,
   Agent, ...), our in-process `mcp__sre__*` tools, trailing `role: "system"` messages, and
   `<system-reminder>` text blocks.  Each lab's system prompt contains a unique marker
   (e.g. KESTREL-SRE-INVESTIGATOR) that the matchers look for.

The CLI really executes the tool calls these policies emit (ripgrep over data/ops_logs, file
reads, hook callbacks, subagents), and every answer is computed from the tool results the "model"
received - never from files the policy reads itself.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import re
from typing import Any

from ..registry import scenario
from ..reply import Reply, say, tool, use_tools
from ..request import MockRequest, ToolCall

# =============================================================================== generic helpers
REMINDER_PREFIXES = ("<system-reminder>", "[SYSTEM NOTIFICATION")


def _blocks(content: Any) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [b for b in content if isinstance(b, dict)]
    return []


def _base(name: str) -> str:
    """'mcp__kestrel-plant-ops__check_stock' -> 'check_stock'."""
    return (name or "").rsplit("__", 1)[-1]


def _conversation(req: MockRequest) -> list[dict]:
    """user/assistant turns only: the CLI appends role=system messages (environment, token budget)."""
    return [m for m in req.messages if m.get("role") in ("user", "assistant")]


def _human_texts(req: MockRequest) -> list[str]:
    """Text the *person* typed, one entry per user turn, ignoring harness reminders and tool results."""
    out = []
    for m in _conversation(req):
        if m.get("role") != "user":
            continue
        parts = [b.get("text", "") for b in _blocks(m.get("content")) if b.get("type") == "text"
                 and not b.get("text", "").lstrip().startswith(REMINDER_PREFIXES)]
        text = "\n".join(p for p in parts if p.strip())
        if text.strip():
            out.append(text)
    return out


def _ends_with_tool_results(req: MockRequest) -> bool:
    convo = _conversation(req)
    return bool(convo) and convo[-1].get("role") == "user" and any(
        b.get("type") == "tool_result" for b in _blocks(convo[-1].get("content")))


def _turn_calls(req: MockRequest) -> list[ToolCall]:
    """Tool calls made since the last human message (the current agentic turn)."""
    convo = _conversation(req)
    start = 0
    for i, m in enumerate(convo):
        if m.get("role") == "user" and not any(b.get("type") == "tool_result" for b in _blocks(m.get("content"))):
            if any(b.get("type") == "text" and not b.get("text", "").lstrip().startswith(REMINDER_PREFIXES)
                   for b in _blocks(m.get("content"))):
                start = i
    ids = {b.get("id") for m in convo[start:] if m.get("role") == "assistant"
           for b in _blocks(m.get("content")) if b.get("type") == "tool_use"}
    return [c for c in req.tool_calls if c.id in ids]


def _calls(calls: list[ToolCall], base_name: str) -> list[ToolCall]:
    return [c for c in calls if _base(c.name) == base_name]


def _last(calls: list[ToolCall], base_name: str) -> ToolCall | None:
    found = _calls(calls, base_name)
    return found[-1] if found else None


def _real_name(req: MockRequest, base_name: str) -> str:
    return next((n for n in req.tool_names if _base(n) == base_name), base_name)


def _json_obj(call: ToolCall | None) -> dict:
    if call is None or call.result is None:
        return {}
    text = call.result.strip()
    try:
        data = json.loads(text)
    except ValueError:
        start = text.find("{")
        try:
            data = json.loads(text[start:]) if start >= 0 else {}
        except ValueError:
            return {}
    return data if isinstance(data, dict) else {}


def _json_lines(text: str | None) -> list[dict]:
    """Parse JSON log lines out of Grep/Read output ('path:123:{...}', '123:{...}' or raw lines)."""
    rows = []
    for line in (text or "").splitlines():
        start = line.find("{")
        if start < 0:
            continue
        try:
            obj = json.loads(line[start:])
        except ValueError:
            continue
        if isinstance(obj, dict):
            rows.append(obj)
    return rows


def _read_body(text: str | None) -> str:
    """Strip the Read tool's line-number prefixes ('    12\\tcontent')."""
    lines = []
    for line in (text or "").splitlines():
        m = re.match(r"^\s*\d+\t(.*)$", line)
        lines.append(m.group(1) if m else line)
    return "\n".join(lines)


def _ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value.replace("Z", "+00:00"))


def _hm(value: str | dt.datetime) -> str:
    stamp = _ts(value) if isinstance(value, str) else value
    return stamp.strftime("%H:%M:%S")


# =============================================================================== lab 03: plant-ops MCP tools
PLANT_OPS_TOOLS = {"check_stock", "get_order_status", "list_work_orders"}
ORDER_ID_RE = re.compile(r"\bSO-\d{5}\b")
ASSET_ID_RE = re.compile(r"\b[A-Z]{2}-KP\d{3}X?-\d{2}\b")
SKU_RE = re.compile(r"\b(?:MS|KP|KV|KC|IMP|BRG|CPL|GSK|ORK|LUB|FLT|VS|PT|FT)-[A-Z0-9]{1,5}(?:-[A-Z0-9]{1,5})?\b")
SEAL_RE = re.compile(r"\bseal\b|\bMS-\d{3}\b", re.I)


RMA_PROMPT_MARKER = "leave a placeholder [RMA-####]"      # sentence from lab 01's draft_rma_email prompt


def _is_plant_ops(req: MockRequest) -> bool:
    bases = {_base(n) for n in req.tool_names}
    if PLANT_OPS_TOOLS <= bases:
        return True
    # The MCP prompt flow may expose only get_order_status (least privilege); day 2 also has a tool
    # with that name, so additionally require the prompt's own wording.
    return "get_order_status" in bases and any(RMA_PROMPT_MARKER in t for t in _human_texts(req))


def _documents_text(req: MockRequest) -> str:
    parts = []
    for m in _conversation(req):
        if m.get("role") != "user":
            continue
        for b in _blocks(m.get("content")):
            if b.get("type") == "document":
                src = b.get("source") or {}
                parts.append(src.get("data", "") if src.get("type") == "text" else json.dumps(src)[:2000])
    return "\n".join(parts)


def _plant_ops_answer(sku: str, order: dict, stock: dict, history: dict, asset: str | None) -> str:
    lines = []
    if order and stock:
        qty = sum(line.get("qty", 0) for line in order.get("lines", []) if line.get("sku") == sku)
        total = stock.get("total_available", 0)
        by_wh = {w["warehouse"]: w for w in stock.get("warehouses", [])}
        verdict = "Yes" if total >= qty else "No"
        split = ", ".join(f"{wh} {w['available']}" for wh, w in sorted(by_wh.items()))
        lines.append(f"**Stock: {verdict}.** Order {order.get('order_id')} ({order.get('customer_name')}) was for "
                     f"{qty} x {sku}. We have {total} {sku} available company-wide right now ({split}).")
        short = [wh for wh, w in sorted(by_wh.items()) if w["available"] < qty]
        low = [wh for wh, w in sorted(by_wh.items()) if w.get("below_reorder_point")]
        best = max(by_wh.values(), key=lambda w: w["available"]) if by_wh else None
        if short and best:
            lines.append(f"Note: {', '.join(short)} cannot cover it on its own"
                         + (f" ({', '.join(low)} is below its reorder point)" if low else "")
                         + f" - ship from {best['warehouse']} ({best['available']} available).")
    elif stock:
        lines.append(f"{sku}: {stock.get('total_available')} available in total.")
    elif order:
        qty = sum(line.get("qty", 0) for line in order.get("lines", []) if line.get("sku") == sku)
        lines.append(f"**Stock: unknown.** Order {order.get('order_id')} was for {qty} x {sku}, but I could not check "
                     "current stock, so I cannot confirm we can cover the repeat order.")
    if history:
        wos = history.get("work_orders", [])
        seal = [w for w in wos if SEAL_RE.search(f"{w.get('description', '')} {w.get('parts_used', '')}")]
        if seal:
            w = seal[0]
            lines.append(f"**Last seal replacement on {history.get('asset_id')}:** {w['date']} ({w['work_order_id']}: "
                         f"{w['description']}).")
        else:
            latest = wos[0] if wos else None
            lines.append(f"**Seal replacement on {history.get('asset_id')}:** none on record. The maintenance history "
                         f"({history.get('total_on_record', len(wos))} work orders since installation on "
                         f"{history.get('install_date')}) contains no seal work"
                         + (f"; the latest entry is {latest['date']} {latest['type']}: {latest['description']}"
                            if latest else "") + ".")
            lines.append("If a seal was replaced, it was not recorded against this asset - worth checking with the "
                         "site before assuming the original seal is still fitted.")
    elif asset:
        lines.append(f"I could not retrieve the maintenance history for {asset}.")
    return "\n\n".join(lines)


def _rma_email(order: dict, sku: str, reason: str, policy: str) -> str:
    days = re.search(r"within \*\*(\d+) calendar days of delivery", policy)
    fee = re.search(r"\*\*(\d+)% restocking fee\*\*", policy)
    window = int(days.group(1)) if days else 30
    fee_pct = fee.group(1) if fee else "15"
    ship = order.get("shipment") or {}
    delivered = ship.get("delivered_date")
    line = next((l for l in order.get("lines", []) if l.get("sku") == sku), {})
    deadline = (dt.date.fromisoformat(delivered) + dt.timedelta(days=window)).isoformat() if delivered else None
    body = [f"Subject: Return of {sku} from order {order.get('order_id')} - RMA [RMA-####]", "",
            f"Dear {order.get('customer_name')} team,", "",
            f"Thank you for letting us know you would like to return the {line.get('name', sku)} "
            f"({line.get('qty', '?')} units) from order {order.get('order_id')}"
            + (f", delivered on {delivered}" if delivered else "") + "."]
    if reason in ("no_longer_needed", "not_needed"):
        body += ["", "Under our returns policy (RET-002):",
                 f"- Unused items in their original packaging can be returned within {window} calendar days of "
                 f"delivery" + (f" - for this order, by {deadline}." if deadline else "."),
                 f"- A {fee_pct}% restocking fee of the returned line value applies to non-defective returns."]
    else:
        body += ["", "Because the kits are reported defective, we will handle this as a warranty claim (WAR-001): "
                 "there is no restocking fee, and our technicians will inspect the returned kits."]
    body += ["- Please quote RMA number [RMA-####] on the shipment; returns without an RMA are refused at our dock.",
             "", "We will confirm the RMA number and the return address by email.", "",
             "Kind regards,", "Kestrel Customer Support"]
    return "\n".join(body)


@scenario("day5.plant_ops", match=_is_plant_ops, priority=40)
def plant_ops(req: MockRequest) -> Reply:
    humans = _human_texts(req)
    question = humans[-1] if humans else req.first_user_text
    calls = _turn_calls(req)
    orders = ORDER_ID_RE.findall(question)
    assets = ASSET_ID_RE.findall(question)
    policy = _documents_text(req)

    # --- prompt-driven flow: "Draft ... email" rendered from the MCP prompt (lab 03 part C)
    if re.search(r"\bdraft\b.*\bemail\b", question, re.I | re.S) and orders:
        order_call = _last(calls, "get_order_status")
        if order_call is None:
            return use_tools(tool(_real_name(req, "get_order_status"), order_id=orders[0]),
                             preface="I'll confirm the order details first.")
        order = _json_obj(order_call)
        if order_call.is_error or not order:
            return say(f"I couldn't confirm order {orders[0]}: {(order_call.result or '')[:200]}")
        skus = SKU_RE.findall(question) or [l["sku"] for l in order.get("lines", [])]
        reason = (re.search(r"reason:\s*([a-z_]+)", question) or re.search(r"(defective)", question) or [None, "defective"])[1]
        return say(_rma_email(order, skus[0], reason, policy), complexity=0.35)

    # --- question-answering flow (lab 03 parts A/B, exercises)
    batch = []
    if orders and not _calls(calls, "get_order_status"):
        batch.append(tool(_real_name(req, "get_order_status"), order_id=orders[0]))
    if assets and not _calls(calls, "list_work_orders"):
        batch.append(tool(_real_name(req, "list_work_orders"), asset_id=assets[0]))
    if batch:
        return use_tools(*batch, preface="I'll pull the order and the pump's maintenance history in parallel.")

    order = _json_obj(_last(calls, "get_order_status"))
    skus = SKU_RE.findall(question) or [l["sku"] for l in order.get("lines", [])]
    sku = skus[0] if skus else None
    if sku and not _calls(calls, "check_stock"):
        return use_tools(tool(_real_name(req, "check_stock"), sku=sku),
                         preface=f"The order is for {sku}; checking current stock in every warehouse.")

    errors = [c for c in calls if c.is_error]
    stock = _json_obj(_last(calls, "check_stock"))
    history = _json_obj(_last(calls, "list_work_orders"))
    answer = _plant_ops_answer(sku or "?", order, stock, history, assets[0] if assets else None)
    if errors:
        answer += "\n\n(Some lookups failed: " + "; ".join(f"{_base(c.name)}: {(c.result or '').split('. ')[0][:160]}"
                                                          for c in errors) + ".)"
    return say(answer or "I could not find the information needed to answer.", complexity=0.4)


# =============================================================================== labs 05-07: SRE incident agents
def _gateway_rows(calls: list[ToolCall]) -> list[dict]:
    rows = []
    for c in calls:
        if c.name == "Grep" and "api-gateway" in json.dumps(c.input):
            rows += [r for r in _json_lines(c.result) if r.get("service") == "api-gateway"]
    return sorted({r.get("request_id", i): r for i, r in enumerate(rows)}.values(), key=lambda r: r["ts"])


def _service_rows(calls: list[ToolCall], service: str) -> list[dict]:
    rows = []
    for c in calls:
        if c.name in ("Grep", "Read"):
            rows += [r for r in _json_lines(c.result) if r.get("service") == service]
    unique = {(r.get("ts"), r.get("msg"), r.get("request_id")): r for r in rows}
    return sorted(unique.values(), key=lambda r: r["ts"])


def _deploy_rows(calls: list[ToolCall]) -> list[dict]:
    for c in reversed(calls):
        if c.name == "Read" and str(c.input.get("file_path", "")).endswith("deploys.csv") and not c.is_error:
            return list(csv.DictReader(io.StringIO(_read_body(c.result))))
        if _base(c.name) == "get_deploys" and not c.is_error:
            data = _json_obj(c) or {}
            if isinstance(data.get("deploys"), list):
                return data["deploys"]
    return []


def _count_lines(text: str | None) -> dict[str, int]:
    counts = {}
    for line in (text or "").splitlines():
        m = re.match(r"^(.+?):(\d+)$", line.strip())
        if m:
            counts[m.group(1)] = int(m.group(2))
    return counts


def _incident_facts(calls: list[ToolCall]) -> dict:
    """Everything the 'model' can conclude from the tool results it has seen so far."""
    gateway = _gateway_rows(calls)
    order_svc = _service_rows(calls, "order-service")
    deploys = _deploy_rows(calls)
    facts: dict[str, Any] = {"deploys": deploys}

    errors_5xx = [r for r in gateway if int(r.get("status", 0)) >= 500]
    # The incident = the densest cluster of 5xx (gaps < 15 min); isolated 500s are background noise.
    clusters: list[list[dict]] = []
    for r in errors_5xx:
        if clusters and (_ts(r["ts"]) - _ts(clusters[-1][-1]["ts"])).total_seconds() < 900:
            clusters[-1].append(r)
        else:
            clusters.append([r])
    incident = max(clusters, key=len) if clusters else []
    facts["baseline_5xx"] = len(errors_5xx) - len(incident)
    if incident:
        start, end = _ts(incident[0]["ts"]), _ts(incident[-1]["ts"])
        slow = [r for r in gateway if int(r.get("latency_ms", 0)) >= 1000
                and start - dt.timedelta(minutes=30) <= _ts(r["ts"]) <= end]
        facts.update(
            first_5xx=incident[0]["ts"], last_5xx=incident[-1]["ts"], count_5xx=len(incident),
            statuses=sorted({int(r["status"]) for r in incident}),
            upstreams=sorted({r.get("upstream", "?") for r in incident}),
            routes=sorted({re.sub(r"SO-\d+", "{id}", r.get("path", "")) for r in incident}),
            latency_p50=sorted(int(r.get("latency_ms", 0)) for r in incident)[len(incident) // 2],
            first_slow=slow[0]["ts"] if slow else incident[0]["ts"], first_slow_ms=slow[0]["latency_ms"] if slow else None,
            slow_count=len(slow))
    starts = [r for r in order_svc if r.get("msg") == "starting order-service"]
    facts["restarts"] = [(r["ts"], r.get("version"), (r.get("config") or {}).get("db_pool_max")) for r in starts]
    capacity = [r for r in order_svc if "pool at capacity" in r.get("msg", "")]
    timeouts = [r for r in order_svc if "timeout acquiring" in r.get("msg", "")]
    facts["first_capacity_warn"] = capacity[0]["ts"] if capacity else None
    facts["timeouts"] = len(timeouts)
    facts["max_waiting"] = max((int(r.get("waiting", 0)) for r in timeouts), default=0)
    facts["timeout_pool"] = (timeouts[0].get("pool_active"), timeouts[0].get("pool_max")) if timeouts else None
    facts["first_timeout"] = timeouts[0]["ts"] if timeouts else None

    culprit = rollback = None
    if incident:
        for d in deploys:
            began = _ts(d["started_at"])
            if d.get("service") in facts["upstreams"] and began <= _ts(facts["first_slow"]) and \
                    (_ts(facts["first_slow"]) - began).total_seconds() <= 7200:
                culprit = d
            if d.get("service") in facts["upstreams"] and began >= _ts(facts["first_slow"]) and \
                    re.search(r"rollback", d.get("change_summary", ""), re.I):
                rollback = d
    facts["culprit"], facts["rollback"] = culprit, rollback
    return facts


def _red_herrings(calls: list[ToolCall]) -> list[str]:
    notes = []
    warn_rows = [r for c in calls if c.name == "Grep" for r in _json_lines(c.result) if r.get("level") == "WARN"]
    tls = [r for r in warn_rows if "TLS certificate" in r.get("msg", "")]
    if tls:
        days = sorted({int(m.group(1)) for r in tls for m in [re.search(r"in (\d+) days", r["msg"])] if m})
        notes.append(f"payment-service TLS certificate warnings ({len(tls)} daily warnings, {min(days)}-{max(days)} days "
                     "left, auto-renewal scheduled): not an incident per RB-03 (escalate only below 7 days), and "
                     "payment routes were not failing")
    slow = [r for r in warn_rows if r.get("service") == "inventory-service" and "slow query" in r.get("msg", "")]
    if slow:
        notes.append(f"inventory-service slow-query warnings ({len(slow)} spread over 48 h, "
                     f"{min(int(r['db_query_ms']) for r in slow)}-{max(int(r['db_query_ms']) for r in slow)} ms): baseline "
                     "noise, not correlated with the 5xx window")
    for c in calls:
        if c.name == "Grep" and "wp-admin" in json.dumps(c.input):
            bot = [r for r in _json_lines(c.result) if "wp-admin" in r.get("path", "")]
            count = len(bot) or sum(_count_lines(c.result).values())
            if count:
                when = f" at {bot[0]['ts'][:16].replace('T', ' ')} UTC" if bot else ""
                notes.append(f"a bot scanning /wp-admin ({count} requests{when}, all 404 from the gateway itself): "
                             "noise, no upstream impact")
    return notes


def _incident_report(facts: dict, herrings: list[str], runbook_seen: bool) -> tuple[str, dict]:
    culprit, rollback = facts.get("culprit") or {}, facts.get("rollback") or {}
    service = (facts.get("upstreams") or ["order-service"])[0]
    diff = culprit.get("config_diff") or "no config change recorded"
    pool = facts.get("timeout_pool")
    root = (f"{service} deploy {culprit.get('deploy_id')} (v{culprit.get('version')}) changed {diff}; the DB connection "
            f"pool saturated ({pool[0]}/{pool[1]} connections busy, up to {facts.get('max_waiting')} requests waiting) and "
            f"requests timed out after 3000 ms acquiring a connection, so the gateway returned 503.") if culprit and pool \
        else f"{service} failing requests; root cause not confirmed."
    mitigation = (f"Rollback {rollback.get('deploy_id')} to v{rollback.get('version')} ({_hm(rollback['started_at'])}-"
                  f"{_hm(rollback['finished_at'])} UTC) restored {rollback.get('config_diff') or 'the previous config'}; "
                  f"the last 503 was at {_hm(facts['last_5xx'])}.") if rollback else "No mitigation found in the deploy log."
    timeline = []
    if culprit:
        timeline.append(f"{_hm(culprit['started_at'])} {culprit['deploy_id']} starts: {service} v{culprit['version']} "
                        f"({culprit.get('change_summary')}; {diff})")
    for when, version, pool_max in facts.get("restarts", []):
        timeline.append(f"{_hm(when)} {service} v{version} starts with db_pool_max={pool_max}")
    if facts.get("first_capacity_warn"):
        timeline.append(f"{_hm(facts['first_capacity_warn'])} WARN connection pool at capacity")
    if facts.get("first_slow"):
        timeline.append(f"{_hm(facts['first_slow'])} first slow request through the gateway "
                        f"({facts.get('first_slow_ms')} ms)")
    if facts.get("first_5xx"):
        timeline.append(f"{_hm(facts['first_5xx'])} first 503 at the gateway (upstream {service}, ~3 s latency)")
    if rollback:
        timeline.append(f"{_hm(rollback['started_at'])}-{_hm(rollback['finished_at'])} rollback {rollback['deploy_id']}")
    if facts.get("last_5xx"):
        timeline.append(f"{_hm(facts['last_5xx'])} last 503; errors stop after the rollback completes")
    timeline.sort()
    follow_ups = ["Validate Helm values in CI (bounds on db.pool.max; flag large changes in config diffs)",
                  "Alert on connection-pool saturation (pool_active == pool_max, waiting > 0) before users see 5xx",
                  "Canary order-service deploys and auto-rollback on 5xx rate"]
    evidence = [f"api-gateway.log: {facts.get('count_5xx')} x 503 from {service} between {_hm(facts['first_5xx'])} and "
                f"{_hm(facts['last_5xx'])} (p50 latency {facts.get('latency_p50')} ms, i.e. the 3000 ms pool timeout)"
                if facts.get("count_5xx") else "no 5xx cluster found",
                f"order-service.log: {facts.get('timeouts')} 'timeout acquiring DB connection after 3000ms' errors with "
                f"pool {pool[0]}/{pool[1]}" if pool else "order-service.log: no pool timeouts",
                f"deploys.csv: {culprit.get('deploy_id')} config_diff '{diff}'" if culprit else "deploys.csv: no match"]
    if facts.get("baseline_5xx"):
        evidence.append(f"{facts['baseline_5xx']} other 5xx over the 48 h are isolated (baseline)")
    if runbook_seen:
        evidence.append("RB-02 (DB connection pool exhaustion): pool_max dropped in the latest deploy's config diff "
                        "-> config regression -> roll back (RB-04)")
    report = {
        "root_cause": root,
        "trigger_deploy_id": culprit.get("deploy_id", "unknown"),
        "affected_service": service,
        "config_change": diff,
        "impact_start_utc": facts.get("first_slow", ""),
        "impact_end_utc": facts.get("last_5xx", ""),
        "error_count_5xx": int(facts.get("count_5xx") or 0),
        "mitigation": mitigation,
        "red_herrings": herrings,
        "follow_ups": follow_ups,
        "evidence": evidence,
    }
    md = ["## Incident summary - Kestrel Connect, 2026-09-14", "",
          f"**Root cause.** {root}", "",
          f"**Impact.** {facts.get('count_5xx')} requests failed with 503 on {', '.join(facts.get('routes', []))} "
          f"between {_hm(facts['first_5xx'])} and {_hm(facts['last_5xx'])} UTC; latency degraded from "
          f"{_hm(facts['first_slow'])} ({facts.get('slow_count')} requests over 1 s)." if facts.get("first_5xx") else "",
          "", "**Timeline (UTC)**", *[f"- {t}" for t in timeline], "",
          f"**Mitigation.** {mitigation}", "",
          "**Evidence**", *[f"- {e}" for e in evidence], "",
          "**Red herrings ruled out**", *[f"- {h}" for h in herrings or ["none examined"]], "",
          "**Follow-ups**", *[f"- {f}" for f in follow_ups]]
    return "\n".join(md), report


def _has_marker(req: MockRequest, marker: str) -> bool:
    return marker in req.system_text


def _call(name: str, tool_input: dict) -> dict:
    """A tool_use block whose input may itself contain a 'name' key (reply.tool() reserves that keyword)."""
    return {"type": "tool_use", "name": name, "input": tool_input}


# ------------------------------------------------------------------------------- lab 05: read-only investigator
@scenario("day5.sre_investigator", priority=50,
          match=lambda r: _has_marker(r, "KESTREL-SRE-INVESTIGATOR") and r.has_tool("Grep", "Read"))
def sre_investigator(req: MockRequest) -> Reply:
    calls = _turn_calls(req)
    if not calls:
        return use_tools(tool("Glob", pattern="**/*"),
                         tool("Grep", pattern='"level": "(ERROR|WARN)"', path="order-portal", output_mode="count"),
                         preface="Let me map the workspace and count warnings and errors per service before reading "
                                 "anything in detail.")
    if not any(c.name == "Read" for c in calls):
        return use_tools(
            tool("Grep", pattern='"status": 5\\d\\d|"latency_ms": \\d{4,}', path="order-portal/api-gateway.log",
                 output_mode="content"),
            tool("Grep", pattern='"level": "(ERROR|WARN)"|starting order-service', path="order-portal/order-service.log",
                 output_mode="content"),
            tool("Read", file_path="deploys.csv"),
            preface="Errors concentrate in api-gateway and order-service. Pulling the gateway's 5xx and slow "
                    "requests, the order-service warnings and restarts, and the deploy history.")
    if not any("runbooks" in json.dumps(c.input) for c in calls):
        facts = _incident_facts(calls)
        culprit = facts.get("culprit") or {}
        return use_tools(
            tool("Grep", pattern='"level": "WARN"', path="order-portal", glob="{payment,inventory}-service.log",
                 output_mode="content"),
            tool("Grep", pattern="wp-admin", path="order-portal/api-gateway.log", output_mode="content"),
            tool("Read", file_path="runbooks/db_connection_pool_exhaustion.md"),
            preface=f"{facts.get('count_5xx', 0)} x 503 from {', '.join(facts.get('upstreams', ['?']))} start minutes "
                    f"after deploy {culprit.get('deploy_id', '?')} ({culprit.get('config_diff') or 'no config diff'}). "
                    "Before concluding, checking the other services' warnings, the bot traffic and the pool runbook.")
    facts = _incident_facts(calls)
    markdown, report = _incident_report(facts, _red_herrings(calls), runbook_seen=True)
    structured = next((n for n in req.tool_names if n == "StructuredOutput"), None)
    if structured:
        return use_tools(tool(structured, **report), preface=markdown)
    return say(markdown, complexity=0.6)


# ------------------------------------------------------------------------------- lab 06: guarded agent with custom tools
@scenario("day5.sre_guarded", priority=50,
          match=lambda r: _has_marker(r, "KESTREL-SRE-GUARDED") and r.has_tool("mcp__sre__get_deploys"))
def sre_guarded(req: MockRequest) -> Reply:
    calls = _turn_calls(req)
    if not calls:
        return use_tools(
            tool("Bash", command="grep -c '\"status\": 503' order-portal/api-gateway.log"),
            tool("mcp__sre__get_deploys", service="order-service"),
            tool("Grep", pattern='"level": "(ERROR|WARN)"|starting order-service',
                 path="order-portal/order-service.log", output_mode="content"),
            preface="Counting the 503s, and pulling order-service's deploy history and warnings.")
    if not any(c.name == "Glob" for c in calls):
        bash_denied = any(c.name == "Bash" and c.is_error for c in calls)
        deploys = _deploy_rows(calls)
        suspect = next((d for d in deploys if d.get("config_diff")), {})
        return use_tools(
            tool("Grep", pattern='"status": 50\\d|"latency_ms": \\d{4,}', path="order-portal/api-gateway.log",
                 output_mode="content"),
            tool("Glob", pattern="**/values*.yaml", path="../.."),
            preface=("Bash is blocked for this agent, so I'll use Grep for the 5xx lines. " if bash_denied else "")
                    + f"Deploy {suspect.get('deploy_id', '?')} changed '{suspect.get('config_diff', '?')}'; "
                      "looking for the Helm values file to confirm the configured pool size.")
    if not _calls(calls, "read_runbook"):
        return use_tools(_call("mcp__sre__read_runbook", {"name": "db_connection_pool_exhaustion"}),
                         preface="The Helm values are outside my workspace (the guardrail refused the search), so the "
                                 "deploy config diff is my evidence for the pool size. Checking the runbook.")
    facts = _incident_facts(calls)
    markdown, _ = _incident_report(facts, [], runbook_seen=True)
    denied = [c for c in calls if c.is_error and "hook" in (c.result or "").lower()]
    if denied:
        markdown += "\n\n**Guardrails.** " + "; ".join(
            f"{c.name} was refused ({(c.result or '').split(':', 2)[-1].strip()[:110]})" for c in denied)
    return say(markdown, complexity=0.5)


# ------------------------------------------------------------------------------- lab 07: coordinator + subagents
def _subagent_reports(calls: list[ToolCall]) -> dict[str, str]:
    reports = {}
    for c in calls:
        if c.name in ("Agent", "Task") and c.result:
            reports[c.input.get("subagent_type", "?")] = c.result
    return reports


def _facts_from_text(text: str) -> dict:
    deploy = re.search(r"\b(D-\d{4})\b[^\n]{0,240}?(db\.pool\.max:?\s*\d+\s*->\s*\d+)", text)
    rollback = re.search(r"\b(D-\d{4})\b[^.\n]*?rollback|rollback[^.\n]*?\b(D-\d{4})\b", text, re.I)
    first = re.search(r"first 503[^0-9]*(\d{2}:\d{2}:\d{2})|(\d{2}:\d{2}:\d{2}) first 503", text)
    last = re.search(r"last 503[^0-9]*(\d{2}:\d{2}:\d{2})|(\d{2}:\d{2}:\d{2}) last 503", text)
    count = re.search(r"(\d+)\s*x\s*503", text)
    slow = re.search(r"(\d{2}:\d{2}:\d{2}) first slow", text)
    return {"deploy": deploy.group(1) if deploy else None, "diff": deploy.group(2).strip() if deploy else None,
            "rollback": next((g for g in (rollback.groups() if rollback else ()) if g), None),
            "first_503": next((g for g in (first.groups() if first else ()) if g), None),
            "last_503": next((g for g in (last.groups() if last else ()) if g), None),
            "count": count.group(1) if count else None, "first_slow": slow.group(1) if slow else None}


@scenario("day5.sre_coordinator", priority=50,
          match=lambda r: _has_marker(r, "KESTREL-SRE-COORDINATOR") and r.has_tool("Agent", "Task"))
def sre_coordinator(req: MockRequest) -> Reply:
    humans = _human_texts(req)
    latest = humans[-1] if humans else ""
    everything = " ".join(c.result or "" for c in req.tool_calls)
    facts = _facts_from_text(everything)

    if len(humans) > 1 and not _ends_with_tool_results(req):
        # A follow-up turn: answer from what is already in the conversation - no new tool calls.
        if re.search(r"status", latest, re.I):
            return say(
                "**Kestrel Connect - resolved incident (2026-09-14)**\n\n"
                f"Between {facts['first_slow'] or facts['first_503']} and {facts['last_503']} UTC, some requests to view "
                "or place orders and to track shipments in Kestrel Connect were slow or failed with an error. "
                "The cause was a configuration change in a routine update of our order service, which we reversed at "
                f"{facts['last_503'][:5] if facts['last_503'] else 'about 10:08'} UTC. No orders or payments were lost; "
                "if an order submission failed during this window, please submit it again. We are adding safeguards "
                "so that this kind of configuration change is caught before release.", complexity=0.3)
        return say(f"Deploy {facts['deploy']} changed {facts['diff']} for order-service, so its database connection "
                   "pool saturated and requests timed out into 503s until the rollback"
                   + (f" ({facts['rollback']})" if facts["rollback"] else "") + ".", complexity=0.2)

    calls = _turn_calls(req)
    reports = _subagent_reports(calls)
    if not reports:
        return use_tools(
            tool("Agent", subagent_type="log-analyst", description="Build 503 timeline from logs",
                 prompt="Incident on 2026-09-14 in Kestrel Connect. Using order-portal/api-gateway.log and "
                        "order-portal/order-service.log, build a UTC timeline: first slow request, first and last 503, "
                        "count of 503s and affected upstream, and order-service pool warnings/errors (pool_active, "
                        "pool_max, waiting) and restarts (version, db_pool_max). Report numbers, not prose.",
                 run_in_background=False),
            tool("Agent", subagent_type="runbook-checker", description="Correlate deploys with runbooks",
                 prompt="Incident on 2026-09-14: order-service returned 503s. Check the order-service deploy history "
                        "and the relevant runbooks. Which deploy and config change is the most likely trigger, what "
                        "do the runbooks prescribe, and was it followed?",
                 run_in_background=False),
            preface="Delegating in parallel: the log analyst builds the timeline, the runbook checker correlates "
                    "deploys with our runbooks.")
    log_report = reports.get("log-analyst", "")
    rb_report = reports.get("runbook-checker", "")
    return say(
        "## Incident synthesis (coordinator)\n\n"
        f"**Root cause.** Deploy {facts['deploy']} changed {facts['diff']} for order-service. With only 5 connections the "
        "pool saturated and requests waited 3000 ms for a connection, then failed as 503 at the gateway.\n\n"
        f"**Impact.** {facts['count'] or '?'} x 503 between {facts['first_503']} and {facts['last_503']} UTC; latency "
        f"degraded from {facts['first_slow']}.\n\n"
        f"**Mitigation.** Rollback {facts['rollback'] or '(see deploy log)'} restored the pool size; the runbooks "
        "(RB-01 roll back first, RB-02 compare pool_max before/after) match what on-call did.\n\n"
        f"**Sources.** log-analyst report ({len(log_report)} chars), runbook-checker report ({len(rb_report)} chars). "
        "I did not read raw logs myself.", complexity=0.5)


@scenario("day5.log_analyst", priority=50,
          match=lambda r: _has_marker(r, "KESTREL-LOG-ANALYST") and r.has_tool("Grep"))
def log_analyst(req: MockRequest) -> Reply:
    calls = _turn_calls(req)
    if not calls:
        return use_tools(
            tool("Grep", pattern='"status": 5\\d\\d|"latency_ms": \\d{4,}', path="order-portal/api-gateway.log",
                 output_mode="content"),
            tool("Grep", pattern='"level": "(ERROR|WARN)"|starting order-service', path="order-portal/order-service.log",
                 output_mode="content"))
    f = _incident_facts(calls)
    if not f.get("first_5xx"):
        return say("No 5xx cluster found in api-gateway.log.")
    lines = [f"Timeline (UTC, 2026-09-14) from {len(_gateway_rows(calls))} gateway lines and "
             f"{len(_service_rows(calls, 'order-service'))} order-service lines:"]
    for when, version, pool_max in f["restarts"]:
        lines.append(f"- {_hm(when)} order-service v{version} starts with db_pool_max={pool_max}")
    if f["first_capacity_warn"]:
        lines.append(f"- {_hm(f['first_capacity_warn'])} WARN connection pool at capacity")
    lines += [f"- {_hm(f['first_slow'])} first slow request ({f['first_slow_ms']} ms); {f['slow_count']} requests over 1 s",
              f"- {_hm(f['first_5xx'])} first 503 (upstream {', '.join(f['upstreams'])})",
              f"- {_hm(f['last_5xx'])} last 503",
              f"Totals: {f['count_5xx']} x 503, p50 latency {f['latency_p50']} ms; {f['timeouts']} order-service "
              f"'timeout acquiring DB connection' errors at pool {f['timeout_pool'][0]}/{f['timeout_pool'][1]}, up to "
              f"{f['max_waiting']} requests waiting. {f['baseline_5xx']} other 5xx in 48 h are isolated."]
    return say("\n".join(lines), complexity=0.4)


@scenario("day5.runbook_checker", priority=50,
          match=lambda r: _has_marker(r, "KESTREL-RUNBOOK-CHECKER") and r.has_tool("mcp__sre__read_runbook"))
def runbook_checker(req: MockRequest) -> Reply:
    calls = _turn_calls(req)
    if not calls:
        return use_tools(tool("mcp__sre__get_deploys", service="order-service"),
                         _call("mcp__sre__read_runbook", {"name": "high_5xx_rate"}))
    deploys = _deploy_rows(calls)
    suspect = next((d for d in deploys if "pool" in (d.get("config_diff") or "")), None)
    if suspect and not any((c.input or {}).get("name") == "db_connection_pool_exhaustion" for c in calls):
        return use_tools(_call("mcp__sre__read_runbook", {"name": "db_connection_pool_exhaustion"}),
                         _call("mcp__sre__read_runbook", {"name": "deploy_rollback"}))
    rollback = next((d for d in deploys if re.search(r"rollback", d.get("change_summary", ""), re.I)), None)
    books = {c.input.get("name"): (c.result or "") for c in _calls(calls, "read_runbook") if not c.is_error}
    lines = []
    if suspect:
        lines.append(f"Most likely trigger: {suspect['deploy_id']} (order-service v{suspect['version']}, "
                     f"{_hm(suspect['started_at'])}-{_hm(suspect['finished_at'])} UTC, '{suspect['change_summary']}') "
                     f"with config diff {suspect['config_diff']}.")
    if "production value is **50**" in books.get("db_connection_pool_exhaustion", ""):
        lines.append("RB-02 says order-service's production pool size is 50 per instance, so 5 is a config regression; "
                     "prescribed mitigation: roll back the deploy (RB-04) or hot-fix the value.")
    if "roll back first" in books.get("high_5xx_rate", "").lower():
        lines.append("RB-01: when a deploy correlates with the start of errors, roll back first, debug later.")
    if rollback:
        lines.append(f"Followed: rollback {rollback['deploy_id']} to v{rollback['version']} ran "
                     f"{_hm(rollback['started_at'])}-{_hm(rollback['finished_at'])} UTC ({rollback['config_diff']}).")
    if "Freeze further deploys" in books.get("deploy_rollback", ""):
        lines.append("Still to do per RB-04: freeze order-service deploys until the post-incident review.")
    return say("\n".join(lines) or "No deploy or runbook evidence found.", complexity=0.4)


# ------------------------------------------------------------------------------- exercise 11: secret-guard hook
@scenario("day5.secret_guard", priority=50,
          match=lambda r: _has_marker(r, "KESTREL-SECRET-GUARD") and r.has_tool("Read", "Glob"))
def secret_guard(req: MockRequest) -> Reply:
    calls = _turn_calls(req)
    if not calls:
        return use_tools(tool("Glob", pattern="**/*"), preface="Listing the folder first.")
    listing = _last(calls, "Glob")
    files = [line.strip() for line in (listing.result or "").splitlines()
             if line.strip() and not line.startswith(("Found", "No files"))][:6]
    if not _calls(calls, "Read"):
        return use_tools(*[tool("Read", file_path=f) for f in files], preface=f"Reading the {len(files)} files.")
    if not _calls(calls, "Grep"):
        return use_tools(tool("Grep", pattern="PASSWORD|password|token", path=".", output_mode="content"),
                         preface="Checking whether any file mentions credentials that need rotating.")
    read_ok = [c for c in _calls(calls, "Read") if not c.is_error]
    withheld = [c for c in calls if c.is_error]
    lines = ["**Catch-up for the next shift**"]
    for c in read_ok:
        body = [l.strip() for l in _read_body(c.result).splitlines() if l.strip() and not l.startswith("#")]
        lines.append(f"- {c.input.get('file_path')}: " + "; ".join(body[:3]))
    for c in withheld:
        target = c.input.get("file_path") or f"search '{c.input.get('pattern')}' in {c.input.get('path')}"
        lines.append(f"- Withheld: {target} ({(c.result or '').split(':', 2)[-1].strip()[:120]})")
    return say("\n".join(lines), complexity=0.3)
