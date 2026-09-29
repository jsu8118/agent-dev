"""Lab 04 - Agent-to-agent protocols: typed envelopes, one owner per conversation, and three ways to involve another agent.

Objective
    Give the swarm a wire format: a JSON-schema-validated envelope with routing, ownership, correlation and a
    typed payload per message type, plus A2A-style agent cards that say what each agent accepts. Then handle the
    same event - an injury report from Harbor Foods on the recall thread, followed by the customer's follow-up
    question - three ways: delegate the assessment to the quality lead (you keep the conversation), hand the
    conversation over (they own it), or broadcast a lot pause to every unit worker. Finally watch the bus reject
    five bad messages before any agent spends a token on them.

Concepts
    typed messages with JSON Schema, schema versioning, agent cards (capability discovery), delegation vs handoff
    vs broadcast, conversation ownership and the ownership ledger, correlation (in_reply_to), rejected messages,
    what MCP is for (one agent's tools and resources) vs an agent-to-agent envelope (tasks between agents)

Run
    python advanced/day4_orchestration_at_scale/labs/04_agent_to_agent_protocols.py

What to observe
    * Step 1: the envelope schema and the agent cards; a valid envelope passes, and the validator names every
      problem of a malformed one.
    * Steps 3-5: the same trigger and follow-up, three patterns on fresh buses; watch who owns the conversation
      afterwards, who answers the customer, and how many messages and model calls each pattern costs.
    * Step 4: after the handoff the customer's follow-up goes straight to the quality lead; the coordinator only
      gets a notification and makes no model call.
    * Step 6: five rejections - two for ownership (a worker, and the coordinator after it handed off), one each for
      the payload schema, a capability and correlation - then the comparison table.
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

AGENTS = ["coordinator", "quality-lead", "worker-1", "worker-2", "worker-3"]
TYPES = ["delegation", "result", "handoff", "handoff_accept", "broadcast", "ack", "inbound", "outbound", "notification"]
ENVELOPE_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "title": "Kestrel recall swarm envelope", "type": "object", "additionalProperties": False,
    "required": ["schema_version", "id", "type", "from", "to", "conversation_id", "conversation_owner", "task_id", "payload",
                 "sent_at"],
    "properties": {
        "schema_version": {"const": "1.0"},
        "id": {"type": "string", "pattern": "^msg_[0-9a-f]{8}$"},
        "type": {"enum": TYPES},
        "from": {"$ref": "#/$defs/party"},
        "to": {"anyOf": [{"$ref": "#/$defs/party"}, {"type": "array", "items": {"$ref": "#/$defs/agent"}, "minItems": 1}]},
        "conversation_id": {"type": "string", "pattern": "^RC-2026-03/C-[0-9]{4}$"},
        "conversation_owner": {"$ref": "#/$defs/agent"},
        "task_id": {"type": "string", "minLength": 1},
        "in_reply_to": {"type": ["string", "null"]},
        "payload": {"type": "object"},
        "sent_at": {"type": "string"},
        "ttl_s": {"type": "integer", "minimum": 1},
    },
    "$defs": {"agent": {"enum": AGENTS}, "party": {"enum": AGENTS + ["customer"]}},
}
PAYLOAD_SCHEMAS = {
    "delegation": {"type": "object", "required": ["kind", "question"],
                   "properties": {"kind": {"enum": ["safety_event_assessment", "followup_question", "unit_plan"]},
                                  "question": {"type": "string"}, "context": {"type": "object"}}},
    "result": {"type": "object", "required": ["verdict"]},
    "handoff": {"type": "object", "required": ["reason", "state"],
                "properties": {"reason": {"type": "string"},
                               "state": {"type": "object", "required": ["lot", "unit", "last_customer_message",
                                                                        "open_actions", "constraints"]}}},
    "handoff_accept": {"type": "object", "required": ["accepted", "owner"]},
    "broadcast": {"type": "object", "required": ["topic", "scope", "serials"],
                  "properties": {"scope": {"enum": ["lot", "campaign", "customer"]}, "serials": {"type": "array"}}},
    "ack": {"type": "object", "required": ["ack"]},
    "inbound": {"type": "object", "required": ["body"]},
    "outbound": {"type": "object", "required": ["body", "template"],
                 "properties": {"hazard_wording_changed": {"const": False}}},
    "notification": {"type": "object", "required": ["event"]},
}
# A2A-style agent cards: who an agent is and what it accepts. The bus refuses a message its recipient does not accept.
AGENT_CARDS = {
    "coordinator": {"description": "Owns every customer conversation of the campaign by default; triages, delegates, "
                                   "hands off, takes conversations back.",
                    "accepts": ["inbound", "result", "handoff", "handoff_accept", "ack", "notification"]},
    "quality-lead": {"description": "Safety events, incident files, hazard wording; can take over a customer conversation.",
                     "accepts": ["delegation", "handoff", "inbound"], "skills": ["safety_event_assessment", "followup_question"]},
    **{f"worker-{i}": {"description": "Plans the remedy for its own serials.", "accepts": ["delegation", "broadcast"],
                       "skills": ["unit_plan"]} for i in (1, 2, 3)},
}
OWNER_ONLY = {"delegation", "handoff", "outbound"}      # only the conversation's current owner may send these
REPLIES = {"result": "delegation", "handoff_accept": "handoff", "ack": "broadcast"}   # reply type -> request type


class Rejected(Exception):
    pass


class Bus:
    """Validates, enforces ownership and capabilities, correlates replies, delivers. Accepted envelopes are logged;
    rejected ones never reach an agent."""

    buses = 0

    def __init__(self) -> None:
        Bus.buses += 1
        self.name = f"bus{Bus.buses}"
        self.validator = Draft202012Validator(ENVELOPE_SCHEMA)
        self.owners: dict[str, str] = {}
        self.log: list[dict] = []
        self.rejected: list[tuple[dict, list[str]]] = []
        self.counter = 0

    def owner(self, conversation_id: str) -> str:
        return self.owners.get(conversation_id, "coordinator")

    def envelope(self, type_: str, sender: str, to, conversation_id: str, task_id: str, payload: dict,
                 in_reply_to: str | None = None) -> dict:
        self.counter += 1
        return {"schema_version": "1.0", "id": "msg_" + hashlib.sha256(f"{self.name}:{self.counter}".encode()).hexdigest()[:8],
                "type": type_, "from": sender, "to": to, "conversation_id": conversation_id,
                "conversation_owner": self.owner(conversation_id), "task_id": task_id, "in_reply_to": in_reply_to,
                "payload": payload, "sent_at": dt.datetime(2026, 9, 20, 16, 0, tzinfo=dt.timezone.utc).isoformat()}

    def problems(self, env: dict) -> list[str]:
        out = [f"{'/'.join(str(p) for p in e.absolute_path) or '<envelope>'}: {e.message}"
               for e in sorted(self.validator.iter_errors(env), key=lambda e: list(e.absolute_path))]
        if out:
            return out
        schema = PAYLOAD_SCHEMAS[env["type"]]
        out += [f"payload{''.join('/' + str(p) for p in e.absolute_path)}: {e.message}"
                for e in Draft202012Validator(schema).iter_errors(env["payload"])]
        owner = self.owner(env["conversation_id"])
        if env["type"] in OWNER_ONLY and env["from"] != owner:
            out.append(f"ownership: {env['from']} may not send {env['type']!r} on {env['conversation_id']} ({owner} owns it)")
        if env["conversation_owner"] != owner:
            out.append(f"ownership: envelope says the owner is {env['conversation_owner']}, the ledger says {owner}")
        for recipient in (env["to"] if isinstance(env["to"], list) else [env["to"]]):
            card = AGENT_CARDS.get(recipient)
            if card is not None and env["type"] not in card["accepts"]:
                out.append(f"capability: {recipient} does not accept {env['type']!r} (its card: {', '.join(card['accepts'])})")
        if env["type"] in REPLIES:
            request = next((e for e in self.log if e["id"] == env.get("in_reply_to")), None)
            if request is None or request["type"] != REPLIES[env["type"]]:
                out.append(f"correlation: {env['type']!r} must answer an open {REPLIES[env['type']]!r} (in_reply_to)")
        if env["type"] == "inbound" and env["to"] != owner:
            out.append(f"routing: inbound messages go to the conversation owner ({owner}), not {env['to']}")
        return out

    def send(self, env: dict) -> dict:
        problems = self.problems(env)
        if problems:
            self.rejected.append((env, problems))
            raise Rejected("; ".join(problems))
        self.log.append(env)
        if env["type"] == "handoff_accept":                 # the ledger changes when the new owner accepts
            self.owners[env["conversation_id"]] = env["from"]
        return env


class Agent:
    """An agent = a role prompt + the model. It reads an envelope and answers with a JSON payload; the harness wraps
    the payload in a reply envelope, so the model never sets routing, ownership or correlation fields."""

    def __init__(self, client, name: str, role: str, meter: d4.Meter, *, serials_: list[str] | None = None) -> None:
        self.client, self.name, self.role, self.meter = client, name, role, meter
        self.serials = serials_ or []

    def system(self) -> str:
        card = AGENT_CARDS[self.name]
        return (f'<adv_day4_a2a role="{self.role}" name="{self.name}" serials="{" ".join(self.serials)}">\n'
                f"You are the {self.name} agent of Kestrel's recall swarm: {card['description']} Read the envelope and "
                "answer with ONE JSON object: the payload of your reply. Never promise compensation; never change "
                "hazard wording.\n</adv_day4_a2a>")

    def read(self, env: dict, pattern: str) -> dict:
        response = self.client.messages.create(model=MODEL, max_tokens=1500, system=self.system(),
                                               messages=[{"role": "user", "content": f"<envelope>\n{json.dumps(env)}\n</envelope>"}])
        self.meter.add_response(pattern, response)
        try:
            return json.loads(text_of(response))
        except ValueError:
            return {"raw": text_of(response)}


def show(env: dict) -> str:
    to = ", ".join(env["to"]) if isinstance(env["to"], list) else env["to"]
    return f"    {env['type']:<14} {env['from']:>12} -> {to:<30} owner={env['conversation_owner']:<12} " \
           f"{json.dumps(env['payload'])[:62]}"


class World:
    """A fresh bus and fresh agents for one pattern, so the three patterns start from the same state."""

    def __init__(self, client, meter: d4.Meter, lot_serials: list[str]) -> None:
        self.bus = Bus()
        self.agents = {"coordinator": Agent(client, "coordinator", "coordinator", meter),
                       "quality-lead": Agent(client, "quality-lead", "quality_lead", meter)}
        for i in (1, 2, 3):
            self.agents[f"worker-{i}"] = Agent(client, f"worker-{i}", "unit_worker", meter, serials_=lot_serials[i - 1::3])

    def post(self, env: dict) -> dict:
        print(show(self.bus.send(env)))
        return env


def main() -> None:
    header("Lab 04 - Agent-to-agent protocols")
    d4.mock_note("the agents are rule-based stand-ins that read the envelope they are sent; the schemas, agent cards, bus, "
                 "ownership ledger and rejections are real code.")
    client = get_client()
    meter = d4.Meter()
    report = next(r for r in d4.replies() if r["reply_id"] == "RPL-018")
    conv = report["thread"]
    unit_ = d4.unit(next(s for s in d4.serials() if d4.unit(s)["customer_id"] == report["customer_id"]))
    lot = unit_["lot"]
    lot_serials = [u["serial_number"] for u in d4.units() if u["lot"] == lot]
    follow_up = "The operator is back at work. Do you still need to visit, and when?"

    step(1, "The envelope and the agent cards")
    print("  required: " + ", ".join(ENVELOPE_SCHEMA["required"]))
    print("  types: " + ", ".join(f"{t} ({'owner only' if t in OWNER_ONLY else 'answers a ' + REPLIES[t] if t in REPLIES else 'any'})"
                                  for t in TYPES))
    for name, card in AGENT_CARDS.items():
        if name in ("coordinator", "quality-lead", "worker-1"):
            print(f"  card {name:<13} accepts {', '.join(card['accepts'])}")
    probe = Bus()
    good = probe.envelope("delegation", "coordinator", "quality-lead", conv, "T-1",
                          {"kind": "safety_event_assessment", "question": "Is this a stop condition?"})
    print(f"  a well-formed delegation: {probe.problems(good) or 'no problems'}")
    bad = {**good, "type": "handof", "id": "42", "schema_version": "2.0"}
    bad.pop("conversation_owner")
    print("  a malformed one:")
    for problem in probe.problems(bad):
        print(f"    - {problem}")

    step(2, f"The trigger: {report['reply_id']} from {report['from_name']} on {conv}, then a follow-up")
    print(wrap(report["body"], "    "))
    print(wrap(f"Follow-up a day later: {follow_up}", "    "))

    results = {}
    step(3, "Pattern A - delegation: ask the quality lead, keep the conversation")
    w = World(client, meter, lot_serials)
    for n, body in enumerate((report["body"], follow_up), 1):
        flags = ["safety_event_open"] if n == 2 else []
        inbound = w.post(w.bus.envelope("inbound", "customer", w.bus.owner(conv), conv, f"A-{n}",
                                        {"body": body, "lot": lot, "thread_flags": flags}))
        triage = w.agents["coordinator"].read(inbound, "delegation")
        ask = w.post(w.bus.envelope("delegation", "coordinator", "quality-lead", conv, f"A-{n}",
                                    {"kind": "followup_question" if n == 2 else "safety_event_assessment",
                                     "question": "Is this a stop condition, and what may we tell the customer?" if n == 1
                                     else "Can the visit go ahead, and when?", "context": {"triage": triage, "lot": lot}}))
        answer = w.post(w.bus.envelope("result", "quality-lead", "coordinator", conv, f"A-{n}",
                                       w.agents["quality-lead"].read(ask, "delegation"), in_reply_to=ask["id"]))
        reply = w.agents["coordinator"].read(answer, "delegation")
        w.post(w.bus.envelope("outbound", "coordinator", "customer", conv, f"A-{n}",
                              {k: reply[k] for k in ("body", "template", "hazard_wording_changed") if k in reply}))
    results["delegation"] = (w, "coordinator", "coordinator (relaying)")
    print(f"  owner of {conv} after delegation: {w.bus.owner(conv)} - every later customer message still comes to the coordinator")

    step(4, "Pattern B - handoff: the quality lead takes over the conversation")
    w = World(client, meter, lot_serials)
    inbound = w.post(w.bus.envelope("inbound", "customer", w.bus.owner(conv), conv, "B-1", {"body": report["body"], "lot": lot}))
    triage = w.agents["coordinator"].read(inbound, "handoff")
    handoff = w.post(w.bus.envelope("handoff", "coordinator", "quality-lead", conv, "B-2", {
        "reason": triage.get("reason", "safety event"),
        "state": {"lot": lot, "unit": unit_["serial_number"], "last_customer_message": report["reply_id"],
                  "open_actions": triage.get("actions", []), "constraints": ["interim measures only", "no compensation",
                                                                              "hazard wording unchanged"]}}))
    accepted = w.agents["quality-lead"].read(handoff, "handoff")
    w.post(w.bus.envelope("handoff_accept", "quality-lead", "coordinator", conv, "B-2",
                          {k: accepted[k] for k in ("accepted", "owner", "first_action") if k in accepted},
                          in_reply_to=handoff["id"]))
    w.post(w.bus.envelope("outbound", "quality-lead", "customer", conv, "B-2",
                          {"body": accepted["customer_reply"], "template": "TPL-RC-01", "hazard_wording_changed": False}))
    w.post(w.bus.envelope("notification", "quality-lead", "coordinator", conv, "B-2",
                          {"event": "handoff_complete", "owner": "quality-lead"}))
    print(f"  owner of {conv} after the handoff: {w.bus.owner(conv)}; the follow-up is routed to the owner:")
    inbound = w.post(w.bus.envelope("inbound", "customer", w.bus.owner(conv), conv, "B-3", {"body": follow_up}))
    answer = w.agents["quality-lead"].read(inbound, "handoff")
    w.post(w.bus.envelope("outbound", "quality-lead", "customer", conv, "B-3",
                          {"body": answer["customer_reply"], "template": "TPL-RC-01", "hazard_wording_changed": False}))
    results["handoff"] = (w, "quality-lead", "quality-lead")
    handoff_world = w

    step(5, "Pattern C - broadcast: pause the lot for every unit worker")
    w = World(client, meter, lot_serials)
    inbound = w.post(w.bus.envelope("inbound", "customer", w.bus.owner(conv), conv, "C-1", {"body": report["body"], "lot": lot}))
    w.agents["coordinator"].read(inbound, "broadcast")
    workers = [f"worker-{i}" for i in (1, 2, 3)]
    notice = w.post(w.bus.envelope("broadcast", "coordinator", workers, conv, "C-2",
                                   {"topic": "lot_pause", "scope": "lot", "lot": lot, "serials": lot_serials,
                                    "reason": "safety event under investigation; no new bookings"}))
    for name in workers:
        w.post(w.bus.envelope("ack", name, "coordinator", conv, "C-2", w.agents[name].read(notice, "broadcast"),
                              in_reply_to=notice["id"]))
    results["broadcast"] = (w, "coordinator", "nobody - a broadcast owns nothing")
    print("  The customer got no answer from the broadcast: in practice you combine it with B (hand off the customer,")
    print("  broadcast the pause).")

    step(6, "Five bad messages, five rules - and the comparison")
    w = handoff_world
    attempts = [
        ("worker-2 answers the customer itself", w.bus.envelope("outbound", "worker-2", "customer", conv, "X-1",
         {"body": "We will visit Tuesday and credit you $500.", "template": "TPL-RC-02"})),
        ("the coordinator keeps answering after the handoff", w.bus.envelope("outbound", "coordinator", "customer", conv, "X-2",
         {"body": "Your visit is confirmed for Tuesday.", "template": "TPL-RC-02", "hazard_wording_changed": False})),
        ("a handoff with no state", w.bus.envelope("handoff", "quality-lead", "coordinator", conv, "X-3",
         {"reason": "back to routine"})),
        ("a handoff to a unit worker", w.bus.envelope("handoff", "quality-lead", "worker-1", conv, "X-4",
         {"reason": "x", "state": {"lot": lot, "unit": "x", "last_customer_message": "x", "open_actions": [], "constraints": []}})),
        ("a result nobody asked for", w.bus.envelope("result", "worker-3", "coordinator", conv, "X-5", {"verdict": "done"})),
    ]
    for label, env in attempts:
        try:
            w.bus.send(env)
            print(f"  {label}: accepted (unexpected)")
        except Rejected as exc:
            print(f"  {label}: rejected - {exc}")
    rows = []
    for pattern, (world, owner, replier) in results.items():
        t = meter.roles.get(pattern, d4.RoleTotals())
        rows.append([pattern, len(world.bus.log), t.calls, f"{t.input_tokens:,}", f"{t.output_tokens:,}", d4.money(t.cost),
                     world.bus.owner(conv), replier])
    print()
    print(d4.table(rows, ["pattern", "envelopes", "model calls", "input tok", "output tok", "cost", "owner after",
                          "customer answered by"]))
    print(wrap("Delegation keeps one voice towards the customer but puts the coordinator in the middle of every later "
               "exchange (two model calls per customer message); a handoff moves the conversation, and the state that "
               "goes with it, to the agent that must own what happens next; a broadcast informs many and owns nothing.", "  "))
    print(wrap("MCP is not this protocol: MCP connects ONE agent to tools and resources (a server offers tools, the agent "
               "calls them). Agents talking to agents need tasks, replies, ownership and acknowledgements - an envelope "
               "like this one, or an A2A-style task with an agent card. Use MCP for the desk, the calendar and the ERP; "
               "use envelopes between the coordinator, the workers and the quality lead.", "  "))


if __name__ == "__main__":
    main()
