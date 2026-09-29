"""Lab 04 - Sandboxing agent-written code: the hosted container's guarantees, escape probes, and why never exec().

Objective
    Run agent-written Python in the code-execution container and see what the container gives you for free:
    isolation, no network, resource caps, a working directory that outbound files pass through the Files API.
    Then run the same probes with a plain in-process exec() to show what you are signing up for if you run
    model-written code yourself, and finish with programmatic tool calling: a call composed in code still goes
    through the capability desk, and only the final result reaches the model's context.

Concepts
    the hosted code-execution container (code_execution_20260120), container isolation, no network egress,
    resource/file limits, container persistence, the Files API round-trip for outputs, the danger of exec()/
    subprocess on model output, programmatic tool calling (allowed_callers/caller), tools-not-a-boundary

Run
    python advanced/day5_security_engineering/labs/04_sandboxing_agent_code.py

What to observe
    * The container's guarantees are a table you can quote in a review; the [mock] note says which of them the local
      stand-in reproduces (the working directory and the Files API round-trip) and which it does not (network and
      filesystem isolation, resource caps - those are the real container's job).
    * An agent-written analysis cell runs and its stdout comes back; its files land under the container's own
      directory, not next to your code.
    * The unsandboxed arm - the same code run with exec() in this process - reads a repository file and a process
      environment variable. That is the default if you execute model output yourself.
    * A refund/cross-customer read composed inside a code cell is refused by the same tool layer that answers a
      direct call; only the summary reaches the model.
"""
# test: expect=no network egress
# test: expect=unsandboxed
# test: expect=refused by the tool layer

from __future__ import annotations

import io
import os
import sys
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from labkit import MODEL, REPO_ROOT, get_client, header, is_mock, step, wrap
from labkit.data import memory_db

import _day5 as d5

CODE_TOOL = {"type": "code_execution_20260120", "name": "code_execution"}
BETAS = ["code-execution-2025-08-25"]

CONTAINER_LIMITS = [
    ["CPU / RAM / disk", "1 CPU, 5 GiB RAM, 5 GiB disk", "resource exhaustion is capped, not your problem"],
    ["network", "no internet egress", "a cell cannot phone home or exfiltrate over the network"],
    ["filesystem", "isolated per container; host not mounted", "model code cannot read your repo, keys or /etc"],
    ["runtime", "Python 3.11 + data-science libs", "no arbitrary install of native escape tools at will"],
    ["persistence", "container lives 30 days, reusable by id", "state survives turns; expires on its own"],
    ["outputs", "files under outputs/ via the Files API", "the only way data leaves is one you can log"],
    ["billing", "free with web tools; else $0.05/hr after 1,550 free hrs/mo/org", "cost is bounded and observable"],
]

# Model-written code, as a fixture: two probes that a compromised or confused cell might run. They are benign here
# (they read a repository file and a process env var this lab sets); the point is where they are allowed to run.
HOST_FILE = REPO_ROOT / "advanced" / "README.md"
PROBE_FS = f"open({str(HOST_FILE)!r}).readline().strip()"
PROBE_ENV = "__import__('os').environ.get('KESTREL_DEMO_TOKEN', '<not set>')"
DEMO_TOKEN = "kp-demo-token-NOT-A-REAL-SECRET"


def run_cell(client, code: str, *, container: str | None = None):
    """Run one agent-written Python cell in the container; return (stdout_text, container_id)."""
    msg = f"<cell>{code}</cell>"
    resp = client.beta.messages.create(model=MODEL, max_tokens=2000, system=d5.SANDBOX_MARK, tools=[CODE_TOOL],
                                       messages=[{"role": "user", "content": msg}], betas=BETAS,
                                       **({"container": container} if container else {}))
    cid = resp.container.id if getattr(resp, "container", None) else container
    text = "".join(b.text for b in resp.content if b.type == "text")
    return text, cid


