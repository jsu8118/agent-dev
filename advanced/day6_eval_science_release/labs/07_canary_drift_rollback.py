"""Lab 07 - Canary, drift and rollback: sequential decisions, drift on the stream, a runbook, a migration audit.

Objective
    Analyse the haiku fast-path canary as a sequential experiment - after seeing why peeking at a fixed-sample
    test lies - and separate statistical evidence from guardrails; monitor the trace stream for drift with PSI and
    KS against a noise floor you measure instead of a rule of thumb; turn the findings into a rollback decision
    and a runbook written to disk, with the customers to contact named from the traces; and run the next
    migration's audit (Claude Opus 5 -> Claude Opus 5.5) as code: request-shape probes, default changes, thinking
    binding across a migration, a rollback and a prompt hotfix, prompt cruft, price and cache re-baselining.

Concepts
    canary vs concurrent and historical control, peeking and alpha inflation, Wald's SPRT and its expected sample
    size, guardrail metrics vs decision metrics, PSI (categorical and binned) with a bootstrap noise floor, KS as
    an effect size, input vs output drift, the change log, rollback triggers and runbooks, customer remediation
    from traces, model-migration audits (parameters, defaults, prompt cruft, thinking binding, cache and price).

Run
    python advanced/day6_eval_science_release/labs/07_canary_drift_rollback.py

What to observe
    * Step 1: testing after every trace at 5% raises false alarms several-fold; the SPRT keeps them near 5% and
      stops early when there is an effect.
    * Step 2: the canary's guardrails fire on 2026-09-13 and its SPRT crosses on the same day - on two events, so
      the verdict hinges on the H0 written down before the canary started.
    * Step 3: the ticket mix is stable (inputs) while model mix and latency moved (outputs), each explained by
      the change log; at these sample sizes the 0.1 PSI rule of thumb is inside the noise.
    * Step 4: the decision (disable the fast path, route returns back to v14, contact named customers) and the
      runbook, written to .runs/advanced/day6/release/.
    * Step 5: the audit finds request shapes that break on Claude Opus 5.5, a config error already in production,
      a rollback that silently drops thinking, and a prompt hotfix that breaks in-flight conversations.
"""
# test: expect=runbook
# test: expect=BLOCKS
# test: expect=PSI

from __future__ import annotations

import json
import math
import random
import re
import statistics
import sys
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _day6 as d6  # noqa: E402
from labkit import LEDGER, cost_usd, get_client, get_spec, header, is_mock, runs_dir, step  # noqa: E402
from labkit.models import can_read_thinking  # noqa: E402

CANARY_ARM = "haiku-fastpath"
CANARY_CATEGORIES = ("order_status", "product_inquiry")
CANARY_START = "2026-09-09"
CANARY_H1 = 0.12                  # the regression the canary must catch: one customer in eight hurt
REFERENCE = ("2026-08-24", "2026-08-30")          # caching on, v14 only: the last quiet week
WINDOWS = {"08-17..08-23": ("2026-08-17", "2026-08-23"), "08-31..09-06": ("2026-08-31", "2026-09-06"),
           "09-07..09-14": ("2026-09-07", "2026-09-14")}
# The change log, with the features each change is EXPECTED to move - what turns drift into "explained" or not.
CHANGE_LOG = [("2026-08-24", "prompt caching switched on", {"cache-read share"}),
              ("2026-08-31", "v15 on claude-opus-5 at 50% of traffic",
               {"model mix", "latency", "output+thinking tokens", "reply length"}),
              ("2026-09-07", "effort staircase on the v15 arm", {"latency", "output+thinking tokens"}),
              ("2026-09-09", "haiku fast-path canary", {"model mix", "latency", "output+thinking tokens"})]
BINDING_BETA = "thinking-binding-controls-2026-08-01"
PROBE_SYSTEM = "<adv_day6_migration_probe>\nYou are Kestrel's support assistant. Answer order questions from the tools.\n" \
               "</adv_day6_migration_probe>"
