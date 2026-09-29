"""Solutions to exercises 2 and 3 - when deferring pays, and when code beats round trips.

Objective
    Reproduce the arithmetic of the two calculation exercises with the numbers stated in exercises/README.md (Claude
    Opus 5 prices, lab 01's and lab 05's measured token counts), print every intermediate figure, and find the
    break-even points.

Concepts
    cache reads at 0.1x, writes at 1.25x, discovery cost in the conversation tail, prefix cost per request, break-even
    discoveries per conversation, context carried by later turns, output tokens priced at 5x input

Run
    python advanced/day2_tools_at_scale/solutions/ex02_ex03_cost_math.py

What to observe
    * Deferring the 111 tools costs about a fifth of loading them at Kestrel's discovery mix - and it would take more
      discoveries per conversation than the conversation has requests to break even.
    * With a 20-tool catalog the answer flips: loading wins below ~0.4 discoveries per conversation.
    * Code beats direct calls from about ten units with caching, from about four without.
"""
# test: expect=break-even

from __future__ import annotations

INPUT = 5.00 / 1_000_000          # Claude Opus 5, $ per input token
OUTPUT_RATIO = 25.00 / 5.00       # output tokens cost 5x input tokens
READ, WRITE = 0.1, 1.25           # cache read / 5-minute cache write multipliers

# Exercise 2 inputs (lab 01)
EXTRA_LOADED = 10_742             # prompt tokens of the 111 non-core definitions, loaded (12,064 - 1,322)
DISCOVERY = 702                   # tokens one search turn adds to the tail (search blocks + ~5 definitions)
REQUESTS = 4                      # requests per conversation
CONVERSATIONS = 400               # per day
MIX = {0: 0.30, 1: 0.50, 2: 0.20} # share of conversations with 0, 1 or 2 discoveries
SMALL_EXTRA = 11 * 97             # a 20-tool catalog: 11 tools beyond the core at ~97 tokens each

# Exercise 3 inputs (lab 05)
DIRECT_PER_UNIT = 1_337 / 11      # tool output the model reads per unit, direct calls
CODE_PER_UNIT = 276 / 11          # stdout per unit, code cell
CELL_OUTPUT = 600                 # output tokens of the code cell
CALL_OUTPUT_PER_UNIT = 27         # output tokens of direct tool calls per unit (~1.4 calls x 20 tokens)
LATER = 6                         # requests after the triage in the same conversation


def discovery_units(k: int) -> float:
    """Input-price units of k discoveries in one conversation: discovery i is written on request i+1 and read after."""
    total = 0.0
    for i in range(1, k + 1):
        reads = max(0, REQUESTS - (i + 1))
        total += DISCOVERY * (WRITE + READ * reads)
    return total


def ex02() -> None:
    print("Exercise 2 - loaded vs deferred (units = input tokens at list price)")
    loaded_warm = REQUESTS * EXTRA_LOADED * READ
    loaded_cold = EXTRA_LOADED * WRITE + (REQUESTS - 1) * EXTRA_LOADED * READ
    deferred = sum(share * discovery_units(k) for k, share in MIX.items())
    print(f"  a) loaded, warm prefix: {REQUESTS} x {EXTRA_LOADED:,} x {READ} = {loaded_warm:,.1f} units per conversation")
    print(f"     deferred: 1 discovery = {discovery_units(1):,.1f} units ({DISCOVERY} x ({WRITE} + {REQUESTS - 2} x {READ}));"
          f" 2 discoveries = {discovery_units(2):,.1f}")
    print(f"     mix {MIX}: {deferred:,.1f} units per conversation")
    print(f"     per day ({CONVERSATIONS} conversations): loaded ${CONVERSATIONS * loaded_warm * INPUT:,.2f}, "
          f"deferred ${CONVERSATIONS * deferred * INPUT:,.2f}, saving ${CONVERSATIONS * (loaded_warm - deferred) * INPUT:,.2f}")
    per_discovery = discovery_units(2) / 2
    print(f"  b) break-even: {loaded_warm:,.1f} / {per_discovery:,.1f} per discovery = {loaded_warm / per_discovery:.1f} "
          f"discoveries per conversation - more than its {REQUESTS} requests")
    print(f"  c) cold prefix on each conversation's first request: loaded {loaded_cold:,.1f} units "
          f"({EXTRA_LOADED:,} x {WRITE} + {REQUESTS - 1} x {EXTRA_LOADED:,} x {READ}); deferred unchanged at {deferred:,.1f}")
    small = REQUESTS * SMALL_EXTRA * READ
    print(f"  d) 20-tool catalog: loaded {small:,.1f} units vs deferred {deferred:,.1f}; break-even at "
          f"{small / per_discovery:.2f} discoveries per conversation - loading wins for this catalog")


def ex03() -> None:
    print("\nExercise 3 - direct calls vs a code cell, per conversation (units = input tokens at list price)")
    cached_factor = WRITE + LATER * READ
    uncached_factor = 1 + LATER
    for label, factor in (("with caching", cached_factor), ("without caching", uncached_factor)):
        direct = DIRECT_PER_UNIT * factor + CALL_OUTPUT_PER_UNIT * OUTPUT_RATIO
        code_slope = CODE_PER_UNIT * factor
        code_fixed = CELL_OUTPUT * OUTPUT_RATIO
        n_star = code_fixed / (direct - code_slope)
        print(f"  {label}: direct {direct:,.1f} units per unit; code {code_fixed:,.0f} + {code_slope:,.1f} per unit; "
              f"break-even at {n_star:.1f} units")
        for n in (11, 110):
            d, c = direct * n * INPUT, (code_fixed + code_slope * n) * INPUT
            print(f"     {n:>3} units: direct ${d:.4f}, code ${c:.4f} ({'code' if c < d else 'direct'} cheaper)")
    print(f"  a) each later request re-reads {DIRECT_PER_UNIT:.0f} tokens per unit (direct) vs {CODE_PER_UNIT:.0f} (code)")


if __name__ == "__main__":
    ex02()
    ex03()