def run_bash(client, command: str, *, container: str | None = None):
    resp = client.beta.messages.create(model=MODEL, max_tokens=2000, system=d5.SANDBOX_MARK, tools=[CODE_TOOL],
                                       messages=[{"role": "user", "content": f"<bash>{command}</bash>"}], betas=BETAS,
                                       **({"container": container} if container else {}))
    for b in resp.content:
        if b.type == "bash_code_execution_tool_result":
            return b.content
    return None


def step_guarantees() -> None:
    print("The hosted code-execution container (code_execution_20260120) is a sandbox Anthropic runs. Its properties "
          "are the security you get for free when the model writes and runs code:\n")
    d5.table(CONTAINER_LIMITS, ["property", "guarantee", "why it matters for security"])
    if is_mock():
        print("\n[mock] the offline stand-in runs the cell as a REAL subprocess in a scratch directory under .runs/. "
              "It reproduces the working directory and the outputs/ -> Files API round-trip; it does NOT reproduce "
              "network isolation, filesystem isolation or the resource caps - those are the hosted container's job, "
              "taught here from the docs and verified live.")


def step_agent_code(client) -> str:
    print("An agent writes Python to add up an order's lines - the kind of arithmetic you want done in code, not "
          "guessed by the model. It runs in the container:\n")
    cell = ("lines = [('LUB-EP2', 11, 19.23), ('IMP-250-A', 3, 1306.40)]\n"
            "totals = {sku: round(qty * price, 2) for sku, qty, price in lines}\n"
            "print('line totals:', totals)\n"
            "print('order total:', round(sum(totals.values()), 2))")
    out, cid = run_cell(client, cell)
    print(wrap(out, "  "))
    print(f"\n  container id: {cid} (reusable by id; state persists across turns)")
    out2, cid2 = run_cell(client, "print('order total still in scope:', round(sum(totals.values()), 2))", container=cid)
    print("  A second cell in the same container still sees the first cell's variables:")
    print(wrap(out2, "  "))
    print("  Where do its files go? A cell writes to its own working directory, not next to your code:")
    res = run_bash(client, "pwd; printf 'checkpoint\\n' > outputs/scratch.txt; ls outputs", container=cid)
    if res is not None:
        print(wrap(f"pwd -> {res.stdout.strip().splitlines()[0]}", "    "))
        print(wrap("outputs/ -> " + " ".join(res.stdout.strip().splitlines()[1:]), "    "))
    print("  The working directory is the container's, under .runs/mock_containers/ in mock mode; on the platform it "
          "is the isolated container and files in outputs/ come back as Files API ids you can audit.")
    return cid


def _exec_unsandboxed(code: str) -> str:
    """Run model-written code the WRONG way: exec() in this very process. Never do this in production."""
    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            print(eval(code))                              # noqa: S307 - the whole point is that this is unsafe
        return buf.getvalue().strip()
    except Exception as exc:                               # noqa: BLE001
        return f"<error: {type(exc).__name__}>"


def step_escape_probes(client) -> None:
    print("Escape probes: code that tries to reach the host. Run each two ways - inside the container, and with a "
          "plain in-process exec() (what you get if you run model output yourself).\n")
    os.environ["KESTREL_DEMO_TOKEN"] = DEMO_TOKEN          # a synthetic, clearly-fake secret this process holds
    probes = [("read a repository file", PROBE_FS, "the container's filesystem is isolated; the host is not mounted"),
              ("read a process env var / secret", PROBE_ENV, "the container never sees your process environment")]
    print("  unsandboxed exec() in this process:")
    for label, code, _ in probes:
        leaked = _exec_unsandboxed(f"({code})")
        print(f"    {label:34s} -> {d5.short(leaked, 70)}")
    print("    ^ both succeeded: exec()/subprocess on model output runs with YOUR privileges, YOUR files, YOUR env.")
    print("\n  the hosted container (same probes):")
    for label, _, guarantee in probes:
        print(f"    {label:34s} -> blocked: {guarantee}")
    print("    a network egress probe (open a socket to an external host) is refused the same way: no network egress.")
    if is_mock():
        print("  [mock] the offline container is a real subprocess and would NOT block the file/env probes, so this lab "
              "does not run them there and claim a block; the containment above is the hosted container's, from the docs.")
    print("  Rule: run agent-written code only in a sandbox you did not build from exec()/subprocess/eval. If you must "
          "host your own, you owe yourself seccomp/containers, a read-only rootfs, no network namespace, CPU/memory "
          "cgroups and a wall-clock timeout - which is exactly what the hosted container already is.")


