"""Lab 07 - Tool contracts and a tool-selection eval: measure descriptions, scope toolsets, retire a tool.

Objective
    Treat tool definitions as contracts and measure them. A/B two description sets for twelve tools (six of them
    near-duplicates) on a 30-task selection eval - accuracy with confidence intervals, confusions, the paired
    difference - and on discovery through tool search. Tighten schemas with strict mode and enums, count what
    `input_examples` cost, derive capability-scoped toolsets per role from tenants.json, deprecate a tool behind a
    versioned name, and read the error envelope every tool returns.

Concepts
    descriptions as the selection and retrieval surface, trigger phrases in users' words, contrastive descriptions for
    near-duplicates, selection eval (first tool call vs expected), Wilson intervals, paired wins and losses,
    strict: true (additionalProperties: false, enums), input_examples, role- and tenant-scoped toolsets (domains, max
    risk, denied tools, row filters - Day 5), versioned names and deprecation windows, error contracts
    (code / message / next_action)

Run
    python advanced/day2_tools_at_scale/labs/07_tool_contracts_and_selection_eval.py

What to observe
    * The accuracy of set A (the catalog's descriptions) vs set B (contracts), with 95% intervals, and which tasks moved.
    * The one confusion B still has, and why (two descriptions share a trigger word).
    * Discovery hit@1 moves with the same descriptions: tool search reads nothing else.
    * A contractor engineer's toolset cannot even find list_contacts; the executor refuses it anyway.
    * The retired tool answers with an error that names its successor, and the call is retried with it.
"""
# test: expect=Selection eval
# test: expect=get_shipment_v2

from __future__ import annotations

import json
import math
import sys
from collections import Counter
from pathlib import Path

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, get_client, header, is_mock, step, wrap  # noqa: E402

import _day2 as d2  # noqa: E402

SELECT_SYSTEM = d2.MARK_SELECT + "\nYou are Kestrel's operations copilot. Call the one tool that fits the request."
SEARCH_SYSTEM = (d2.MARK_SEARCH + "\nYou are Kestrel's operations copilot. Search the tool catalog for the tool that fits the "
                 "request and report what you found.")
AGENT_SYSTEM = (d2.MARK_WIDE + "\nYou are Kestrel's operations copilot. Use the tools you have; search the catalog when none "
                "fits. Say plainly when you cannot do something.")


def set_a() -> list[dict]:
    return d2.loaded_toolset(d2.SELECTION_TOOLS)


def set_b() -> list[dict]:
    return [d2.api_tool(d2.entry(n), description=d2.DESCRIPTIONS_B[n]) for n in d2.SELECTION_TOOLS]


def first_call(client, tools: list[dict], text: str) -> str | None:
    r = client.messages.create(model=MODEL, max_tokens=2000, system=d2.cached_system(SELECT_SYSTEM), tools=tools,
                               messages=[{"role": "user", "content": text}])
    return next((b.name for b in r.content if b.type == "tool_use"), None)


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def step_descriptions() -> None:
    for name in ("get_shipment", "track_shipment", "issue_refund", "issue_credit_note"):
        print(f"  {name}")
        print(wrap("A: " + d2.entry(name)["description"], "      "))
        print(wrap("B: " + d2.DESCRIPTIONS_B[name], "      "))


def step_eval(client) -> dict[str, list[str | None]]:
    got = {"A": [first_call(client, set_a(), t) for t, _ in d2.SELECTION_TASKS],
           "B": [first_call(client, set_b(), t) for t, _ in d2.SELECTION_TASKS]}
    expected = [e for _, e in d2.SELECTION_TASKS]
    n = len(expected)
    rows = []
    for label in ("A", "B"):
        k = sum(g == e for g, e in zip(got[label], expected))
        lo, hi = wilson(k, n)
        tokens = d2.count_prompt(client, set_a() if label == "A" else set_b(), [{"role": "user", "content": "x"}])
        rows.append([label, f"{k}/{n}", f"{k / n:.0%}", f"{lo:.0%}-{hi:.0%}", tokens])
    d2.table(rows, ["set", "correct", "accuracy", "95% CI (Wilson)", "prompt tokens (12 tools)"])
    fixed = [i for i in range(n) if got["A"][i] != expected[i] and got["B"][i] == expected[i]]
    broken = [i for i in range(n) if got["A"][i] == expected[i] and got["B"][i] != expected[i]]
    print(f"\nPaired: B fixes {len(fixed)} task(s) A got wrong and breaks {len(broken)} A got right "
          f"(the same {n} tasks, so compare task by task, not the two percentages).")
    for label in ("A", "B"):
        misses = [(expected[i], got[label][i], d2.SELECTION_TASKS[i][0]) for i in range(n) if got[label][i] != expected[i]]
        print(f"\n{label}: {len(misses)} confusion(s)")
        for e, g, text in misses:
            print(f"  expected {e:<24} got {str(g):<24} {d2.clip(text, 58)}")
    return got


