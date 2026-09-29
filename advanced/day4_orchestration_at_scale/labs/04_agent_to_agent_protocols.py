"""Lab 04 - Agent-to-agent protocols: typed envelopes, one owner per conversation, and three ways to involve another agent.

Objective
    Give the swarm a wire format: a JSON-schema-validated envelope with routing, ownership and a typed payload.
    Then handle the same event - an injury report from Harbor Foods on the recall thread - three ways: delegate
    the assessment to the quality lead (you keep the conversation), hand the conversation over (they own it), or
    broadcast a lot pause to every unit worker. Finally reject a malformed message and a message from an agent
    that does not own the conversation.

Concepts
    typed handoffs with JSON schema, schema versioning, delegation vs handoff vs broadcast, conversation
    ownership and the ownership ledger, ack/reply correlation (in_reply_to), rejected messages, what MCP is for
    (tools and resources for one agent) vs an agent-to-agent envelope (tasks between agents)

Run
    python advanced/day4_orchestration_at_scale/labs/04_agent_to_agent_protocols.py

What to observe
    * Step 1: the validator names every problem of a malformed envelope (unknown type, missing owner, bad id);
      the bus never delivers it.
    * Steps 3-5: the same trigger, three patterns; watch who owns the conversation afterwards and how many
      messages and model calls each pattern costs.
    * Step 4: after the handoff, the customer's follow-up goes straight to the quality lead; the coordinator only
      gets a notification.
    * Step 6: a unit worker that tries to message the customer is rejected: not the conversation owner.
"""
# test: expect=rejected
# test: expect=handoff

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from jsonschema import Draft202012Validator  # noqa: E402

from labkit import MODEL, get_client, header, step, text_of, wrap  # noqa: E402

import _day4 as d4  # noqa: E402

AGENTS = ["coordinator", "quality-lead", "scheduler", "worker-1", "worker-2", "worker-3", "customer"]
ENVELOPE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Kestrel recall swarm envelope",
    "type": "object", "additionalProperties": False,
    "required": ["schema_version", "id", "type", "from", "to", "conversation_id", "conversation_owner", "task_id", "payload", "sent_at"],
    "properties": {
        "schema_version": {"const": "1.0"},
        "id": {"type": "string", "pattern": "^msg_[0-9a-f]{8}$"},
        "type": {"enum": ["delegation", "reply", "handoff", "handoff_ack", "broadcast", "ack", "message", "notification"]},
        "from": {"$ref": "#/$defs/agent"},
        "to": {"anyOf": [{"$ref": "#/$defs/agent"}, {"type": "array", "items": {"$ref": "#/$defs/agent"}, "minItems": 1}]},
        "conversation_id": {"type": "string", "pattern": "^RC-2026-03/C-[0-9]{4}$"},
        "conversation_owner": {"$ref": "#/$defs/agent"},
        "task_id": {"type": "string", "minLength": 1},
        "in_reply_to": {"type": ["string", "null"]},
        "payload": {"type": "object"},
        "sent_at": {"type": "string"},
        "ttl_s": {"type": "integer", "minimum": 1},
    },
    "$defs": {"agent": {"enum": AGENTS}},
}
PAYLOAD_SCHEMAS = {
    "delegation": {"type": "object", "required": ["kind", "body"], "properties": {"kind": {"type": "string"}, "body": {"type": "string"}}},
    "handoff": {"type": "object", "required": ["reason", "state"], "properties": {"reason": {"type": "string"}, "state": {"type": "object"}}},
    "broadcast": {"type": "object", "required": ["topic", "scope"], "properties": {"topic": {"type": "string"}, "scope": {"enum": ["lot", "campaign", "customer"]}}},
    "message": {"type": "object", "required": ["body"]},
}
OWNER_ONLY = {"message", "handoff", "delegation"}         # only the conversation's owner may send these on it


class Rejected(Exception):
    pass