GET_ORDER = {"name": "get_order", "description": "Look up an order by its id (SO-#####); returns status and dates.",
             "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}}, "required": ["order_id"]}}
RECORD_FACTS = {"name": "record_facts", "description": "Record the order id and the issue a customer reports.",
                "input_schema": {"type": "object", "properties": {"order_id": {"type": "string"}, "issue": {"type": "string"}},
                                 "required": ["order_id", "issue"]}}

# The production v15 system prompt of the reply agent (an excerpt), as the migration audit finds it.
V15_SYSTEM = """\
You are Kestrel Pumps & Controls' customer support assistant.
CRITICAL: You MUST call get_order before answering ANY question about an order.
Think step by step before you answer, and show your reasoning to the customer when a case is complicated.
Be brief: never write more than 60 words.
IMPORTANT: NEVER mention internal policy documents.
Cite the policy clause (for example RET-002) for every policy statement.
If a return request is ambiguous, hand it off to a colleague.
Do not use bullet points or headers."""

CRUFT = [   # (tag, name, pattern, case-sensitive?, why) - candidates for a human, each removal tested on the eval
    ("TUNE", "emphatic capitals", r"\b(CRITICAL|IMPORTANT|MUST|NEVER|ALWAYS)\b", True,
     "shouted rules over-trigger on current models; state the rule once, with its reason"),
    ("TUNE", "thinking scaffold", r"think step by step|<scratchpad>|<thinking>", False,
     "thinking is adaptive and always on for Opus 5.5; control depth with effort, not prose"),
    ("BLOCKS", "asks for the model's reasoning", r"show (your|its) reasoning", False,
     "reproducing internal reasoning can be declined as reasoning_extraction, which no fallback retries"),
    ("TUNE", "numeric word cap", r"(never|no more than|at most) (write )?(more than )?\d+ words", False,
     "caps tuned against an older model's verbosity starve hard answers; ask for concision"),
    ("TUNE", "anti-formatting rule", r"do not use (bullet|headers)", False,
     "written against over-formatting models; say when formatting helps instead"),
    ("BLOCKS", "claims an action no tool performs", r"hand (it|them)? ?off to a colleague", False,
     "the v15 regression: tie the sentence to escalate_to_human, or the reply promises a colleague nobody assigned"),
]


# ============================================================================================ step 1
def binom_sf(k: int, n: int, p: float) -> float:
    """P(X >= k) for X ~ Binomial(n, p)."""
    return sum(math.comb(n, i) * p ** i * (1 - p) ** (n - i) for i in range(k, n + 1))


def critical_counts(n_max: int, p0: float, alpha: float) -> list[int]:
    """c[n] = the smallest failure count whose one-sided binomial p-value is <= alpha after n traces."""
    out = [1] * (n_max + 1)
    k = 1
    for n in range(1, n_max + 1):
        # P(X >= k) grows with n for a fixed k, so the critical count never decreases: walk it upwards only
        while k <= n and binom_sf(k, n, p0) > alpha:
            k += 1
        out[n] = k                              # k > n means no count of failures could alarm yet
    return out


def simulate_monitors(p_true: float, p0: float, p1: float, *, n_max: int = 200, trials: int = 2000,
                      first_look: int = 10, seed: int = 6) -> dict:
    rng = random.Random(seed)
    crit = critical_counts(n_max, p0, 0.05)
    up, down = math.log((1 - 0.2) / 0.05), math.log(0.2 / (1 - 0.05))
    step_fail, step_ok = math.log(p1 / p0), math.log((1 - p1) / (1 - p0))
    peek = fixed = sprt_h1 = sprt_h0 = 0
    stops = []
    for _ in range(trials):
        failures, llr, sprt_done, peeked = 0, 0.0, False, False
        for n in range(1, n_max + 1):
            failed = rng.random() < p_true
            failures += failed
            if not sprt_done:
                llr += step_fail if failed else step_ok
                if llr >= up or llr <= down:
                    sprt_done = True
                    stops.append(n)
                    sprt_h1 += llr >= up
                    sprt_h0 += llr <= down
            if not peeked and n >= first_look and failures >= crit[n]:
                peeked = True
        if not sprt_done:
            stops.append(n_max)
        peek += peeked
        fixed += failures >= crit[n_max]
    return {"peek": peek / trials, "fixed": fixed / trials, "sprt_h1": sprt_h1 / trials, "sprt_h0": sprt_h0 / trials,
            "asn": statistics.fmean(stops)}


def step_peeking() -> None:
    p0, p1, n_max = 0.05, 0.15, 200
    print(f"A canary watched after every trace: the healthy failure rate is {p0:.0%}, the regression to catch "
          f"{p1:.0%}.\n  Three monitors, 2,000 simulated canaries of up to {n_max} traces each:")
    h0 = simulate_monitors(p0, p0, p1, n_max=n_max)
    h1 = simulate_monitors(p1, p0, p1, n_max=n_max)
    d6.table([["fixed test, one look at n = 200", d6.pct(h0["fixed"]), d6.pct(h1["fixed"]), "200"],
              ["same test after EVERY trace (n >= 10)", d6.pct(h0["peek"]), d6.pct(h1["peek"]), "-"],
              ["SPRT (alpha 0.05, power 0.8)", d6.pct(h0["sprt_h1"]), d6.pct(h1["sprt_h1"]),
               f"{h0['asn']:.0f} / {h1['asn']:.0f}"]],
             ["monitor", "false alarm (no regression)", "detection (regression)", "mean traces to stop (H0 / H1)"])
    print(f"  Peeking at a fixed-sample test is a new test at every look: the false-alarm rate climbs to "
          f"{d6.pct(h0['peek'], 0)}. The SPRT is designed\n  to be looked at after every observation: it holds the "
          f"false-alarm rate near 5% and stops after {h1['asn']:.0f} traces on\n  average when the regression is real. "
          "(Always-valid variants such as mixture SPRTs trade a little power for not\n  having to name H1.)")


# ============================================================================================ step 2
def severe(trace: dict) -> bool:
    """A defect a customer feels: the run failed, the reply names the wrong order, or the customer rated it 1-2."""
    csat = trace["outcome"]["csat"]
    return trace["outcome"]["status"] == "failed" or d6.contradicts_ticket_order(trace) or (csat is not None and csat <= 2)


def step_canary(traces: list[dict]) -> dict:
    canary = sorted((t for t in traces if d6.arm_of(t) == CANARY_ARM), key=lambda t: t["ts"])
    concurrent = [t for t in traces if d6.arm_of(t) == "baseline" and t["category"] in CANARY_CATEGORIES
                  and d6.day_of(t) >= CANARY_START]
    historical = [t for t in traces if d6.arm_of(t) == "baseline" and t["category"] in CANARY_CATEGORIES]
    k0 = sum(map(severe, historical))
    p0 = max(k0 / len(historical), 0.02)
    print(f"The canary: {CANARY_ARM} ({get_spec('claude-haiku-4-5').display_name}) takes 30% of "
          f"{' and '.join(CANARY_CATEGORIES)} from {CANARY_START}.")
    print("Pre-registered decision metric: SEVERE defects - a failed run, a reply naming a different order than the\n"
          "  customer's email, or a CSAT of 1-2. Guardrail (stop without statistics): any reply that contradicts the\n"
          "  customer's order id, and any failed run the customer rated 1-2.")
    print(f"Control: the concurrent baseline on the same categories has {len(concurrent)} traces - too few to compare "
          f"with. The historical\n  baseline on those categories has {len(historical)} traces with {k0} severe "
          f"defect{'' if k0 == 1 else 's'}; "
          f"it is a valid control only if the traffic\n  has not drifted (step 3 checks). H0 = {p0:.1%} (floored at 2%), "
          f"H1 = {CANARY_H1:.0%}.")
    up, down = math.log(0.8 / 0.05), math.log(0.2 / 0.95)
    llr, rows, guardrails, stopped = 0.0, [], [], None
    for i, t in enumerate(canary, 1):
        bad = severe(t)
        if stopped is None:
            llr += math.log(CANARY_H1 / p0) if bad else math.log((1 - CANARY_H1) / (1 - p0))
            if llr >= up or llr <= down:
                stopped = (i, t, "reject H0 (regression)" if llr >= up else "accept H0 (no regression)")
        flags = []
        if d6.contradicts_ticket_order(t):
            flags.append(f"order id {'/'.join(sorted(d6.order_ids(t['reply'])))} != customer's "
                         f"{'/'.join(sorted(d6.ticket_order_ids(t['ticket_id'])))}")
        if t["outcome"]["status"] == "failed" and (t["outcome"]["csat"] or 5) <= 2:
            flags.append(f"failed run, CSAT {t['outcome']['csat']}")
        if flags:
            guardrails.append((t, flags))
        llr_cell = f"{llr:+.2f}" if stopped is None or stopped[0] >= i else "stopped"
        rows.append([d6.day_of(t), t["trace_id"], t["category"], t["outcome"]["status"],
                     t["outcome"]["csat"] if t["outcome"]["csat"] is not None else "-", "yes" if bad else "-",
                     llr_cell + (" <- boundary" if stopped and stopped[0] == i else ""),
                     "GUARDRAIL: " + "; ".join(flags) if flags else ""])
    d6.table(rows, ["day", "trace", "category", "status", "CSAT", "severe", "LLR", "guardrail"])
    decision = stopped[2] if stopped else "continue sampling"
    print(f"  SPRT boundaries {down:+.2f} / {up:+.2f}: "
          + (f"crossed at trace {stopped[0]} ({stopped[1]['trace_id']}, {d6.day_of(stopped[1])}) -> {decision}."
             if stopped else f"no crossing in {len(canary)} traces -> {decision}."))
    if stopped:
        k = sum(map(severe, canary[:stopped[0]]))
        for alt in (0.03, 0.04):
            alt_llr = k * math.log(CANARY_H1 / alt) + (stopped[0] - k) * math.log((1 - CANARY_H1) / (1 - alt))
            print(f"  Had H0 been registered at {alt:.0%} instead of {p0:.0%}, the same {k} severe defects in {stopped[0]} traces "
                  f"give LLR {alt_llr:+.2f}: {'still a crossing' if alt_llr >= up else 'still sampling'}.")
        print("  Two events decide it, so the verdict hinges on numbers chosen BEFORE the data - which is why H0, H1\n"
              "  and the control are written down before the canary starts, never tuned after.")
    per_day = len(canary) / len({d6.day_of(t) for t in canary})
    asn = simulate_monitors(CANARY_H1, p0, CANARY_H1, n_max=400, trials=1000)["asn"]
    print(f"  Planning number: under H1 the SPRT needs about {asn:.0f} canary traces on average; at {per_day:.1f} canary "
          f"traces a day\n  that is about {asn / per_day:.0f} days. A 30% canary on two categories decides statistically "
          "in weeks, not hours - it needs guardrails.")
    reviewed = [(t, t["review"]) for t in canary if t["review"]]
    late = [f"{t['trace_id']}: {', '.join(r['issues']) or 'no findings'}" for t, r in reviewed]
    print(f"  Guardrails fired {len(guardrails)} times, both on {d6.day_of(guardrails[0][0]) if guardrails else '-'}. "
          f"Reviews arrive later: {len(reviewed)} of {len(canary)} canary\n  replies were reviewed ({'; '.join(late)}).")
    print("  Decision: stop the canary. The guardrail fired first and needs no statistics: a wrong order id sent to a\n"
          "  customer has a mechanism to test (the fast path answers without checking the id against the tool\n"
          "  result), and the rollback is a routing flag, so stopping early costs a few days of a cheaper model.")
    return {"guardrails": guardrails, "decision": decision, "p0": p0}


# ============================================================================================ step 3
FEATURES = [
    ("category mix", "input", "cat", lambda t: [t["category"]]),
    ("priority mix", "input", "cat", lambda t: [t["priority"]]),
    ("model mix", "system", "cat", lambda t: [t["deployment"]["model"]]),
    ("tool-call mix", "output", "cat", lambda t: list(t["tools"])),
    ("turns", "output", "cat", lambda t: [t["turns"]]),
    ("reply length", "output", "num", lambda t: [len(t["reply"])]),
    ("latency", "output", "num", lambda t: [t["latency_ms"]]),
    ("output+thinking tokens", "output", "num", lambda t: [t["usage"]["output_tokens"] + t["usage"]["thinking_tokens"]]),
    ("cache-read share", "system", "num", lambda t: [t["usage"]["cache_read_input_tokens"] / t["usage"]["input_tokens"]]),
]


def feature_psi(kind: str, fn, ref: list[dict], cur: list[dict]) -> float:
    a = [v for t in ref for v in fn(t)]
    b = [v for t in cur for v in fn(t)]
    return d6.psi_numeric(a, b) if kind == "num" else d6.psi(d6.shares(a), d6.shares(b))


def psi_noise_floor(kind: str, fn, ref: list[dict], n_cur: int, *, reps: int = 200, seed: int = 6) -> float:
    """95th percentile of PSI between two samples drawn from the SAME reference window, at these sample sizes."""
    rng = random.Random(seed)
    values = sorted(feature_psi(kind, fn, [rng.choice(ref) for _ in ref], [rng.choice(ref) for _ in range(n_cur)])
                    for _ in range(reps))
    return values[int(0.95 * reps) - 1]


def step_drift(traces: list[dict]) -> dict:
    ref = [t for t in traces if REFERENCE[0] <= d6.day_of(t) <= REFERENCE[1]]
    wins = {name: [t for t in traces if lo <= d6.day_of(t) <= hi] for name, (lo, hi) in WINDOWS.items()}
    print(f"Reference window {REFERENCE[0]}..{REFERENCE[1]} ({len(ref)} traces: caching on, v14 only, the last quiet week). "
          "Each week is\n  compared with it; the threshold is the 95th percentile of PSI between two resamples of the "
          "reference\n  at the same sample sizes (the noise floor), not the 0.1 / 0.25 rule of thumb.")
    rows, drifted = [], {}
    for name, family, kind, fn in FEATURES:
        row = [name, family]
        floor = psi_noise_floor(kind, fn, ref, len(next(iter(wins.values()))))
        for wname, cur in wins.items():
            value = feature_psi(kind, fn, ref, cur)
            is_drift = value > floor
            if is_drift:
                drifted.setdefault(wname, []).append((name, family))
            ks = ""
            if kind == "num":
                ks = f" D={d6.ks_statistic([v for t in ref for v in fn(t)], [v for t in cur for v in fn(t)])[0]:.2f}"
            row.append(f"{value:.3f}{'*' if is_drift else ' '}{ks}")
        row.append(f"{floor:.3f}")
        rows.append(row)
    d6.table(rows, ["feature", "family"] + [f"PSI {w}" for w in wins] + ["noise floor"])
    print("  * above the noise floor. D = Kolmogorov-Smirnov distance for numeric features (an effect size; with\n"
          "    hundreds of traces any p-value is small, so read D).")
    unexplained = []
    print("  Change log (with the features each change is expected to move):")
    for day, what, feats in CHANGE_LOG:
        print(f"    {day}  {what:<40} -> {', '.join(sorted(feats))}")
    for wname, (lo, hi) in WINDOWS.items():
        between = [(day, what, feats) for day, what, feats in CHANGE_LOG
                   if (REFERENCE[1] < day <= hi) or (hi < day <= REFERENCE[1])]
        verdicts = []
        for name, family in drifted.get(wname, []):
            why = [f"{day} {what}" for day, what, feats in between if name in feats]
            verdicts.append(f"{name} ({'explained by ' + ', '.join(w[5:10] for w in why) if why else 'UNEXPLAINED'})")
            if not why:
                unexplained.append((wname, name, family))
        print(f"  {wname}: {'; '.join(verdicts) or 'no drift'}")
    total = len(FEATURES) * len(WINDOWS)
    print(f"  The inputs held - customers asked for the same things - and the system changed. Explained drift is a\n"
          f"  check that a change did what it said; the alert is the rest: {len(unexplained)} unexplained "
          f"({', '.join(f'{n} in {w}' for w, n, _ in unexplained) or 'none'}).\n"
          f"  With {total} comparisons at a 95% threshold, about {0.05 * total:.0f} false alarm is expected: an "
          "unexplained drift with no\n  mechanism and a small D gets a look, not a rollback.")
    stable = [name for name, fam, kind, fn in FEATURES
              if fam == "output" and all((name, fam) not in drifted.get(w, []) for w in WINDOWS)]
    print(f"  Missing drift is a finding too: v15's release note promised 'be brief', and reply length did not move\n"
          f"  ({', '.join(stable)} stayed inside the noise): a change that did not do what it said.")
    floor_cat = psi_noise_floor("cat", FEATURES[0][3], ref, len(ref))
    print(f"  The noise floor for a 10-category mix at ~{len(ref)} traces is {floor_cat:.3f}, above the 0.1 'stable' line "
          "- a rule of\n  thumb from portfolios of thousands of loans, not a week of tickets. Measure the floor at "
          "your sizes.")
    before = [t for t in traces if d6.arm_of(t) == "baseline" and "2026-08-31" <= d6.day_of(t) < CANARY_START]
    after = [t for t in traces if d6.arm_of(t) == "baseline" and d6.day_of(t) >= CANARY_START]
    arm_psi = d6.psi(d6.shares(t["category"] for t in before), d6.shares(t["category"] for t in after))
    arm_floor = psi_noise_floor("cat", FEATURES[0][3], before, len(after))
    print(f"  One arm's traffic, before vs after the canary took easy tickets away from it: PSI {arm_psi:.3f} against a "
          f"noise\n  floor of {arm_floor:.3f} at {len(before)} vs {len(after)} traces - invisible at this size. You know "
          "the arms' mixes differ from the\n  router's rule, not from a monitor: compare arms within a window, "
          "standardised (labs 04, 06).")
    return drifted


# ============================================================================================ step 4
def unexpected_handoff(trace: dict) -> bool:
    return d6.handoff_language(trace) and "escalate_to_human" not in trace["tools"]


def step_rollback(traces: list[dict], canary: dict) -> Path:
    v15_returns = [t for t in traces if d6.version_of(t) == "v15" and t["category"] == "return_request"]
    handoffs = sorted((t for t in v15_returns if unexpected_handoff(t)), key=lambda t: t["ts"])
    v14_handoffs = sum(unexpected_handoff(t) for t in traces if d6.version_of(t) == "v14")
    decisions = [
        {"component": "prompt v15 on return_request",
         "evidence": f"{len(handoffs)}/{len(v15_returns)} v15 return replies claim a hand-off with no escalate_to_human call "
                     f"(v14: {v14_handoffs}); lab 04 gate BLOCK",
         "decision": "ROLL BACK the slice: route return_request to v14 now; ship v16 through the gate",
         "how": "router rule category=return_request -> baseline arm (a flag, minutes; no deploy)",
         "verify": "0 unexpected hand-offs in the next 30 return replies; return-slice success back inside v14's interval",
         "why_not_full_rollback": "v15's policy and citation fixes hold on the other nine categories"},
        {"component": "haiku fast path (canary)",
         "evidence": f"{len(canary['guardrails'])} guardrail events on "
                     f"{', '.join(sorted({d6.day_of(t) for t, _ in canary['guardrails']}))}; SPRT: {canary['decision']}",
         "decision": "STOP the canary: route order_status and product_inquiry back to the default arm",
         "how": "canary share 30% -> 0% (a flag); keep the route definition for the relaunch",
         "verify": "no fast-path traffic in the next hour of traces; order-id contradiction rate back to baseline",
         "relaunch_when": "the reply's order ids are validated against the tool results in code, and the canary "
                          "runs long enough for its SPRT (step 2) or on a larger share"},
    ]
    print("Evidence -> decision, one row per component:")
    for dcs in decisions:
        print(f"  {dcs['component']}:\n    evidence : {dcs['evidence']}\n    decision : {dcs['decision']}\n"
              f"    how      : {dcs['how']}\n    verify   : {dcs['verify']}")
    print("\nCustomers to contact today (from the traces - the rollback does not fix what was already sent):")
    remediation = [[d6.day_of(t), t["trace_id"], t["ticket_id"], t["customer_id"] or "-", "promised a colleague; no "
                    "escalation exists - open one now"] for t in handoffs]
    remediation += [[d6.day_of(t), t["trace_id"], t["ticket_id"], t["customer_id"] or "-",
                     "reply named " + "/".join(sorted(d6.order_ids(t["reply"]))) + " - send the correct order status"]
                    for t, _ in canary["guardrails"] if d6.contradicts_ticket_order(t)]
    d6.table(remediation, ["day", "trace", "ticket", "customer", "action"])
    print("\nIn-flight conversations: a rollback that changes the model hands the old model's thinking blocks to the new one.")
    moves = [("claude-opus-5", "claude-sonnet-5", "returns: v15 arm -> v14 arm (today's rollback)"),
             ("claude-haiku-4-5", "claude-sonnet-5", "fast path -> default arm (today's rollback)"),
             ("claude-opus-5-5", "claude-opus-5", "a future rollback after the Opus 5.5 migration (step 5)")]
    d6.table([[f"{a} -> {b}", why, "kept" if can_read_thinking(b, a) else "DROPPED"] for a, b, why in moves],
             ["model move", "when", "thinking blocks"])
    out = runs_dir("advanced", "day6", "release")
    runbook = ["# Runbook - v15 return hand-offs and the haiku fast-path canary (2026-09-15)", "",
               "## Triggers", "- any reply that claims a hand-off with no escalate_to_human call in its trace (zero tolerance)",
               "- any fast-path reply naming an order id other than the customer's (guardrail)",
               "- SPRT on a canary's severe-defect stream crossing its upper boundary", "",
               "## Steps (in order; each has a check)"]
    for i, dcs in enumerate(decisions, 1):
        runbook += [f"{i}. {dcs['decision']} - {dcs['how']}", f"   - check: {dcs['verify']}"]
    runbook += [f"{len(decisions) + 1}. Contact the {len(remediation)} customers listed in rollback_decision.json "
                "(open the missing escalations; correct the wrong order status)",
                f"{len(decisions) + 2}. Post-incident: add the hand-off / escalation contradiction check and the order-id check "
                "to the release gate (lab 04) and to the canary guardrails; count review findings by type", "",
                "## Rollback of the rollback", "- if the return slice on v14 shows its own regression, route returns to a human "
                "queue, not back to v15", "", "## Model moves and thinking",
                "- moving a conversation to a model that cannot read the old model's thinking blocks drops them "
                "(silently, unbilled): prefer draining in-flight conversations on the old route"]
    (out / "runbook.md").write_text("\n".join(runbook) + "\n", encoding="utf-8")
    (out / "rollback_decision.json").write_text(json.dumps({"as_of": d6.TODAY, "decisions": decisions,
                                                             "remediation": remediation}, indent=2), encoding="utf-8")
    print(f"\nWrote the runbook and the decision record: {out / 'runbook.md'}, {out / 'rollback_decision.json'}")
    return out


# ============================================================================================ step 5
def shorten(text: str, limit: int) -> str:
    """Cut at a word boundary, marking the cut."""
    return text if len(text) <= limit else text[:limit].rsplit(" ", 1)[0] + " ..."


def probe(client, **kwargs) -> tuple[str, str, object]:
    """Send one request; return ("ok"|"400", short evidence, response)."""
    try:
        if "betas" in kwargs:
            response = client.beta.messages.create(**kwargs)
        else:
            response = client.messages.create(**kwargs)
        return "ok", f"200, stop_reason {response.stop_reason}", response
    except anthropic.BadRequestError as exc:
        message = exc.body.get("error", {}).get("message", str(exc)) if isinstance(exc.body, dict) else str(exc)
        return "400", "400: " + shorten(message, 110), None


def two_turns(client, first_model: str, second_model: str, *, system2: str | None = None, **extra) -> tuple[str, str]:
    """A tool round started on one model and continued on another (or with an edited system prompt)."""
    messages = [{"role": "user", "content": "Where is SO-10312?"}]
    first = client.beta.messages.create(model=first_model, max_tokens=4000, system=PROBE_SYSTEM, tools=[GET_ORDER],
                                        messages=messages, betas=[BINDING_BETA])
    call = next((b for b in first.content if b.type == "tool_use"), None)
    if call is None or not any(b.type == "thinking" for b in first.content):
        # Live, a model may answer without the tool or skip thinking on an easy turn: nothing to carry over.
        return "inconclusive", "the first turn produced no tool call or no thinking block - rerun the probe"
    messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in first.content]})
    messages.append({"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id,
                                                  "content": json.dumps({"order_id": "SO-10312", "status": "shipped"})}]})
    status, evidence, response = probe(client, model=second_model, max_tokens=4000, system=system2 or PROBE_SYSTEM,
                                       tools=[GET_ORDER], messages=messages, betas=[BINDING_BETA], **extra)
    if response is not None:
        moves = [f"{x.type} ({x.reason})" for x in (response.input_transformations or [])]
        evidence = "input_transformations: " + (", ".join(moves) or "none")
    return status, evidence