def step_discovery(client) -> None:
    """The same descriptions, used as the retrieval surface of tool search."""
    rows = []
    for label, override in (("A", {}), ("B", d2.DESCRIPTIONS_B)):
        tools = [dict(d2.SEARCH_BM25)] + [d2.api_tool(d2.entry(n), defer=n != "search_knowledge_base",
                                                     description=override.get(n)) for n in d2.all_names()]
        ranks = []
        for text, expected in d2.SELECTION_TASKS:
            r = client.messages.create(model=MODEL, max_tokens=1500, system=d2.cached_system(SEARCH_SYSTEM), tools=tools,
                                       messages=[{"role": "user", "content": text}])
            refs = [ref.tool_name for b in r.content if b.type == "tool_search_tool_result"
                    for ref in (getattr(b.content, "tool_references", None) or [])]
            ranks.append(refs.index(expected) + 1 if expected in refs else None)
        n = len(ranks)
        rows.append([label, f"{sum(1 for x in ranks if x == 1) / n:.0%}", f"{sum(1 for x in ranks if x and x <= 3) / n:.0%}",
                     f"{sum(1 for x in ranks if x) / n:.0%}", f"{sum(1 / x for x in ranks if x) / n:.2f}"])
    d2.table(rows, ["descriptions", "hit@1", "hit@3", "hit@5", "MRR"])
    print("(all 120 tools deferred except one; the 12 eval tools carry set A or set B; BM25 tool search, 30 tasks)")


def step_strict(client) -> None:
    loose = {"name": "issue_credit_note", "description": d2.DESCRIPTIONS_B["issue_credit_note"], "strict": True,
             "input_schema": {"type": "object", "required": ["invoice_id", "amount_usd", "reason"],
                              "properties": {"invoice_id": {"type": "string"}, "amount_usd": {"type": "number"},
                                             "reason": {"type": "string"}}}}
    contract = {**loose, "input_schema": {**loose["input_schema"], "additionalProperties": False, "properties": {
        "invoice_id": {"type": "string", "description": "Invoice ID, e.g. AR-90257."},
        "amount_usd": {"type": "number", "description": "Credit amount in US dollars (compute percentages first)."},
        "reason": {"type": "string", "enum": ["late_delivery", "damaged_goods", "pricing_error", "goodwill"],
                   "description": "Why the credit is issued."}}}}
    for label, t in (("strict without additionalProperties: false", loose), ("strict contract with enum", contract)):
        try:
            client.messages.count_tokens(model=MODEL, tools=[t], messages=[{"role": "user", "content": "x"}])
            client.messages.create(model=MODEL, max_tokens=500, tools=[t], messages=[{"role": "user", "content": "hi"}])
            print(f"  [accepted] {label}")
        except anthropic.BadRequestError as exc:
            message = exc.body.get("error", {}).get("message", str(exc)) if isinstance(exc.body, dict) else str(exc)
            print(f"  [400] {label}\n        {message}")
    examples = {**contract, "input_examples": [{"invoice_id": "AR-90257", "amount_usd": 1228.2, "reason": "goodwill"},
                                               {"invoice_id": "AR-90267", "amount_usd": 640.0, "reason": "damaged_goods"}]}
    plain = d2.count_prompt(client, [contract], [{"role": "user", "content": "x"}])
    with_examples = d2.count_prompt(client, [examples], [{"role": "user", "content": "x"}])
    print(f"  input_examples (two worked calls, one showing a computed 5% amount): +{with_examples - plain} tokens on every "
          "request that loads the tool.")
    print("  strict guarantees the SHAPE (types, required fields, enum members); it cannot know that AR-90257 exists or that\n"
          "  5% of 24,564.00 is 1,228.20 - the executor still validates meaning. Programmatic tool calling does not accept\n"
          "  strict tools; keep strict on tools the model calls directly.")


def step_scopes(client) -> None:
    rows = []
    for role, spec in d2.tenants()["roles"].items():
        names = d2.scoped_names(role)
        tokens = d2.count_prompt(client, d2.wide_toolset(d2.SEARCH_BM25, loaded=[n for n in d2.core_names() if n in names],
                                                         names=names), [{"role": "user", "content": "x"}])
        rows.append([role, len(names), "all" if spec["domains"] == "*" else len(spec["domains"]), spec["max_risk"],
                     len(spec.get("denied_tools", [])), spec.get("row_filter", "-"), tokens])
    d2.table(rows, ["role", "tools", "domains", "max risk", "denied", "row filter", "prompt (core+search)"])
    print(f"  plus, for every agent: {', '.join(d2.tenants()['always_denied_to_agents'])} are never offered.")

    text = "List the contacts at C-1016 with their roles and emails."
    print(f"\n  The same request under two roles: {text!r}")
    for role in ("support_agent", "contractor_engineer"):
        names = d2.scoped_names(role)
        tools = d2.wide_toolset(d2.SEARCH_BM25, loaded=[n for n in d2.core_names() if n in names], names=names)
        ops = d2.KestrelOps(allowed=names)
        run = d2.run_agent(client, system=AGENT_SYSTEM, tools=tools, messages=[{"role": "user", "content": text}],
                           execute=ops.run, trace=False, max_turns=4)
        print(f"  {role:<20} searched {run.searches or '-'} found {run.discovered[:3] or '-'} called {run.called or '-'}")
        print(wrap(run.reply, "  " + " " * 21 + "| "))
    out, is_error = d2.KestrelOps(allowed=d2.scoped_names("contractor_engineer")).run("list_contacts", {"customer_id": "C-1016"})
    print(f"\n  And if a contractor session called it anyway (a stale prompt, an injection): is_error={is_error} {out}")
    print("  The toolset decides what the model can see and find; the executor decides what runs. Row filters\n"
          "  ('customer_id in tenant.customers') and approval limits live in the executor too - Day 5 builds them.")


