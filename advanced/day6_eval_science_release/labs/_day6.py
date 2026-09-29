"""Shared helpers for the Day 6 labs (a helper, not a lab: files starting with "_" are not run by the tests).

What lives here and why:
* trace access - the 480 production traces of the Service Desk Copilot, their deployment arms, the 120 human
  pairwise judgments and the ticket-level split. `load_traces()` STRIPS the hidden `_truth` field: labs must mine
  the observable fields only (reviews, CSAT, human edits, status, reply text). Solutions may grade against the
  truth at the very end through `hidden_truth_for_grading()`, whose name says what it is for;
* labels - the observable success signals and the weak label the labs derive from them;
* statistics - Wilson and bootstrap intervals, the Newcombe interval for a difference of two rates, paired and
  unpaired tests, power and sample size, multiple-comparison corrections (Bonferroni, Holm, Benjamini-Hochberg),
  pass@k / pass^k, Cohen's kappa, Bradley-Terry, PSI (categorical and binned numeric) and KS drift statistics, a
  sequential probability ratio test. All pure Python, seeded, and small enough to read: the labs teach these, so
  they must not hide in a library call;
* observable facts - the order ids a ticket's email names and a reply names (for contradiction checks);
* printing - an aligned text table, money and percentage formatting.
"""

from __future__ import annotations

import json
import math
import random
import re
import statistics
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from labkit import DATA_DIR, REPO_ROOT

TRACE_DIR = REPO_ROOT / "advanced" / "data" / "traces"
TODAY = "2026-09-15"
ARMS = ["baseline", "opus-v15", "opus-v15-high", "opus-v15-low", "haiku-fastpath"]
CATEGORIES = ["order_status", "shipping_delay", "return_request", "warranty_claim", "billing", "technical_support",
              "product_inquiry", "safety_incident", "account_access", "other"]
Z95 = 1.959964


# ============================================================================================ data access
def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@lru_cache(maxsize=1)
def _raw_traces() -> tuple[dict, ...]:
    return tuple(_read_jsonl(TRACE_DIR / "traces.jsonl"))


def load_traces() -> list[dict]:
    """The production traces WITHOUT the hidden ground truth. Labs work on these copies only."""
    out = []
    for t in _raw_traces():
        t = json.loads(json.dumps(t))       # deep copy: labs may annotate their copy
        t.pop("_truth", None)
        out.append(t)
    return out


def hidden_truth_for_grading() -> dict[str, list[str]]:
    """trace_id -> planted issues. For SOLUTIONS that grade a learner's method after the fact, never for labs."""
    return {t["trace_id"]: list(t["_truth"]["issues"]) for t in _raw_traces()}


def load_deployments() -> dict:
    return json.loads((TRACE_DIR / "deployments.json").read_text(encoding="utf-8"))


def load_pairs() -> list[dict]:
    return _read_jsonl(TRACE_DIR / "pairwise_judgments.jsonl")


def load_splits() -> dict:
    return json.loads((TRACE_DIR / "splits.json").read_text(encoding="utf-8"))


def split_of() -> dict[str, str]:
    """ticket_id -> train | validation | test."""
    splits = load_splits()
    return {tid: name for name in ("train", "validation", "test") for tid in splits[name]}


@lru_cache(maxsize=1)
def ticket_labels() -> dict[str, dict]:
    """The base course's ticket ground truth (category, priority, order_id ...), keyed by ticket id."""
    return {row["ticket_id"]: row for row in _read_jsonl(DATA_DIR / "support" / "ticket_labels.jsonl")}


@lru_cache(maxsize=1)
def tickets() -> dict[str, dict]:
    """The base course's 62 inbound support emails, keyed by ticket id."""
    return {row["ticket_id"]: row for row in _read_jsonl(DATA_DIR / "support" / "tickets.jsonl")}


def day_of(trace: dict) -> str:
    return trace["ts"][:10]


def arm_of(trace: dict) -> str:
    return trace["deployment"]["arm"]


def version_of(trace: dict) -> str:
    return trace["deployment"]["prompt_version"]


# ============================================================================================ labels
def observable_success(trace: dict) -> bool:
    """The success signal available on EVERY trace: the run did not fail and a human did not rewrite the reply."""
    outcome = trace["outcome"]
    return outcome["status"] != "failed" and not outcome["human_edited"]


