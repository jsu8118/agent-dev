# Authoring guide (for course maintainers and contributors)

This guide keeps the seven days consistent. Learners never need it, but if you extend the course, follow it.

## 1. Voice and pedagogy

The audience is **working engineers** (intermediate to advanced) who want depth *and* pragmatism.

For every concept, cover these, in prose (not a checklist):

1. **What it is**, in one or two plain sentences.
2. **Why it exists**: the problem it solves and the context it came from.
3. **When to use it** (and when not), with concrete Kestrel use cases.
4. **How it compares** with neighbouring concepts, usually as a small comparison table
   (e.g. tool use vs. structured output vs. code execution; RAG vs. long context vs. agentic search).
5. **How it works under the hood**, to the depth needed to debug it.
6. **Pitfalls** seen in production, and a **pragmatic recipe**.

Rules:
* Prefer measured claims ("in our lab run, caching cut input cost by 74%") over adjectives.
* Show trade-offs honestly (cost, latency, reliability, complexity). "It depends" must be followed by *on what*.
* No filler, no hype, no pedantry. Explain jargon on first use.
* Code comments explain *why*, not *what*.

## 2. Directory layout (per day)

```
dayN_topic/
  README.md            lesson: objectives, agenda, concepts, case study, lab walkthrough, takeaways, further reading
  labs/                runnable, numbered scripts: 01_*.py, 02_*.py ...  (helpers start with "_")
  exercises/README.md  questions: conceptual, design/scenario, and hands-on coding tasks
  exercises/*.py       optional starter files for coding exercises (must run, may print TODO notes)
  solutions/README.md  detailed answers to every question: reasoning, not just results
  solutions/*.py       runnable solutions for the coding exercises
```

Mock policies for a day's labs live in `labkit/mock/scenarios/dayN_<topic>.py`.

## 3. Lab scripts

* Start with a module docstring: **objective**, **concepts**, **how to run**, **what to observe**.
* Import from `labkit` (`get_client`, `MODEL`, `FAST_MODEL`, display helpers) and, where relevant, `kestrel`.
  Never read `ANTHROPIC_API_KEY` yourself; never branch on mock vs live except to print a note.
* Structure: small functions per step, `main()`, `if __name__ == "__main__": main()`.
* Print with `labkit.display` (`header`, `step`, `show_message`) so output is readable in CI logs.
* Scripts must work in **both** modes: never assert on exact model wording; assert on structure.
* Keep API usage current (see the claude-api reference): no `temperature`/`top_p`/`top_k` on Opus 5,
  no assistant prefill, `thinking={"type": "adaptive"}` (or omit it), `output_config={"effort": ...}`,
  structured outputs via `client.messages.parse(output_format=Model)` or `output_config.format`.
  Default model: `labkit.MODEL` (claude-opus-5). Only use `FAST_MODEL`/`MID_MODEL` where the lesson is about
  routing, judging, or cost - and check `labkit.supports_effort()` before sending `effort` to them.
* `max_tokens`: thinking counts toward it on Opus 5 - use >= 2000 even for short answers, ~8000-16000 for agent turns;
  stream when you need more than ~16000.
* Test directives (see `tests/test_labs.py`): `# test: args=...`, `# test: timeout=...`, `# test: expect=...`,
  `# test: skip` (only for truly interactive scripts; explain why).

## 4. Mock scenarios

A scenario is a rule-based stand-in for Claude for ONE lab or agent (see `labkit/mock/registry.py`):

```python
from labkit.mock import scenario, say, use_tools, tool, json_reply

@scenario("day3.rag_answer", match=lambda r: "<kestrel_manual_excerpts>" in r.system_text)
def rag_answer(req):
    ...
```

* Match on something **specific** to your lab (a tool name, a unique system-prompt marker), never on generic text.
* Be deterministic. Derive every decision from the request (`req.first_user_text`, `req.tool_calls`, results).
* Write answers only from information the "model" actually has in the request (tool results, documents in the prompt).
* Structured-output requests must return JSON that validates against the schema (`json_reply`).
* If nothing matches, the generic fallback still returns a schema-valid or polite answer - but labs should have a
  scenario so mock-mode output is meaningful.

## 5. Exercises and solutions

Each day has 8-12 exercises mixing:
* **Concept checks** (short answer; the solution explains why, including why tempting wrong answers are wrong),
* **Design scenarios** (e.g. "Kestrel wants X under constraint Y — which architecture?"; the solution compares options),
* **Hands-on tasks** (modify or extend a lab; the solution is runnable code + explanation of the design choices).

## 6. Definition of done

* `pytest` passes (all scripts run in mock mode).
* The README's lab walkthrough matches what the scripts actually print.
* Every factual claim about the Claude API matches the current documentation.
