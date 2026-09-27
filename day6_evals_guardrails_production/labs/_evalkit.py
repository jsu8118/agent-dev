"""_evalkit - a small, dependency-free evaluation harness for Kestrel's support agent.

Built on Day 6 (lab 02) and reused by the Day 6 solutions and the Day 7 capstone:

    import _evalkit as ek                      # labs/ is on sys.path when you run a lab script
    scenarios = ek.load_scenarios(limit=10)
    report = ek.run_eval(get_client(), scenarios, run_name="baseline")
    ek.print_summary(report)
    json_path, md_path = ek.write_reports(report)

Design choices (each one maps to an item of the eval-health checklist):
  * The harness calls the REAL entry point (kestrel.support_agent.run_support_agent), never a
    re-implementation, so it measures what production runs.
  * Every (case, rep) gets a fresh scratch copy of the ops database: no state leaks between cases,
    and graders can read the END STATE (refund rows, RMAs, escalations), not just the transcript.
  * Code graders only.  Checks are atomic (one property each) and flagged `critical` when a failure
    would be unacceptable even once (an unauthorised refund, a forbidden tool call, a safety miss).
  * Infra failures (API errors after retries) are recorded as status="error" and excluded from the
    pass rate; truncated replies are status="truncated".  Neither is scored as a model failure.
  * Cost is metered per case from the API's `usage` (a per-case UsageLedger), latency is wall-clock.
  * Every case writes a full trajectory (traces/<id>_rep<k>.json) so a surprising score can be audited.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import hashlib
import json
import math
import sqlite3
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import anthropic

from kestrel.support_agent import SYSTEM_PROMPT, AgentResult, run_support_agent
from kestrel.support_tools import SupportDesk
from labkit import DATA_DIR, LEDGER, MODEL, runs_dir
from labkit.data import ops_db, scratch_db
from labkit.metering import UsageLedger, UsageMeter

EVAL_SET = DATA_DIR / "evals" / "support_eval_set.jsonl"
WRITE_OUTCOMES = ("refund_issued", "rma_created")       # irreversible: never acceptable when not expected
HIGH_STAKES_TYPES = ("safety", "security", "privacy")


# ============================================================================================ data
@dataclass(frozen=True)
class Scenario:
    id: str
    type: str
    from_email: str
    message: str
    expect: dict
    facts: dict = field(default_factory=dict)


def load_scenarios(path: Path = EVAL_SET, *, ids: Iterable[str] | None = None, types: Iterable[str] | None = None,
                   limit: int | None = None) -> list[Scenario]:
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    wanted_ids = {i.strip().upper() for i in ids} if ids else None
    wanted_types = set(types) if types else None
    out = [Scenario(r["id"], r["type"], r["from_email"], r["message"], r["expect"], r.get("facts") or {})
           for r in rows
           if (wanted_ids is None or r["id"] in wanted_ids) and (wanted_types is None or r["type"] in wanted_types)]
    return out[:limit] if limit else out


def prompt_version(text: str) -> str:
    """A content hash: two prompts with the same version string are byte-identical."""
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:10]


# ============================================================================================ observations
@dataclass
class ToolEvent:
    name: str
    input: dict
    output: Any              # parsed JSON when possible
    is_error: bool

    @property
    def ok(self) -> bool:
        return not self.is_error


def _block_get(block: Any, key: str, default: Any = None) -> Any:
    return block.get(key, default) if isinstance(block, dict) else getattr(block, key, default)


def tool_events(messages: list[dict]) -> list[ToolEvent]:
    """Pair every tool_use in the transcript with its tool_result (the agent's trajectory)."""
    pending: dict[str, ToolEvent] = {}
    events: list[ToolEvent] = []
    for message in messages:
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            kind = _block_get(block, "type")
            if message.get("role") == "assistant" and kind == "tool_use":
                event = ToolEvent(_block_get(block, "name"), dict(_block_get(block, "input") or {}), None, False)
                pending[_block_get(block, "id")] = event
                events.append(event)
            elif message.get("role") == "user" and kind == "tool_result":
                event = pending.get(_block_get(block, "tool_use_id"))
                if event is not None:
                    raw = _block_get(block, "content")
                    try:
                        event.output = json.loads(raw) if isinstance(raw, str) else raw
                    except ValueError:
                        event.output = raw
                    event.is_error = bool(_block_get(block, "is_error"))
    return events


def _original_rmas() -> set[str]:
    with ops_db() as conn:
        return {row[0] for row in conn.execute("SELECT rma_id FROM rmas")}


ORIGINAL_RMAS = _original_rmas()


@dataclass
class EndState:
    """What the run left behind in the (scratch) system of record."""
    refunds: list[dict]
    rmas_created: list[dict]
    escalations: list[dict]
    audit_rows: list[dict]


def end_state(db: sqlite3.Connection) -> EndState:
    def rows(sql: str) -> list[dict]:
        cursor = db.execute(sql)
        names = [d[0] for d in cursor.description]
        return [dict(zip(names, r)) for r in cursor.fetchall()]
    rmas = [r for r in rows("SELECT * FROM rmas") if r["rma_id"] not in ORIGINAL_RMAS]
    return EndState(rows("SELECT * FROM refunds"), rmas, rows("SELECT * FROM escalations"),
                    rows("SELECT * FROM audit_log"))


def infer_outcome(events: list[ToolEvent], state: EndState) -> str:
    """Outcome from the END STATE first (writes), then from the trajectory (read-only outcomes)."""
    if state.refunds:
        return "refund_issued"
    if state.rmas_created:
        return "rma_created"
    if any(e["queue"] == "security" for e in state.escalations):
        return "flagged"
    if state.escalations:
        return "escalated"
    if not events:
        return "declined"            # no lookups at all: an out-of-scope decline or a clarifying question
    for e in events:
        if e.ok and e.name in ("check_return_eligibility", "check_warranty") and isinstance(e.output, dict) \
                and e.output.get("eligible") is False:
            return "declined"
    unverified = any(e.ok and e.name == "get_customer_profile" and isinstance(e.output, dict)
                     and e.output.get("verified") is False for e in events) or \
        any(e.is_error and "not verified" in json.dumps(e.output) for e in events)
    account_read = any(e.ok and e.name in ("get_order", "get_invoice", "get_rma", "list_customer_orders")
                       for e in events)
    if unverified and not account_read:
        return "needs_verification"
    return "info_only"


# ============================================================================================ graders
@dataclass
class Check:
    name: str
    passed: bool
    detail: str = ""
    critical: bool = False       # a failure here is unacceptable even once (gates releases on its own)


@dataclass
class Observation:
    """Everything a grader may look at: the case, the reply, the trajectory and the end state."""
    scenario: Scenario
    reply: str
    events: list[ToolEvent]
    state: EndState
    outcome: str


Grader = Callable[[Observation], list[Check]]


def alternatives(phrase: str) -> list[str]:
    return [p.strip() for p in phrase.split("|") if p.strip()]


def contains_any(text: str, phrase: str) -> bool:
    low = text.lower()
    return any(alt.lower() in low for alt in alternatives(phrase))


def grade_tools_required(obs: Observation) -> list[Check]:
    called = {e.name for e in obs.events}
    return [Check(f"calls {tool}", tool in called, "" if tool in called else "never called")
            for tool in obs.scenario.expect.get("tools_required", [])]


def grade_tools_forbidden(obs: Observation) -> list[Check]:
    checks = []
    for tool in obs.scenario.expect.get("tools_forbidden", []):
        attempts = [e for e in obs.events if e.name == tool]
        # An ATTEMPT is a failure even if the tool refused: the model tried something it must not do,
        # and only a lower layer saved us.  Evals should see that; production logs should too.
        detail = "" if not attempts else (
            f"attempted {len(attempts)}x" + (" (tool refused)" if all(e.is_error for e in attempts) else ""))
        checks.append(Check(f"never calls {tool}", not attempts, detail, critical=True))
    return checks


def grade_outcome(obs: Observation) -> list[Check]:
    expected = obs.scenario.expect.get("outcome")
    if not expected:
        return []
    ok = obs.outcome in alternatives(expected)
    critical = not ok and obs.outcome in WRITE_OUTCOMES          # e.g. an unauthorised refund or RMA
    return [Check("outcome", ok, "" if ok else f"expected {expected}, got {obs.outcome}", critical=critical)]


def grade_escalation(obs: Observation) -> list[Check]:
    expect, checks = obs.scenario.expect, []
    high_stakes = obs.scenario.type in HIGH_STAKES_TYPES
    if expect.get("escalation_queue"):
        queues = [e["queue"] for e in obs.state.escalations]
        ok = expect["escalation_queue"] in queues
        checks.append(Check(f"escalates to {expect['escalation_queue']}", ok,
                            "" if ok else f"queues used: {queues or 'none'}", critical=high_stakes))
    if expect.get("escalation_priority"):
        prios = [e["priority"] for e in obs.state.escalations]
        ok = expect["escalation_priority"] in prios
        checks.append(Check(f"priority {expect['escalation_priority']}", ok,
                            "" if ok else f"priorities used: {prios or 'none'}", critical=high_stakes))
    return checks


def grade_must_include(obs: Observation) -> list[Check]:
    # For safety cases the required sentences ARE the safety procedure (isolate, 1-hour response).
    critical = obs.scenario.type == "safety"
    return [Check(f"says '{phrase}'", contains_any(obs.reply, phrase), "" if contains_any(obs.reply, phrase)
                  else "missing from reply", critical=critical)
            for phrase in obs.scenario.expect.get("must_include", [])]


def grade_must_not_include(obs: Observation) -> list[Check]:
    checks = []
    for phrase in obs.scenario.expect.get("must_not_include", []):
        hit = next((alt for alt in alternatives(phrase) if alt.lower() in obs.reply.lower()), None)
        checks.append(Check(f"never says '{phrase}'", hit is None, f"reply contains '{hit}'" if hit else "",
                            critical=True))
    return checks


DEFAULT_GRADERS: list[Grader] = [grade_tools_required, grade_tools_forbidden, grade_outcome, grade_escalation,
                                 grade_must_include, grade_must_not_include]


# ============================================================================================ running
@dataclass
class CaseRun:
    scenario_id: str
    type: str
    rep: int
    status: str                          # ok | truncated | error
    model: str = ""
    reply: str = ""
    outcome: str = ""
    expected_outcome: str = ""
    checks: list[Check] = field(default_factory=list)
    tools: list[str] = field(default_factory=list)       # "name" or "name!" when the call errored
    turns: int = 0
    stop_reason: str = ""
    llm_calls: int = 0
    input_tokens: int = 0
    cache_write_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    latency_s: float = 0.0
    escalated: bool = False
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.status == "ok" and all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> list[Check]:
        return [c for c in self.checks if not c.passed]

    @property
    def critical_failures(self) -> list[Check]:
        return [c for c in self.checks if c.critical and not c.passed]


def _failure_class(exc: BaseException) -> str:
    for cls, name in ((anthropic.RateLimitError, "rate_limited"), (anthropic.OverloadedError, "overloaded"),
                      (anthropic.InternalServerError, "server_error"), (anthropic.APITimeoutError, "timeout"),
                      (anthropic.APIConnectionError, "connection"), (anthropic.BadRequestError, "bad_request"),
                      (anthropic.APIStatusError, "api_error")):
        if isinstance(exc, cls):
            return name
    return "harness"


def _trace_turns(system_prompt: str, messages: list[dict]) -> list[dict]:
    """The transcript in the portable {role, content} shape used by eval report viewers."""
    turns: list[dict] = [{"role": "system", "content": system_prompt}]
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            turns.append({"role": message["role"], "content": content})
            continue
        for block in content or []:
            kind = _block_get(block, "type")
            if kind == "text":
                turns.append({"role": "assistant", "content": _block_get(block, "text")})
            elif kind == "tool_use":
                turns.append({"role": "tool_call", "name": _block_get(block, "name"),
                              "content": json.dumps(_block_get(block, "input"), indent=2, ensure_ascii=False)})
            elif kind == "tool_result":
                turns.append({"role": "tool_result", "content": _block_get(block, "content"),
                              "is_error": bool(_block_get(block, "is_error"))})
    return turns


def metered(client: anthropic.Anthropic, ledger: UsageLedger, extra_middleware: Iterable[Any] = ()) -> anthropic.Anthropic:
    """A copy of `client` that also meters into `ledger` (the process-wide LEDGER keeps working)."""
    return client.with_options(middleware=[UsageMeter(LEDGER), UsageMeter(ledger), *extra_middleware])


def run_case(client: anthropic.Anthropic, scenario: Scenario, *, rep: int = 0, model: str = MODEL,
             system_prompt: str = SYSTEM_PROMPT, run_name: str = "adhoc", graders: list[Grader] | None = None,
             max_turns: int = 12, desk_factory: Callable[[Scenario, sqlite3.Connection], SupportDesk] | None = None,
             extra_middleware: Iterable[Any] = (), trace_dir: Path | None = None, keep_db: bool = False) -> CaseRun:
    """Run the real agent on one scenario in an isolated sandbox and grade the result."""
    db_name = f"day6_{run_name}_{scenario.id}_r{rep}.db"
    db = scratch_db(db_name)
    desk = desk_factory(scenario, db) if desk_factory else SupportDesk(scenario.from_email, db=db,
                                                                       ticket_ref=f"{scenario.id}-r{rep}")
    ledger = UsageLedger()
    case = CaseRun(scenario.id, scenario.type, rep, "ok", model=model,
                   expected_outcome=scenario.expect.get("outcome", ""))
    started = time.perf_counter()
    try:
        result: AgentResult = run_support_agent(metered(client, ledger, extra_middleware), scenario.message,
                                                scenario.from_email, model=model, desk=desk, max_turns=max_turns,
                                                system_prompt=system_prompt, ticket_ref=f"{scenario.id}-r{rep}")
    except Exception as exc:   # infra failures are data, not crashes: record the class and move on
        case.status, case.error = "error", f"{_failure_class(exc)}: {type(exc).__name__}: {str(exc)[:200]}"
        result = None
    case.latency_s = time.perf_counter() - started
    totals = list(ledger.by_model.values())
    case.llm_calls = sum(t.calls for t in totals)
    case.input_tokens = sum(t.input_tokens for t in totals)
    case.cache_write_tokens = sum(t.cache_write_tokens for t in totals)
    case.cache_read_tokens = sum(t.cache_read_tokens for t in totals)
    case.output_tokens = sum(t.output_tokens for t in totals)
    case.cost_usd = ledger.total_cost
    if result is not None:
        events = tool_events(result.messages)
        state = end_state(db)
        case.reply, case.turns, case.stop_reason = result.reply, result.turns, result.stop_reason
        case.escalated = result.escalated
        case.tools = [e.name + ("!" if e.is_error else "") for e in events]
        case.outcome = infer_outcome(events, state)
        if result.stop_reason == "max_tokens":
            case.status = "truncated"
        obs = Observation(scenario, result.reply, events, state, case.outcome)
        case.checks = [c for grader in (graders or DEFAULT_GRADERS) for c in grader(obs)]
        if trace_dir is not None:
            trace_dir.mkdir(parents=True, exist_ok=True)
            (trace_dir / f"{scenario.id}_rep{rep}.json").write_text(
                json.dumps(_trace_turns(system_prompt, result.messages), indent=1, ensure_ascii=False, default=str),
                encoding="utf-8")
    db.close()
    if not keep_db:
        (runs_dir("db") / db_name).unlink(missing_ok=True)
    return case


@dataclass
class EvalReport:
    run_name: str
    model: str
    prompt_version: str
    reps: int
    cases: list[CaseRun]
    wall_s: float
    created_at: str
    notes: dict = field(default_factory=dict)

    def by_scenario(self) -> dict[str, list[CaseRun]]:
        out: dict[str, list[CaseRun]] = {}
        for case in self.cases:
            out.setdefault(case.scenario_id, []).append(case)
        return out

    def summary(self) -> dict:
        return summarize(self.cases, self.reps)


def run_eval(client: anthropic.Anthropic, scenarios: list[Scenario], *, reps: int = 1, concurrency: int = 4,
             model: str = MODEL, system_prompt: str = SYSTEM_PROMPT, run_name: str = "baseline",
             graders: list[Grader] | None = None, report_dir: Path | None = None, **case_kwargs: Any) -> EvalReport:
    """Run every (scenario, rep) concurrently (bounded) and collect graded CaseRuns in a stable order."""
    report_dir = report_dir or runs_dir("day6_reports", run_name)
    jobs = [(s, r) for s in scenarios for r in range(reps)]
    started = time.perf_counter()
    results: dict[tuple[str, int], CaseRun] = {}
    progress = sys.stderr.isatty()

    def run(job: tuple[Scenario, int]) -> CaseRun:
        s, r = job
        return run_case(client, s, rep=r, model=model, system_prompt=system_prompt, run_name=run_name,
                        graders=graders, trace_dir=report_dir / "traces", **case_kwargs)

    # Warm-up: run one case alone so the shared prefix (tools + system prompt) is written to the prompt
    # cache ONCE; the concurrent cases that follow then read it instead of all paying the cache write.
    if jobs:
        results[(jobs[0][0].id, jobs[0][1])] = run(jobs[0])
    with cf.ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        futures = {pool.submit(run, job): (job[0].id, job[1]) for job in jobs[1:]}
        for done, future in enumerate(cf.as_completed(futures), 2):
            results[futures[future]] = future.result()
            if progress:
                print(f"\r  [{done}/{len(jobs)}] cases finished", end="", file=sys.stderr, flush=True)
    if progress:
        print(file=sys.stderr)
    ordered = [results[(s.id, r)] for s, r in jobs]
    return EvalReport(run_name, model, prompt_version(system_prompt), reps, ordered, time.perf_counter() - started,
                      dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"))


# ============================================================================================ statistics
def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion - sane at 0% and 100%, unlike the normal approximation."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def percentile(values: list[float], q: float) -> float:
    """Linear-interpolated percentile (q in 0..100)."""
    if not values:
        return 0.0
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def pass_at_k(n: int, c: int, k: int) -> float:
    """P(at least one of k samples passes), unbiased estimate from c passes in n trials."""
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """pass^k: P(all k samples pass), unbiased estimate from c passes in n trials."""
    return math.comb(c, k) / math.comb(n, k) if k <= n else 0.0


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pair counts (b: pass->fail, c: fail->pass)."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def cohens_kappa(a: list[Any], b: list[Any]) -> float:
    """Agreement beyond chance between two raters on the same items (1 = perfect, 0 = chance)."""
    n = len(a)
    labels = sorted(set(a) | set(b), key=str)
    observed = sum(x == y for x, y in zip(a, b)) / n
    expected = sum((a.count(lbl) / n) * (b.count(lbl) / n) for lbl in labels)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def confusion(truth: list[Any], pred: list[Any], labels: list[Any]) -> list[list[int]]:
    index = {lbl: i for i, lbl in enumerate(labels)}
    matrix = [[0] * len(labels) for _ in labels]
    for t, p in zip(truth, pred):
        matrix[index[t]][index[p]] += 1
    return matrix


def print_confusion(matrix: list[list[int]], labels: list[Any], *, row_title: str = "truth", col_title: str = "pred",
                    width: int = 7) -> None:
    title = f"{row_title} \\ {col_title}"
    print(f"  {title:<16}" + "".join(f"{str(lbl):>{width}}" for lbl in labels))
    for lbl, row in zip(labels, matrix):
        print(f"  {str(lbl):<16}" + "".join(f"{v:>{width}}" for v in row))


def pct(x: float) -> str:
    return f"{100 * x:.1f}%"


# ============================================================================================ reporting
def summarize(cases: list[CaseRun], reps: int = 1) -> dict:
    ok = [c for c in cases if c.status == "ok"]
    passed = [c for c in ok if c.passed]
    by_type: dict[str, dict] = {}
    for case in ok:
        row = by_type.setdefault(case.type, {"n": 0, "passed": 0})
        row["n"] += 1
        row["passed"] += case.passed
    for row in by_type.values():
        row["rate"] = row["passed"] / row["n"]
        row["ci95"] = wilson(row["passed"], row["n"])
    costs = [c.cost_usd for c in cases]
    lat = [c.latency_s for c in cases]
    resolved = [c for c in ok if not c.escalated]
    prompt_tokens = sum(c.input_tokens + c.cache_write_tokens + c.cache_read_tokens for c in cases)
    summary = {
        "runs": len(cases), "ok": len(ok), "errors": sum(c.status == "error" for c in cases),
        "truncated": sum(c.status == "truncated" for c in cases), "passed": len(passed),
        "pass_rate": len(passed) / len(ok) if ok else 0.0, "ci95": wilson(len(passed), len(ok)),
        "critical_failures": sum(len(c.critical_failures) for c in ok),
        "by_type": dict(sorted(by_type.items())),
        "cost_usd": {"total": sum(costs), "mean": sum(costs) / len(costs) if costs else 0.0,
                     "p50": percentile(costs, 50), "p95": percentile(costs, 95), "max": max(costs, default=0.0)},
        "latency_s": {"mean": sum(lat) / len(lat) if lat else 0.0, "p50": percentile(lat, 50),
                      "p95": percentile(lat, 95), "max": max(lat, default=0.0)},
        "turns_mean": sum(c.turns for c in ok) / len(ok) if ok else 0.0,
        "escalation_rate": sum(c.escalated for c in ok) / len(ok) if ok else 0.0,
        "cost_per_resolved_usd": sum(costs) / len(resolved) if resolved else None,
        "cache_read_share": sum(c.cache_read_tokens for c in cases) / prompt_tokens if prompt_tokens else 0.0,
    }
    if reps > 1:
        per_scenario: dict[str, list[bool]] = {}
        for c in cases:
            per_scenario.setdefault(c.scenario_id, []).append(c.passed)
        n = reps
        summary["pass_at_k"] = sum(pass_at_k(n, sum(v), reps) for v in per_scenario.values()) / len(per_scenario)
        summary["pass_hat_k"] = sum(pass_hat_k(n, sum(v), reps) for v in per_scenario.values()) / len(per_scenario)
        summary["flaky_scenarios"] = sorted(k for k, v in per_scenario.items() if 0 < sum(v) < len(v))
    return summary


def print_case_line(case: CaseRun) -> None:
    mark = {"ok": "PASS" if case.passed else "FAIL", "error": "ERROR", "truncated": "TRUNC"}[case.status]
    rep = f".r{case.rep}" if case.rep else ""
    print(f"  {case.scenario_id + rep:<8}{case.type:<13}{mark:<6}{case.outcome:<19}{case.turns:>2} turns  "
          f"${case.cost_usd:.4f}  {case.latency_s:6.2f}s  {' '.join(case.tools)[:60]}")
    for check in case.failed_checks:
        flag = "CRITICAL " if check.critical else ""
        print(f"            - {flag}{check.name}: {check.detail}")
    if case.error:
        print(f"            - {case.error}")


def print_summary(report: EvalReport) -> dict:
    s = report.summary()
    lo, hi = s["ci95"]
    print(f"  run={report.run_name}  model={report.model}  prompt={report.prompt_version}  reps={report.reps}  "
          f"wall={report.wall_s:.1f}s")
    print(f"  passed {s['passed']}/{s['ok']} = {pct(s['pass_rate'])}  (95% CI {pct(lo)}-{pct(hi)})   "
          f"critical failures: {s['critical_failures']}   errors: {s['errors']}   truncated: {s['truncated']}")
    print(f"  {'type':<14}{'n':>3}{'pass':>6}{'rate':>9}   95% CI")
    for kind, row in s["by_type"].items():
        print(f"  {kind:<14}{row['n']:>3}{row['passed']:>6}{pct(row['rate']):>9}   "
              f"{pct(row['ci95'][0])}-{pct(row['ci95'][1])}")
    c, lat = s["cost_usd"], s["latency_s"]
    per_resolved = s["cost_per_resolved_usd"]
    print(f"  cost/case  mean ${c['mean']:.4f}  p50 ${c['p50']:.4f}  p95 ${c['p95']:.4f}  max ${c['max']:.4f}  "
          f"(total ${c['total']:.4f}, cache-read share of input {pct(s['cache_read_share'])})")
    print(f"  latency    mean {lat['mean']:.2f}s  p50 {lat['p50']:.2f}s  p95 {lat['p95']:.2f}s  max {lat['max']:.2f}s")
    print(f"  escalation rate {pct(s['escalation_rate'])}  mean turns {s['turns_mean']:.1f}  cost per resolved ticket "
          + (f"${per_resolved:.4f}" if per_resolved is not None else "n/a"))
    if "pass_at_k" in s:
        print(f"  pass@{report.reps} {pct(s['pass_at_k'])}   pass^{report.reps} {pct(s['pass_hat_k'])}   "
              f"flaky scenarios: {s['flaky_scenarios'] or 'none'}")
    return s


def _case_dict(case: CaseRun) -> dict:
    d = asdict(case)
    d["passed"] = case.passed
    return d


def write_reports(report: EvalReport, out_dir: Path | None = None) -> tuple[Path, Path]:
    out_dir = out_dir or runs_dir("day6_reports", report.run_name)
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = report.summary()
    payload = {"run_name": report.run_name, "model": report.model, "prompt_version": report.prompt_version,
               "reps": report.reps, "created_at": report.created_at, "wall_s": round(report.wall_s, 2),
               "notes": report.notes, "summary": summary, "cases": [_case_dict(c) for c in report.cases]}
    json_path = out_dir / "report.json"
    json_path.write_text(json.dumps(payload, indent=1, default=str), encoding="utf-8")

    lo, hi = summary["ci95"]
    lines = [f"# Eval report: {report.run_name}", "",
             f"* model `{report.model}`, prompt `{report.prompt_version}`, reps {report.reps}, created {report.created_at}",
             f"* **passed {summary['passed']}/{summary['ok']} ({pct(summary['pass_rate'])}, 95% CI {pct(lo)}-{pct(hi)})**, "
             f"critical failures {summary['critical_failures']}, errors {summary['errors']}, truncated {summary['truncated']}",
             f"* cost/case mean ${summary['cost_usd']['mean']:.4f} (p95 ${summary['cost_usd']['p95']:.4f}); "
             f"latency p50 {summary['latency_s']['p50']:.2f}s, p95 {summary['latency_s']['p95']:.2f}s", "",
             "| type | n | passed | rate | 95% CI |", "|---|---|---|---|---|"]
    for kind, row in summary["by_type"].items():
        lines.append(f"| {kind} | {row['n']} | {row['passed']} | {pct(row['rate'])} | "
                     f"{pct(row['ci95'][0])}-{pct(row['ci95'][1])} |")
    lines += ["", "| case | type | result | outcome | turns | cost | latency | failed checks |",
              "|---|---|---|---|---|---|---|---|"]
    for case in report.cases:
        result = case.status if case.status != "ok" else ("PASS" if case.passed else "FAIL")
        failed = "; ".join(("**CRITICAL** " if c.critical else "") + f"{c.name}: {c.detail}"
                           for c in case.failed_checks) or (case.error or "")
        lines.append(f"| {case.scenario_id}.r{case.rep} | {case.type} | {result} | {case.outcome} | {case.turns} | "
                     f"${case.cost_usd:.4f} | {case.latency_s:.2f}s | {failed.replace('|', '/')} |")
    md_path = out_dir / "report.md"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path


def load_report(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _scenario_passes(report: EvalReport | dict) -> dict[str, tuple[bool, list[str], str]]:
    """scenario -> (passed in every rep, critical failures, type)."""
    cases = report.cases if isinstance(report, EvalReport) else report["cases"]
    out: dict[str, tuple[bool, list[str], str]] = {}
    for c in cases:
        if isinstance(c, CaseRun):
            sid, ok, crit, kind = c.scenario_id, c.passed, [x.name for x in c.critical_failures], c.type
        else:
            sid, ok, kind = c["scenario_id"], c["passed"], c["type"]
            crit = [x["name"] for x in c["checks"] if x["critical"] and not x["passed"]]
        prev = out.get(sid, (True, [], kind))
        out[sid] = (prev[0] and ok, prev[1] + crit, kind)
    return out


def compare(base: EvalReport | dict, cand: EvalReport | dict) -> dict:
    """Paired comparison of two runs on the same scenarios, plus a release-gate verdict."""
    b, c = _scenario_passes(base), _scenario_passes(cand)
    shared = [sid for sid in b if sid in c]
    regressions = [sid for sid in shared if b[sid][0] and not c[sid][0]]
    fixes = [sid for sid in shared if not b[sid][0] and c[sid][0]]
    new_critical = {sid: c[sid][1] for sid in shared if c[sid][1] and not b[sid][1]}
    high_stakes = [sid for sid in regressions if c[sid][2] in HIGH_STAKES_TYPES]
    if new_critical or high_stakes:
        verdict = "BLOCK"
    elif regressions:
        verdict = "REVIEW"
    else:
        verdict = "PASS"

    def summ(r: EvalReport | dict) -> dict:
        return r.summary() if isinstance(r, EvalReport) else r["summary"]
    return {"n": len(shared), "regressions": regressions, "fixes": fixes, "new_critical": new_critical,
            "mcnemar_p": mcnemar_exact(len(regressions), len(fixes)), "verdict": verdict,
            "mean_cost_base": summ(base)["cost_usd"]["mean"], "mean_cost_cand": summ(cand)["cost_usd"]["mean"],
            "cache_share_base": summ(base).get("cache_read_share", 0.0),
            "cache_share_cand": summ(cand).get("cache_read_share", 0.0)}


def print_comparison(cmp: dict) -> None:
    print(f"  paired scenarios: {cmp['n']}   pass->fail: {cmp['regressions'] or 'none'}   "
          f"fail->pass: {cmp['fixes'] or 'none'}")
    print(f"  exact McNemar p-value on the discordant pairs: {cmp['mcnemar_p']:.3f}")
    for sid, names in cmp["new_critical"].items():
        print(f"  NEW CRITICAL FAILURE {sid}: {', '.join(names)}")
    base, cand = cmp["mean_cost_base"], cmp["mean_cost_cand"]
    delta = (cand - base) / base if base else 0.0
    print(f"  mean cost/case ${base:.4f} -> ${cand:.4f} ({delta:+.1%})")
    share_b, share_c = cmp["cache_share_base"], cmp["cache_share_cand"]
    if abs(share_c - share_b) > 0.03:
        print(f"  caution: cache-read share differs ({pct(share_b)} vs {pct(share_c)}), so part of the cost delta "
              "is prompt-cache state (run order), not the change itself")
    print(f"  release gate: {cmp['verdict']}")


def load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]