def weak_label(trace: dict) -> tuple[bool, str]:
    """(passed, source): the best available signal per trace - reviewer verdict > CSAT > human edit."""
    review = trace.get("review")
    if review is not None:
        return (not review["issues"], "review")
    csat = trace["outcome"]["csat"]
    if csat is not None:
        return (csat >= 4, "csat")
    return (observable_success(trace), "edit")


def handoff_language(trace: dict) -> bool:
    """Text signal: the reply hands the customer off to a colleague instead of answering."""
    reply = trace["reply"].lower()
    return "escalated your" in reply or "passed it to a colleague" in reply or "handed this to a colleague" in reply


ORDER_ID = re.compile(r"\bSO-\d{5}\b")


def order_ids(text: str) -> set[str]:
    """Every order id (SO-#####) a piece of text names."""
    return set(ORDER_ID.findall(text or ""))


def ticket_order_ids(ticket_id: str) -> set[str]:
    """The order ids the customer's own email names (subject + body): an observable fact, not a label."""
    ticket = tickets()[ticket_id]
    return order_ids(f"{ticket['subject']}\n{ticket['body']}")


def contradicts_ticket_order(trace: dict) -> bool:
    """The reply names an order id, the customer's email names one, and none of the reply's ids is the customer's."""
    named, asked = order_ids(trace["reply"]), ticket_order_ids(trace["ticket_id"])
    return bool(named and asked and not (named & asked))


