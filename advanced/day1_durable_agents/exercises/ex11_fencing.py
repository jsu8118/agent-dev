"""Exercise 11 starter - fence the zombie: a worker that lost its lease must stop writing.

Lab 06's false takeover: worker-a runs a tool slower than its lease without heartbeating, worker-b takes the
run over and finishes it, and worker-a - still alive - comes back and logs a second result for the same tool
call. The runtime stops it at its next heartbeat (LeaseLost), but that check comes AFTER the write. Write,
WITHOUT editing advanced/lib/durable.py:

  * FencedStore(RunStore), bound to one worker (owner=...): append() and set_status() succeed only while that
    worker holds the run's lease - checked and written in ONE statement, so nothing can slip in between - and
    raise LeaseLost (advanced.lib.durable's) when it does not.

The DurableRunner then stops at the zombie's first write after the takeover, and that write never lands. This
starter runs the takeover with a FencedStore that does not fence yet and prints the duplicate.
Run: python advanced/day1_durable_agents/exercises/ex11_fencing.py
Solution: advanced/day1_durable_agents/solutions/ex11_fencing.py
"""

from __future__ import annotations

import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from advanced.lib.durable import LeaseLost, RunStore  # noqa: E402,F401  (LeaseLost: for your FencedStore)
from kestrel.support_tools import SupportDesk  # noqa: E402
from labkit import get_client, header  # noqa: E402
from labkit.data import memory_db  # noqa: E402

import _day1 as d1  # noqa: E402


class FencedStore(RunStore):
    """TODO: fence append() and set_status() on lease ownership: a write by a worker that lost the lease raises
    LeaseLost and changes nothing."""

    def __init__(self, path, *, owner: str) -> None:
        super().__init__(path)
        self.owner = owner


# ---------------------------------------------------------------------------- the takeover (keep this)
def worker(store: RunStore, client, db, name: str, *, delay_s: float = 0.0):
    """The support agent on T-1001 with a 1-second lease; get_order takes delay_s seconds and never heartbeats."""
    ticket = d1.ticket("T-1001")
    desk = SupportDesk(ticket["from_email"], db=db, ticket_ref=ticket["ticket_id"])
    tools = d1.desk_executor(desk)

    def execute(tool, tool_input, ctx):
        if tool == "get_order":
            time.sleep(delay_s)
        return tools(tool, tool_input, ctx)

    return d1.support_runner(store, client, execute=execute, worker=name, lease_ttl_s=1.0)


def false_takeover(client, store_cls, store_name: str) -> dict:
    store, db = d1.fresh_store(store_name), memory_db()
    run = store.create("support", input=d1.run_input(d1.ticket("T-1001")), run_id="fenced-takeover")
    a = worker(store_cls(store.path, owner="worker-a"), client, db, "worker-a", delay_s=2.5)
    b = worker(store_cls(store.path, owner="worker-b"), client, db, "worker-b")
    result: dict = {}

    def run_a():
        try:
            result["worker-a"] = d1.outcome_line(a.run(run.id))
        except Exception as exc:                                    # LeaseLost: from the runtime or your fence
            result["worker-a"] = f"stopped: {type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run_a)
    thread.start()
    time.sleep(1.6)                                                 # the lease expired at 1.0 s; worker-a is mid-tool
    result["worker-b"] = d1.outcome_line(b.run(run.id))
    thread.join()
    counts = Counter(e["type"] for e in store.events(run.id))
    result["log"] = ", ".join(f"{k}={counts[k]}" for k in ("model.response", "tool.started", "tool.result"))
    result["get_order results"] = sum(1 for e in store.events(run.id, types=("tool.result",)) if e["name"] == "get_order")
    result["final status"] = store.get(run.id).status
    return result


def main() -> None:
    client = get_client()
    header("Exercise 11 - fencing a zombie worker (starter)")
    for key, value in false_takeover(client, FencedStore, "ex11_starter").items():
        print(f"  {key:<18} {value}")
    print("\nTODO: the log has 2 results for the same get_order: worker-a wrote one after it lost the run, and only")
    print("      its next heartbeat stopped it. Make FencedStore refuse that write, so that the log ends with one")
    print("      get_order result and worker-a stopped by the fence: 'stopped: LeaseLost: worker-a may not write ...'.")


if __name__ == "__main__":
    main()