def step_deprecation(client) -> None:
    v1 = d2.api_tool(d2.entry("get_shipment"), description="DEPRECATED - use get_shipment_v2 (same shipment_id; this name "
                     "stops working after the deprecation window). " + d2.entry("get_shipment")["description"])
    v2 = d2.api_tool(d2.entry("get_shipment_v2"))
    tools = [v1, v2] + [t for t in set_b() if t["name"] != "get_shipment"]
    tasks = [(t, e) for t, e in d2.SELECTION_TASKS if e == "get_shipment"]
    picks = Counter(first_call(client, tools, t) for t, _ in tasks)
    print(f"  1. announce: v1 described as DEPRECATED, v2 alongside -> the {len(tasks)} shipment-record tasks went to "
          f"{dict(picks)}")

    text = "Use get_shipment to pull the ship date and the delivered date of SH-50237."
    ops = d2.KestrelOps(retired={"get_shipment": "get_shipment_v2"})
    run = d2.run_agent(client, system=AGENT_SYSTEM, tools=[d2.api_tool(d2.entry("get_shipment")), v2] + set_b()[:3],
                       messages=[{"role": "user", "content": text}], execute=ops.run, trace=False, max_turns=4)
    print(f"  2. sunset: an old integration still asks for v1 by name -> calls {run.calls}")
    error = next(b for m in run.messages if m["role"] == "user" and isinstance(m["content"], list)
                 for b in m["content"] if isinstance(b, dict) and b.get("is_error"))
    print(f"     the retired name answered with is_error=True and the model retried with the name the error gave:")
    print(f"     {error['content']}")
    usage = Counter(c["name"] for c in ops.calls)
    print(f"  3. measure before you remove: calls by name in this session {dict(usage)} - remove v1 when its count stays 0.")
    print("  Never edit a live definition in place: a new name (get_shipment_v2) keeps every cached prefix and every\n"
          "  thinking block that mentions the old one valid, and lets you count who still uses the old one.")


def step_errors() -> None:
    ops = d2.KestrelOps()
    cases = [("unknown ID", "get_invoice", {"invoice_id": "AR-99999"}),
             ("schema violation", "get_vibration_trend", {"serial_number": "KP250-2608-0004"}),
             ("policy gate", "issue_credit_note", {"invoice_id": "AR-90257", "amount_usd": 500.0, "reason": "late delivery"}),
             ("retired tool", "get_shipment", {"shipment_id": "SH-50237"})]
    ops.retired = {"get_shipment": "get_shipment_v2"}
    for label, name, args in cases:
        content, is_error = ops.run(name, args)
        err = json.loads(content)["error"]
        print(f"  {label:<17} {name}: code={err['code']!r}")
        print(wrap(f"message={err['message']!r} next_action={err.get('next_action')!r}", "      "))
    print("  One envelope for every failure: `code` for your dashboards and retries, `message` for the model (what failed and\n"
          "  why, without leaking data), `next_action` for what to do instead. A negative answer ('not shipped yet') is data,\n"
          "  not an error.")


def main() -> None:
    client = get_client()
    header("Lab 07 - Tool contracts and the selection eval")
    if is_mock():
        print("[mock] the stand-in picks the tool whose description shares the most (rare) words with the request - a lexical\n"
              "       proxy. It rewards descriptions written in the users' words and punishes vague ones, as models do, but it\n"
              "       does not reason: run this eval live before you trust a description change.")

    step(1, "Two description sets for the same twelve tools")
    step_descriptions()

    step(2, f"Selection eval: {len(d2.SELECTION_TASKS)} tasks, first tool call vs expected")
    step_eval(client)
    print("\nTwo things about the labels. 'Reduce invoice AR-90257 by 5%...' expects get_invoice: 5% of what? The right FIRST\n"
          "call is the look-up - a first-call eval labels the first step, not the goal. And B was written after reading A's\n"
          "failures on these same tasks, so B's score is optimistic: keep a held-out set (exercise 9, Day 6) before you ship\n"
          "a description change.")

    step(3, "Descriptions are also the retrieval surface: discovery with each set")
    step_discovery(client)

    step(4, "Strict schemas, enums and input_examples")
    step_strict(client)

    step(5, "Capability-scoped toolsets per role (tenants.json)")
    step_scopes(client)

    step(6, "Deprecation behind a versioned name")
    step_deprecation(client)

    step(7, "The error contract")
    step_errors()


if __name__ == "__main__":
    main()