# ============================================================================================ intervals and tests
def wilson(k: int, n: int, z: float = Z95) -> tuple[float, float]:
    """Wilson score interval for a proportion - behaves at 0% and 100%, unlike the normal approximation."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def bootstrap_ci(values: Sequence[float], stat: Callable[[Sequence[float]], float] = statistics.fmean, *,
                 n_boot: int = 2000, seed: int = 6, alpha: float = 0.05) -> tuple[float, float]:
    """Percentile bootstrap interval of `stat` over `values` (resample with replacement, n_boot times)."""
    if not values:
        return (0.0, 0.0)
    rng = random.Random(seed)
    n = len(values)
    stats = sorted(stat([values[rng.randrange(n)] for _ in range(n)]) for _ in range(n_boot))
    lo = stats[int(math.floor(alpha / 2 * n_boot))]
    hi = stats[min(n_boot - 1, int(math.ceil((1 - alpha / 2) * n_boot)) - 1)]
    return (lo, hi)


def paired_bootstrap_ci(diffs: Sequence[float], **kw: Any) -> tuple[float, float]:
    """Bootstrap interval of a mean paired difference (resample the PAIRS, not the arms)."""
    return bootstrap_ci(list(diffs), statistics.fmean, **kw)


def normal_cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def z_quantile(p: float) -> float:
    """Inverse normal CDF by bisection (enough precision for power arithmetic)."""
    lo, hi = -10.0, 10.0
    for _ in range(100):
        mid = (lo + hi) / 2
        if normal_cdf(mid) < p:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def two_proportion_test(k1: int, n1: int, k2: int, n2: int) -> tuple[float, float]:
    """Unpaired two-proportion z-test: (z, two-sided p)."""
    p1, p2 = k1 / n1, k2 / n2
    pooled = (k1 + k2) / (n1 + n2)
    se = math.sqrt(pooled * (1 - pooled) * (1 / n1 + 1 / n2))
    if se == 0:
        return (0.0, 1.0)
    z = (p1 - p2) / se
    return (z, 2 * (1 - normal_cdf(abs(z))))


def newcombe_diff_ci(k1: int, n1: int, k2: int, n2: int, z: float = Z95) -> tuple[float, float]:
    """Interval for p2 - p1 (candidate minus baseline) from two Wilson intervals (Newcombe 1998, method 10).

    Unlike the Wald interval it behaves at 0% and 100% and on small slices - the situation of every release gate.
    """
    if not n1 or not n2:
        return (-1.0, 1.0)
    p1, p2 = k1 / n1, k2 / n2
    l1, u1 = wilson(k1, n1, z)
    l2, u2 = wilson(k2, n2, z)
    d = p2 - p1
    lo = d - math.sqrt((p2 - l2) ** 2 + (u1 - p1) ** 2)
    hi = d + math.sqrt((u2 - p2) ** 2 + (p1 - l1) ** 2)
    return (max(-1.0, lo), min(1.0, hi))


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant pairs (b: pass->fail, c: fail->pass)."""
    n = b + c
    if n == 0:
        return 1.0
    tail = sum(math.comb(n, i) for i in range(0, min(b, c) + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def fisher_exact(a: int, b: int, c: int, d: int) -> float:
    """Two-sided Fisher exact test on [[a, b], [c, d]] (sum of tables as or less probable than the observed one)."""
    row1, col1, n = a + b, a + c, a + b + c + d

    def prob(x: int) -> float:
        return math.comb(row1, x) * math.comb(n - row1, col1 - x) / math.comb(n, col1)

    observed = prob(a)
    lo, hi = max(0, col1 - (n - row1)), min(row1, col1)
    return min(1.0, sum(prob(x) for x in range(lo, hi + 1) if prob(x) <= observed * (1 + 1e-9)))


def sample_size_two_proportions(p1: float, p2: float, *, alpha: float = 0.05, power: float = 0.8) -> int:
    """Per-arm n to detect p1 vs p2 with an unpaired two-sided z-test."""
    za, zb = z_quantile(1 - alpha / 2), z_quantile(power)
    pbar = (p1 + p2) / 2
    num = (za * math.sqrt(2 * pbar * (1 - pbar)) + zb * math.sqrt(p1 * (1 - p1) + p2 * (1 - p2))) ** 2
    return math.ceil(num / (p1 - p2) ** 2)


def mde_two_proportions(p: float, n: int, *, alpha: float = 0.05, power: float = 0.8) -> float:
    """Smallest absolute drop from p detectable with n per arm (unpaired), by bisection on the sample-size formula."""
    lo, hi = 1e-4, p - 1e-4
    for _ in range(60):
        mid = (lo + hi) / 2
        if sample_size_two_proportions(p, p - mid, alpha=alpha, power=power) > n:
            lo = mid
        else:
            hi = mid
    return hi


def paired_sample_size(p_fail_new: float, p_fix_new: float, *, alpha: float = 0.05, power: float = 0.8) -> int:
    """Scenarios for McNemar's test (Connor 1987): only the discordant pairs carry information."""
    za, zb = z_quantile(1 - alpha / 2), z_quantile(power)
    disc, delta = p_fail_new + p_fix_new, p_fail_new - p_fix_new
    return math.ceil((za * math.sqrt(disc) + zb * math.sqrt(disc - delta ** 2)) ** 2 / delta ** 2)


def bonferroni(pvalues: dict[str, float], alpha: float = 0.05) -> dict[str, bool]:
    """Reject H0 for every test with p <= alpha / m (controls the family-wise error rate)."""
    m = len(pvalues)
    return {k: p <= alpha / m for k, p in pvalues.items()}


def holm(pvalues: dict[str, float], alpha: float = 0.05) -> dict[str, bool]:
    """Holm step-down: sort p ascending, reject while p_(i) <= alpha / (m - i + 1); never less powerful than Bonferroni."""
    m = len(pvalues)
    out = {k: False for k in pvalues}
    for i, (k, p) in enumerate(sorted(pvalues.items(), key=lambda kv: kv[1]), 1):
        if p > alpha / (m - i + 1):
            break
        out[k] = True
    return out


def benjamini_hochberg(pvalues: dict[str, float], q: float = 0.10) -> dict[str, bool]:
    """Benjamini-Hochberg step-up: reject the i smallest p-values for the largest i with p_(i) <= i q / m.

    Controls the false discovery RATE (the share of flagged slices that are false alarms), not the chance of any
    false alarm - the right trade for monitors that route slices to a human, the wrong one for a hard gate.
    """
    m = len(pvalues)
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    cutoff = 0
    for i, (_, p) in enumerate(ordered, 1):
        if p <= i * q / m:
            cutoff = i
    rejected = {k for k, _ in ordered[:cutoff]}
    return {k: k in rejected for k in pvalues}


def pass_at_k(n: int, c: int, k: int) -> float:
    """P(at least one of k attempts passes), unbiased from c passes in n trials."""
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    """pass^k: P(all k attempts pass), unbiased from c passes in n trials."""
    return math.comb(c, k) / math.comb(n, k) if k <= n else 0.0


# ============================================================================================ agreement
def cohens_kappa(a: Sequence[Any], b: Sequence[Any]) -> float:
    """Agreement beyond chance between two raters on the same items (1 = perfect, 0 = chance, < 0 worse)."""
    n = len(a)
    if n == 0:
        return 0.0
    labels = sorted(set(a) | set(b), key=str)
    observed = sum(x == y for x, y in zip(a, b)) / n
    expected = sum((list(a).count(lbl) / n) * (list(b).count(lbl) / n) for lbl in labels)
    return 1.0 if expected == 1 else (observed - expected) / (1 - expected)


def kappa_ci(a: Sequence[Any], b: Sequence[Any], *, n_boot: int = 1000, seed: int = 6) -> tuple[float, float]:
    """Bootstrap interval of Cohen's kappa (resample items)."""
    rng = random.Random(seed)
    n = len(a)
    ks = sorted(cohens_kappa(*zip(*[(a[i], b[i]) for i in (rng.randrange(n) for _ in range(n))]))
                for _ in range(n_boot))
    return (ks[int(0.025 * n_boot)], ks[int(0.975 * n_boot) - 1])


# ============================================================================================ Bradley-Terry
def bradley_terry(comparisons: Iterable[tuple[str, str, str]], *, reference: str | None = None,
                  iterations: int = 500, tie_weight: float = 0.5) -> dict[str, float]:
    """Bradley-Terry strengths from (a, b, outcome) triples; outcome is "A", "B" or "tie".

    Minorisation-maximisation (Hunter 2004): p_i <- W_i / sum_j n_ij / (p_i + p_j), where W_i counts i's wins
    (a tie counts `tie_weight` for each side). Strengths are scaled so the reference arm (or the geometric
    mean) is 1.0; P(i beats j) = p_i / (p_i + p_j).
    """
    wins: dict[str, float] = defaultdict(float)
    games: dict[tuple[str, str], float] = defaultdict(float)
    items: set[str] = set()
    for a, b, outcome in comparisons:
        items.update((a, b))
        games[(a, b)] += 1
        games[(b, a)] += 1
        if outcome == "A":
            wins[a] += 1
        elif outcome == "B":
            wins[b] += 1
        else:
            wins[a] += tie_weight
            wins[b] += tie_weight
    p = {i: 1.0 for i in items}
    for _ in range(iterations):
        new = {}
        for i in items:
            denom = sum(games[(i, j)] / (p[i] + p[j]) for j in items if j != i and games[(i, j)])
            new[i] = (wins[i] / denom) if denom else p[i]
            new[i] = max(new[i], 1e-6)          # an arm that never won keeps a tiny positive strength
        scale = math.exp(sum(math.log(v) for v in new.values()) / len(new))
        p = {i: v / scale for i, v in new.items()}
    if reference and reference in p:
        p = {i: v / p[reference] for i, v in p.items()}
    return p


def bt_win_probability(strengths: dict[str, float], a: str, b: str) -> float:
    return strengths[a] / (strengths[a] + strengths[b])


# ============================================================================================ drift statistics
def psi(reference: dict[str, float], current: dict[str, float], *, epsilon: float = 1e-4) -> float:
    """Population Stability Index between two categorical distributions (shares, not counts)."""
    keys = set(reference) | set(current)
    total = 0.0
    for k in keys:
        r = max(reference.get(k, 0.0), epsilon)
        c = max(current.get(k, 0.0), epsilon)
        total += (c - r) * math.log(c / r)
    return total


def shares(values: Iterable[Any]) -> dict[str, float]:
    counts = Counter(values)
    n = sum(counts.values())
    return {str(k): v / n for k, v in counts.items()} if n else {}


def quantile_edges(reference: Sequence[float], bins: int = 10) -> list[float]:
    """Inner cut points at the reference's quantiles (deciles by default), de-duplicated for lumpy data."""
    xs = sorted(reference)
    edges = sorted({percentile(xs, 100 * i / bins) for i in range(1, bins)})
    return edges


def binned_shares(values: Iterable[float], edges: Sequence[float]) -> dict[str, float]:
    """Share of values per bin: bin i holds edges[i-1] < x <= edges[i] (bin 0 below the first edge)."""
    counts: Counter = Counter()
    n = 0
    for x in values:
        counts[str(sum(x > e for e in edges))] += 1
        n += 1
    return {k: v / n for k, v in counts.items()} if n else {}


def psi_numeric(reference: Sequence[float], current: Sequence[float], *, bins: int = 10) -> float:
    """PSI of a numeric feature on the reference's quantile bins (the usual way to monitor lengths, latencies)."""
    edges = quantile_edges(reference, bins)
    return psi(binned_shares(reference, edges), binned_shares(current, edges))


def ks_statistic(a: Sequence[float], b: Sequence[float]) -> tuple[float, float]:
    """Two-sample Kolmogorov-Smirnov: (D, asymptotic two-sided p-value)."""
    xs, ys = sorted(a), sorted(b)
    n, m = len(xs), len(ys)
    if not n or not m:
        return (0.0, 1.0)
    i = j = 0
    d = 0.0
    while i < n and j < m:                      # step over ties as one block, so integer data is handled correctly
        v = min(xs[i], ys[j])
        while i < n and xs[i] == v:
            i += 1
        while j < m and ys[j] == v:
            j += 1
        d = max(d, abs(i / n - j / m))
    if d == 0:
        return (0.0, 1.0)
    ne = n * m / (n + m)
    lam = (math.sqrt(ne) + 0.12 + 0.11 / math.sqrt(ne)) * d
    p = 2 * sum((-1) ** (k - 1) * math.exp(-2 * k * k * lam * lam) for k in range(1, 101))
    return (d, max(0.0, min(1.0, p)))


# ============================================================================================ sequential tests
def sprt(outcomes: Sequence[bool], p0: float, p1: float, *, alpha: float = 0.05, beta: float = 0.2) -> dict:
    """Wald's sequential probability ratio test on a stream of failures (True = failure).

    H0: failure rate p0 (the control), H1: p1 (the regression you want to catch). The log-likelihood ratio
    accumulates one observation at a time; crossing log(B) accepts H1 (stop, roll back), crossing log(A)
    accepts H0 (stop, promote), anything between means keep sampling.
    """
    upper = math.log((1 - beta) / alpha)
    lower = math.log(beta / (1 - alpha))
    llr, path, decision, stopped_at = 0.0, [], "continue", None
    for i, failed in enumerate(outcomes, 1):
        llr += math.log(p1 / p0) if failed else math.log((1 - p1) / (1 - p0))
        path.append(llr)
        if decision == "continue":
            if llr >= upper:
                decision, stopped_at = "reject H0 (regression)", i
            elif llr <= lower:
                decision, stopped_at = "accept H0 (no regression)", i
    return {"llr": llr, "path": path, "upper": upper, "lower": lower, "decision": decision, "stopped_at": stopped_at}


# ============================================================================================ small utilities
def percentile(values: Sequence[float], q: float) -> float:
    if not values:
        return 0.0
    xs = sorted(values)
    pos = (len(xs) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


def money(x: float) -> str:
    return f"${x:,.4f}" if x < 1 else f"${x:,.2f}"


def ci_text(lo: float, hi: float) -> str:
    return f"{pct(lo)}-{pct(hi)}"


def table(rows: list[list[Any]], headers: list[str], *, indent: str = "  ") -> None:
    """Print an aligned text table (numbers right-aligned)."""
    cells = [[str(h) for h in headers]] + [[f"{c:,}" if isinstance(c, int) and not isinstance(c, bool) else str(c)
                                            for c in row] for row in rows]
    widths = [max(len(r[i]) for r in cells) for i in range(len(headers))]

    def fmt(row: list[str], is_header: bool = False) -> str:
        out = []
        for i, c in enumerate(row):
            stripped = c.replace(",", "").replace(".", "").replace("$", "").replace("%", "").replace("-", "") \
                        .replace("+", "").replace("x", "").strip()
            numeric = not is_header and stripped.isdigit()
            out.append(c.rjust(widths[i]) if numeric else c.ljust(widths[i]))
        return indent + "  ".join(out).rstrip()

    print(fmt(cells[0], True))
    print(indent + "  ".join("-" * w for w in widths))
    for row in cells[1:]:
        print(fmt(row))


def by(items: Iterable[dict], key: Callable[[dict], Any]) -> dict[Any, list[dict]]:
    out: dict[Any, list[dict]] = defaultdict(list)
    for item in items:
        out[key(item)].append(item)
    return dict(out)


def rate(flags: Sequence[bool]) -> float:
    return sum(flags) / len(flags) if flags else 0.0