class Bus:
    """Validates, enforces ownership, delivers. Every accepted envelope is logged; rejected ones never reach anyone."""

    def __init__(self) -> None:
        self.validator = Draft202012Validator(ENVELOPE_SCHEMA)
        self.owners: dict[str, str] = {}
        self.log: list[dict] = []
        self.rejected: list[tuple[dict, list[str]]] = []
        self.handlers: dict = {}
        self.counter = 0

    def envelope(self, type_: str, sender: str, to, conversation_id: str, task_id: str, payload: dict, in_reply_to=None) -> dict:
        self.counter += 1
        return {"schema_version": "1.0", "id": "msg_" + hashlib.sha256(str(self.counter).encode()).hexdigest()[:8],
                "type": type_, "from": sender, "to": to, "conversation_id": conversation_id,
                "conversation_owner": self.owners.get(conversation_id, "coordinator"), "task_id": task_id,
                "in_reply_to": in_reply_to, "payload": payload,
                "sent_at": dt.datetime(2026, 9, 18, 9, 0, tzinfo=dt.timezone.utc).isoformat()}

    def problems(self, env: dict) -> list[str]:
        out = [f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}" for e in
               sorted(self.validator.iter_errors(env), key=lambda e: list(e.absolute_path))]
        schema = PAYLOAD_SCHEMAS.get(env.get("type"))
        if schema and not out:
            out += [f"payload/{'/'.join(str(p) for p in e.absolute_path)}: {e.message}"
                    for e in Draft202012Validator(schema).iter_errors(env.get("payload", {}))]
        if not out:
            owner = self.owners.get(env["conversation_id"], "coordinator")
            if env["type"] in OWNER_ONLY and env["from"] != owner and env["from"] != "customer":
                out.append(f"ownership: {env['from']} is not the owner of {env['conversation_id']} ({owner} is)")
            if env["conversation_owner"] != owner:
                out.append(f"ownership: envelope claims owner {env['conversation_owner']}, ledger says {owner}")
        return out

    def send(self, env: dict) -> list[dict]:
        """Deliver to every recipient and return their replies (already validated and logged)."""
        problems = self.problems(env)
        if problems:
            self.rejected.append((env, problems))
            raise Rejected("; ".join(problems))
        self.log.append(env)
        if env["type"] == "handoff":
            self.owners[env["conversation_id"]] = env["to"]           # the ledger changes on the handoff itself
        replies = []
        for recipient in (env["to"] if isinstance(env["to"], list) else [env["to"]]):
            handler = self.handlers.get(recipient)
            if handler is None:
                continue
            reply = handler(env)
            if reply is not None:
                if self.problems(reply):
                    self.rejected.append((reply, self.problems(reply)))
                    continue
                self.log.append(reply)
                replies.append(reply)
        return replies


class LLMAgent:
    """An agent = a role prompt + the model. It reads an envelope and returns a JSON payload; the harness wraps
    the payload into a reply envelope (the model never sets routing or ownership fields)."""

    def __init__(self, client, bus: Bus, name: str, role: str, meter: d4.Meter, *, serials_: list[str] | None = None) -> None:
        self.client, self.bus, self.name, self.role, self.meter = client, bus, name, role, meter
        self.serials = serials_ or []
        self.calls = 0
        bus.handlers[name] = self.handle

    def system(self) -> str:
        return (f'<adv_day4_a2a role="{self.role}" name="{self.name}" serials="{" ".join(self.serials)}">\n'
                f"You are the {self.name} agent of Kestrel's recall swarm. Read the envelope and answer with ONE JSON object "
                "(the payload of your reply). Never promise compensation; never change hazard wording.\n</adv_day4_a2a>")

    def handle(self, env: dict) -> dict | None:
        self.calls += 1
        response = self.client.messages.create(model=MODEL, max_tokens=1500, system=self.system(),
                                               messages=[{"role": "user", "content": f"<envelope>\n{json.dumps(env)}\n</envelope>"}])
        self.meter.add_response(env["type"], response)
        try:
            payload = json.loads(text_of(response))
        except ValueError:
            payload = {"raw": text_of(response)}
        reply_type = {"delegation": "reply", "handoff": "handoff_ack", "broadcast": "ack", "message": "reply"}.get(env["type"])
        if reply_type is None:
            return None
        return self.bus.envelope(reply_type, self.name, env["from"], env["conversation_id"], env["task_id"], payload, in_reply_to=env["id"])


def show(env: dict) -> str:
    to = ", ".join(env["to"]) if isinstance(env["to"], list) else env["to"]
    return f"    {env['type']:<12} {env['from']} -> {to}  owner={env['conversation_owner']}  payload={json.dumps(env['payload'])[:90]}"