def step_audit(client, traces: list[dict]) -> None:
    current, target = "claude-opus-5", "claude-opus-5-5"
    cur, tgt = get_spec(current), get_spec(target)
    checks: list[list[str]] = []

    def add(cid: str, tag: str, title: str, result: str, evidence: str) -> None:
        checks.append([cid, tag, title, result, evidence])

    # A1-A4: the production request shapes, sent to the model they will run on
    reply_agent = dict(max_tokens=1000, system=PROBE_SYSTEM, tools=[GET_ORDER], tool_choice={"type": "auto"},
                       output_config={"effort": "medium"}, messages=[{"role": "user", "content": "Where is SO-10312?"}])
    status, evidence, _ = probe(client, model=target, **reply_agent)
    add("A1", "BLOCKS", "reply agent request on the target", "PASS" if status == "ok" else "FAIL", evidence)
    extractor = dict(max_tokens=1000, system=PROBE_SYSTEM, tools=[RECORD_FACTS],
                     messages=[{"role": "user", "content": "SO-10312 arrived with a cracked housing."}])
    s_cur, _, _ = probe(client, model=current, thinking={"type": "disabled"}, **extractor)
    s_tgt, evidence, _ = probe(client, model=target, thinking={"type": "disabled"}, **extractor)
    add("A2", "BLOCKS", "extractor: thinking disabled", "FAIL" if s_tgt != "ok" else "PASS",
        f"{current}: {s_cur}; {target}: {evidence}")
    s_cur, _, _ = probe(client, model=current, tool_choice={"type": "tool", "name": "record_facts"}, **extractor)
    s_tgt, evidence, _ = probe(client, model=target, tool_choice={"type": "tool", "name": "record_facts"}, **extractor)
    add("A3", "BLOCKS", "extractor: forced tool_choice", "FAIL" if s_tgt != "ok" else "PASS",
        f"{current}: {s_cur}; {target}: {evidence}")
    arm = next(a for a in d6.load_deployments()["arms"] if a["arm"] == CANARY_ARM)
    s_rec, evidence, _ = probe(client, model=arm["model"], max_tokens=300, system=PROBE_SYSTEM,
                               output_config={"effort": arm["effort"]},
                               messages=[{"role": "user", "content": "Where is SO-10312?"}])
    add("A4", "BLOCKS", f"fast path as recorded (effort={arm['effort']})", "FAIL" if s_rec != "ok" else "PASS",
        f"{arm['model']} today: {evidence}")
    # A5: defaults
    add("A5", "TUNE", "default effort where a route omits it", "ACTION",
        f"{current} defaults to {cur.default_effort}, {target} to {tgt.default_effort}: the extractor sets none - set it")
    # A6-A8: thinking binding (see Day 3)
    status, evidence = two_turns(client, current, target)
    add("A6", "BLOCKS", "migrate mid-conversation (forward)", "RERUN" if status == "inconclusive" else
        ("PASS" if status == "ok" and "dropped" not in evidence else "FAIL"), f"{current} -> {target}: {evidence}")
    status, evidence = two_turns(client, target, current)
    add("A7", "TUNE", "roll back mid-conversation", "RERUN" if status == "inconclusive" else
        ("ACTION" if "dropped" in evidence else "PASS"), f"{target} -> {current}: {evidence}")
    hotfix = PROBE_SYSTEM.replace("from the tools.", "from the tools. (v16 hotfix)")
    status, evidence = two_turns(client, target, target, system2=hotfix,
                                 thinking={"type": "adaptive", "block_binding": {"prefix_mismatch_behavior": "error"}})
    add("A8", "BLOCKS", "prompt hotfix on in-flight conversations", "RERUN" if status == "inconclusive" else
        ("FAIL" if status != "ok" else "PASS"), f"{target}, system prompt edited, enforcement on: {evidence}")
    # A9: prompt cruft
    for tag, name, pattern, case_sensitive, why in CRUFT:
        hits = [line.strip() for line in V15_SYSTEM.splitlines()
                if re.search(pattern, line, 0 if case_sensitive else re.I)]
        if hits:
            add("A9", tag, f"prompt: {name}", "ACTION", f"\"{shorten(hits[0], 48)}\" - {why}")
    # A10: price and cache re-baseline
    opus = [t for t in traces if d6.arm_of(t).startswith("opus-v15")]

    def priced(t: dict, spec) -> float:
        u = t["usage"]
        uncached = u["input_tokens"] - u["cache_read_input_tokens"] - u["cache_creation_input_tokens"]
        return cost_usd({"input_tokens": uncached, "cache_read_input_tokens": u["cache_read_input_tokens"],
                         "cache_creation_input_tokens": u["cache_creation_input_tokens"],
                         "output_tokens": u["output_tokens"] + u["thinking_tokens"]}, spec.id)
    now, then = statistics.fmean(priced(t, cur) for t in opus), statistics.fmean(priced(t, tgt) for t in opus)
    add("A10", "TUNE", "price and cache re-baseline", "ACTION",
        f"same token volumes: {d6.money(now)} -> {d6.money(then)}/trace (${tgt.input_price:.0f}/${tgt.output_price:.0f}, "
        f"cache reads {tgt.cache_read_multiplier}x); cold cache on switch")
    add("A11", "BLOCKS", "refusals and fallbacks", "ACTION",
        "handle stop_reason 'refusal' before content; new bio and reasoning_extraction categories; opt into fallbacks")
    add("A12", "BLOCKS", "eval gate on the target", "ACTION",
        "paired offline run of labs 02/04 gates on the target; re-calibrate the judge if its model changes (lab 03)")
    for cid, tag, title, result, evidence in checks:
        print(f"  {cid:<4} {tag:<6} {result:<6} {title}")
        print(f"  {'':<18} {evidence}")
    blocks = [c for c in checks if c[1] == "BLOCKS" and c[3] in ("FAIL", "ACTION")]
    ids = sorted({c[0] for c in blocks}, key=lambda x: int(x[1:]))
    listed = [f"{i} x{sum(c[0] == i for c in blocks)}" if sum(c[0] == i for c in blocks) > 1 else i for i in ids]
    print(f"\n  Migration {current} -> {target}: NOT READY - {sum(c[3] == 'FAIL' for c in checks)} failing probes, "
          f"{len(blocks)} BLOCKS items open ({', '.join(listed)}).")
    print("  Read the probes, not the release notes: A2 and A3 are 400s on the target and fine today, so they surface\n"
          "  only at switch-over; A4 is broken NOW - the deployment record says effort=medium for a model with no\n"
          "  effort parameter, so either the router strips it or that route errors. A7 means a rollback after the\n"
          "  migration runs the in-flight turns without the new model's reasoning; A8 means a prompt hotfix must apply\n"
          "  to new conversations (or arrive as a mid-conversation system message), never by editing a live one's\n"
          "  prefix. The price line holds token volumes fixed; the migration guide says Opus 5.5 tends to think more\n"
          "  per turn at the same effort, so re-measure volumes in a shadow run before trusting the saving.")
    if is_mock():
        print("[mock] The probes run against the mock's request validation, which mirrors the documented rules;\n"
              "       claude-opus-5-5 here is a 'recorded' account, so A8 sets prefix_mismatch_behavior 'error' to see\n"
              "       what accounts created on or after 2026-08-31 get by default. Live, the same probes cost cents.")


def main() -> None:
    header("Lab 07 - Canary, drift, rollback and the migration audit")
    client = get_client()
    traces = d6.load_traces()

    step(1, "Why a canary needs a sequential test: peeking at a fixed-sample test")
    step_peeking()

    step(2, "The haiku fast-path canary: statistics and guardrails")
    canary = step_canary(traces)

    step(3, "Drift on the trace stream: PSI and KS against a measured noise floor")
    step_drift(traces)

    step(4, "The rollback decision and the runbook")
    step_rollback(traces, canary)

    step(5, "The next release: a migration audit as code (Claude Opus 5 -> Claude Opus 5.5)")
    step_audit(client, traces)
    print(f"  cost of the probes: {LEDGER.total_calls} calls, ${LEDGER.total_cost:.4f}"
          f"{' (simulated usage at list prices)' if is_mock() else ''}")


if __name__ == "__main__":
    main()
