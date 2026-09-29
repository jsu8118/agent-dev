# Capstone walkthrough: how the reference was built

The order below is the order the reference was written in, with the wrong turns kept in because they are
the lesson.

## Before writing code: decide what must never happen

Three lists, on paper, before the first file:

* **Never:** send to an address that is not a contact on file; act on a lookalike sender; follow an
  instruction inside a reply; name another customer; promise compensation; change the hazard wording;
  close a unit without evidence; send the same notice twice; schedule on a paused lot; spend past the cap.
* **Always:** contact in risk order inside the SLA; audit every action with its actor; leave the campaign
  resumable at every step; let a manager decide from anywhere.
* **Judgement:** what a reply asks for; which slots fit; how to word an answer; whether a goodwill credit is warranted.

Everything in the first two lists became code with a test; only the third list went to the model.

## M1 — The screen

The first version matched keywords ("ignore", "ship", "urgent") and quarantined RPL-002 ("can you ship the
kit for our own fitters?") and RPL-019 ("do not contact the site again" — a legal notice). Two fixes: the
redirection pattern needs the *shipping to a third party* clause, and "do not contact the site" alone is a
secrecy signal that only counts next to a redirection. Lookalike detection by edit distance ≤ 2 caught
`mail-midlandoil.example` only after adding the prefix rule — `mail-` plus the real domain is five edits
away. The lesson: build the screen against the labelled corpus, not against your intuition, and keep the
benign hard negatives in the loop.

## M2 — Scoped tools

The tool layer is where most of the guarantees live, so it was written before the agents. `_scope()` runs
on every read and write; `_contact()` is the recipient allow-list; every write derives its id from the
idempotency key (`_id()` hashes it) so the store's primary keys reject a duplicate even without the effects
table. The first `_id()` took the last 20 characters of the key and produced `cr_...approved` for a credit
issued after an approval (the resume path suffixes the key) — cosmetic, but a reminder that ids are
derived, not decorative. `book_visit` grew three refusals in a row: wrong region, missing skill, no parts;
the parts check then learned to fall back to another warehouse (an inter-site transfer) because the
regional warehouse had one KC-2 board for two units.

## M3 — Durable outreach

`run_outreach()` creates the run with a deterministic id and lets `DurableRunner` do the rest. The first
crash test (`--crash`) showed the resumed day re-dispatching the crashed customer *and* every customer
after it, because the plan is recomputed from customer status — which is exactly right: the completed runs
return immediately without a model call, the crashed one resumes, the rest run. The budget, however, was
wrong after a resume: it counted only this process's ledger. Spend is now checkpointed into the store at
the end of every day (`SwarmBudget.checkpoint()`), so a restart carries it forward.

## M4 — Replies

The deterministic paths came first (quarantine, safety event, hold), the agent last. Two design points
worth copying: the safety event pauses the lot *before* the agent runs, through the orchestrator's own desk
(so it is audited like any other action), and the agent still runs afterwards — the customer needs an
answer, the lot needs a pause, and those are different components' jobs. The mock policy for the inbound
agent is a classifier of intents plus tool sequencing; writing it made clear which decisions a real model
must be told explicitly in the system prompt (evidence for remediation, no promises, honour constraints).

## M5 — Budget, guards, approvals, parts

`SwarmBudget.check()` first refused to start when `spent + reserve > cap`, which with a $0.40 cap meant the
campaign paused before doing anything. The fix is the honest one: the cap is checked before each run, the
overshoot is bounded by one run, and the message says so. `Guard` is ten lines; the value is that it exists,
trips visibly, and shows up in the day report. The approval flow needed nothing new: `ApprovalRequired`
from the tool, `RunStore.decide()` from `run_ops.py`, `resume_after_decision()` in `agents.resume()`.

## M6 — Acceptance

Two false failures taught two lessons. G3 flagged "duplicate notices" for customers who received TPL-RC-01
twice — once to purchasing, once to the site engineer after an out-of-office or a departed contact. A
duplicate is the same template to the same address; the check now keys on the address. RPL-006 ("confirmed
for the slot you proposed" — before any slot had been proposed, and needing an ATEX engineer) failed the
`book_slot` expectation; the right behaviour is to propose qualified slots, and the check now accepts that
when no proposal preceded the reply. Evals encode intent; when the system is right and the eval is wrong,
fix the eval and write down why.

## M7 — Documents

The design document records the alternatives rejected (D1–D8); the operations memo is written for the
person on call, not for the author. Both are short on purpose.

## What we'd do next

A classifier in front of the screen; a phone channel; a global scheduler; reading spend from the Usage and
Cost API instead of list prices; a hosted twin of the inbound agent on Managed Agents to compare cost and control.