def main() -> None:
    header("Lab 04 - Agent-to-agent protocols")
    d4.mock_note("the agents are rule-based stand-ins that read the envelope they are sent; the schema, the bus, the ownership "
                 "ledger and the rejections are real code.")
    client = get_client()
    bus, meter = Bus(), d4.Meter()
    reply = next(r for r in d4.replies() if r["reply_id"] == "RPL-018")
    conv = reply["thread"]
    lot_serials = [u["serial_number"] for u in d4.units() if u["lot"] == "PS-2608-B"]
    coordinator = LLMAgent(client, bus, "coordinator", "coordinator", meter)
    quality = LLMAgent(client, bus, "quality-lead", "quality_lead", meter)
    workers = [LLMAgent(client, bus, f"worker-{i}", "unit_worker", meter, serials_=lot_serials[j::3]) for i, j in ((1, 0), (2, 1), (3, 2))]

    step(1, "The envelope: schema, a valid message, and two malformed ones")
    print("  required: " + ", ".join(ENVELOPE_SCHEMA["required"]) + f"; types: {', '.join(ENVELOPE_SCHEMA['properties']['type']['enum'])}")
    good = bus.envelope("delegation", "coordinator", "quality-lead", conv, "T-1", {"kind": "safety_event_assessment", "body": reply["body"]})
    print(f"  valid envelope: {bus.problems(good) or 'no problems'}")
    bad = {**good, "type": "handof", "id": "42", "schema_version": "2.0"}
    bad.pop("conversation_owner")
    print("  malformed envelope -> " + "; ".join(bus.problems(bad)))
    bad2 = bus.envelope("handoff", "coordinator", "quality-lead", conv, "T-1", {"reason": "injury reported"})
    print("  handoff without state -> " + "; ".join(bus.problems(bad2)))
    print("  Neither one is delivered: validation happens on the bus, before any agent spends a token on it.")

    step(2, f"The trigger: {reply['reply_id']} from {reply['from_name']} on thread {conv}")
    print(wrap(reply["body"], "    "))
    triage = coordinator.handle(bus.envelope("message", "customer", "coordinator", conv, "T-0", {"body": reply["body"], "lot": "PS-2608-B"}))
    verdict = triage["payload"] if triage else {}
    print(f"  coordinator's triage: {json.dumps(verdict)}")

    step(3, "Pattern A - delegation: ask the quality lead for an assessment, keep the conversation")
    env = bus.envelope("delegation", "coordinator", "quality-lead", conv, "T-1", {"kind": "safety_event_assessment", "body": reply["body"], "lot": "PS-2608-B"})
    print(show(env))
    for r in bus.send(env):
        print(show(r))
    print(f"  owner of {conv} after delegation: {bus.owners.get(conv, 'coordinator')}")

    step(4, "Pattern B - handoff: the quality lead takes over the conversation")
    env = bus.envelope("handoff", "coordinator", "quality-lead", conv, "T-2",
                       {"reason": "stop condition: fluid release with an operator injured", "state": {"lot": "PS-2608-B", "unit": "KP250-2608-0004",
                        "last_customer_message": reply["reply_id"], "bookings_on_hold": True}})
    print(show(env))
    for r in bus.send(env):
        print(show(r))
    print(f"  owner of {conv} after handoff: {bus.owners[conv]}")
    follow_up = bus.envelope("message", "customer", bus.owners[conv], conv, "T-3",
                             {"body": "The operator is back at work. Do you still need to visit, and when?"})
    print("  a follow-up from the customer is routed to the current owner:")
    print(show(follow_up))
    for r in bus.send(follow_up):
        print(show(r))
    note = bus.envelope("notification", "quality-lead", "coordinator", conv, "T-3", {"event": "customer_replied", "owner": "quality-lead"})
    bus.send(note)
    print(show(note))

    step(5, "Pattern C - broadcast: pause the lot for every unit worker")
    env = bus.envelope("broadcast", "coordinator", [w.name for w in workers], conv, "T-4",
                       {"topic": "lot_pause", "scope": "lot", "lot": "PS-2608-B", "serials": lot_serials,
                        "reason": "safety event under investigation; no new bookings"})
    print(show(env))
    for r in bus.send(env):
        print(show(r))

    step(6, "A message from the wrong agent, and the comparison")
    rogue = bus.envelope("message", "worker-2", "customer", conv, "T-5", {"body": "We will visit Tuesday and credit you $500."})
    try:
        bus.send(rogue)
    except Rejected as exc:
        print(f"  worker-2 -> customer rejected: {exc}")
    rows = []
    for pattern, kinds, use in (("delegation", ("delegation", "reply"), "you need a result, you keep the customer"),
                                ("handoff", ("handoff", "handoff_ack", "message", "notification"), "another agent must own what happens next"),
                                ("broadcast", ("broadcast", "ack"), "everyone must know; nobody replies to the customer")):
        msgs = [e for e in bus.log if e["type"] in kinds]
        t = meter.roles.get(kinds[0], d4.RoleTotals())
        rows.append([pattern, len(msgs), t.calls, f"{t.input_tokens:,}", f"{t.output_tokens:,}", d4.money(t.cost), use])
    print(d4.table(rows, ["pattern", "messages", "model calls", "input tok", "output tok", "cost", "use when"]))
    print(f"  bus log: {len(bus.log)} accepted envelopes, {len(bus.rejected)} rejected; final owner of {conv}: {bus.owners[conv]}")
    print(wrap("MCP is not this protocol: MCP connects ONE agent to tools and resources (a server offers tools, the agent calls them). "
               "Agents talking to agents need tasks, replies, ownership and acknowledgements - an envelope like this one, or an "
               "A2A-style task object. Use MCP for the desk, the calendar and the ERP; use envelopes between the coordinator, "
               "the workers and the quality lead.", "  "))


if __name__ == "__main__":
    main()