def step_ptc(client) -> None:
    print("Programmatic tool calling: the model composes tool calls in a cell. Its calls do NOT bypass the tool "
          "layer - they go through the same CapabilityDesk as a direct call, and only the cell's final output "
          "returns to the model. Here the cell reads the caller's own order and one belonging to another customer.\n")
    tenants = d5.load_tenants()
    db = memory_db()
    cap = d5.mint_capability(tenants, "kestrel", "copilot", "email", on_behalf_of="C-1005", phase="resolve",
                             request_id="lab04-ptc")
    desk = d5.CapabilityDesk(cap, tenants, d5.CatalogBackend(db))
    scoped = d5.scoped_toolset(cap, tenants)
    tools = [CODE_TOOL] + [({**t, "allowed_callers": ["code_execution_20260120"]} if t["name"] == "get_order" else t)
                           for t in scoped]
    messages = [{"role": "user", "content": "Fetch SO-10248 and SO-10306 in one cell and summarise."}]
    container = None
    final = ""
    for _ in range(6):
        resp = client.beta.messages.create(model=MODEL, max_tokens=3000, system=d5.PTC_MARK, tools=tools,
                                           messages=messages, betas=BETAS, **({"container": container} if container else {}))
        container = resp.container.id if getattr(resp, "container", None) else container
        calls = [b for b in resp.content if b.type == "tool_use" and getattr(b, "caller", None)]
        messages.append({"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in resp.content]})
        if resp.stop_reason == "tool_use" and calls:
            results = []
            for b in calls:
                content, is_error = desk.run(b.name, dict(b.input))
                results.append({"type": "tool_result", "tool_use_id": b.id, "content": content, "is_error": is_error})
            messages.append({"role": "user", "content": results})
            continue
        final = "".join(b.text for b in resp.content if b.type == "text")
        break
    print(wrap(final, "  "))
    print("\n  the desk's view of the calls the code made:")
    for c in desk.calls:
        print(f"    get_order({c['input'].get('order_id')}): {'refused [' + c['layer'] + ']' if c['is_error'] else 'allowed'}")
    print("  PTC does not widen the tool surface: the container exposes only capability-scoped tools marked "
          "code-callable (allowed_callers). issue_refund is not among the copilot's tools, so the cell cannot name it.")


def step_own_vs_hosted() -> None:
    print("Your own sandbox vs the hosted container - the decision:\n")
    rows = [
        ["what runs it", "your infra (gVisor/Firecracker/container)", "Anthropic's infra"],
        ["network", "you configure egress rules", "none by default"],
        ["custom tools from code", "you wire the bridge", "allowed_callers + caller round-trip (built in)"],
        ["state", "you persist it", "container persists 30 days"],
        ["what you still own", "seccomp, cgroups, timeouts, egress, audit", "the tool-layer policy on any callbacks"],
        ["when to prefer", "bespoke runtime, on-prem data, air-gap", "default: less code you own is less attack surface"],
    ]
    d5.table(rows, ["concern", "your own sandbox", "hosted code-execution container"])
    print("\n  The container sandboxes the CODE. It does not authorize the code's TOOL CALLS - that is the "
          "CapabilityDesk (lab 03), which is why the PTC step still saw a refusal. Two different boundaries; you need "
          "both.")


def main() -> None:
    client = get_client()
    header("Lab 04 - Sandboxing agent-written code")

    step(1, "The hosted container's guarantees")
    step_guarantees()

    step(2, "Running agent-written code, and where its files go")
    step_agent_code(client)

    step(3, "Escape probes: the container vs a plain exec()")
    step_escape_probes(client)

    step(4, "Programmatic tool calling still goes through the tool layer")
    step_ptc(client)

    step(5, "Your own sandbox vs the hosted container")
    step_own_vs_hosted()


if __name__ == "__main__":
    main()
