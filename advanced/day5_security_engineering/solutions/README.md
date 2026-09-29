# Day 5 solutions - worked answers

Runnable solutions: `ex03_ex04_security_math.py` (calculations 3-4), `ex08_description_linter.py`,
`ex09_output_sanitizer.py`, `ex10_contractor_capability.py`, `ex11_classifier_feature.py`. Numbers below are what
those scripts print in mock mode.

---

## 1. What is the security boundary?

**a.** That sentence is a **prompt rule**, not a boundary. It is worth having - it lowers how often the lower layers
have to act - but it lives inside the very thing under attack (the model's context), so a good enough injection can
talk past it. The actual boundary for each abuse is a control the text cannot argue with:

| abuse | boundary that stops it |
|---|---|
| unauthorised refund | the capability **risk ceiling** (issue_refund is irreversible; the copilot's ceiling is write) and the **approval gate** with dual control |
| reading another customer's order | the **row filter**: the desk resolves the order's owner from the ops DB and compares it with the channel-derived capability |
| poisoned order note waiving a fee | **policy in the tool** (create_rma re-checks warranty/return eligibility) and the **phase/approval** gates on discounts |
| exfiltrating the account-manager's phone | the **output sanitiser** (internal detail withheld) and the **outbound-email allowlist** |

**b.** "The model is not a security boundary" means: do not rely on the model's cooperation to enforce a security
property, because the model is exactly what the attacker is manipulating. It follows that every property that *must*
hold goes in code the model cannot influence - the tool layer, row filters, allowlists, approval - and the model's
judgement is reserved for what needs judgement (intent, tone, which policy applies). A control **is** a boundary when
its decision does not depend on the model's output: `deny_reason()` reads the capability and the catalog, not the
reply. A control is **not** a boundary when defeating the model defeats it: the system-prompt rule, and the input
classifier itself (it is a *different* model, foolable in its own right - useful, not a wall).

**c.** "The classifier catches it" is a probabilistic claim about a model; "at least one control that does not depend
on the model" is a structural guarantee. The design target is that every attack meets a control of the second kind,
so that a classifier miss (which will happen) is not the same as an incident. Lab 01 checks exactly this: no top
risk is covered by the classifier alone.

## 2. Direct, indirect, and the lethal trifecta

**a.** Direct: the instruction is in the requester's own message (`email`, `email_thread`). Indirect: it rides in
content the agent *reads* while working (`tool_result`, `document`, `web_page`, `mcp_manifest`, `memory`). Indirect
is harder because (i) the malicious text arrives *after* the input screener has already passed the clean-looking
user message, and (ii) it is mixed with data the agent legitimately needs, so you cannot just refuse the whole
source. That is why untrusted content is provenance-tagged (lab 02) and why the durable controls are at the tool
layer, not the input.

**b.** The copilot has legs (1) and (2): it reads a customer's own records (private data) and untrusted email/tool
results (untrusted content). The cheapest leg to remove is **(3), the way data leaves**, and the design removes it
by *scoping*: private data is limited to the sender's own rows (identity from the channel + row filter), and the
only outbound path is a reply to that same verified sender, screened by the output sanitiser and the outbound-email
allowlist. Break any one leg and the trifecta is broken; scoping breaks the third.

**c.** ATK-007's objective is a **tool call** (apply_order_discount / issue_refund), so the tool layer has something
to refuse - the risk ceiling and approval gate stop it. ATK-016's objective is what the agent **writes** (an image
URL the customer's client will fetch); no tool call is involved, so the tool layer sees nothing. Its boundary is the
**output sanitiser**, which removes external images and data-bearing URLs before the reply is sent (lab 06).

## 3. False-positive cost of a classifier threshold

From `ex03_ex04_security_math.py`:

* **a.** A screen is `520 x $1/M + 130 x $5/M = $0.00117`, i.e. **$1.17 / 1,000 screens**, **$2.22 / month** at 1,900
  tickets. Token cost is negligible; it is not the thing to optimise.
* **b.** 1% hostile = **19 attacks/month**. Point A (block-only) catches `0.63 x 19 = 12`, sends nothing to review
  ($0). Point B (block-or-review) catches all 19; at **R = 8%** that is `1,900 x 0.08 x 4 min = 10.1 h = $405/month`,
  at **R = 20%** `$1,013/month`. The cost of a low threshold is **review minutes, not tokens**.
* **c.** B catches **7** more attacks/month than A. If the tool layer stops 96% independently, the expected loss B
  avoids over A is `7 x 0.04 x $L`: **$1,406/month** at $5,000/incident, **$5,174/month** at $18,400/incident.
* **d.** Break-even at an 8% review rate ($405/month): B pays for itself once an escaped incident costs more than
  **$1,441**. Since one corpus refund alone is $18,400, B is easily justified - **but most of B's value is the tool
  layer, not the classifier**. Tune the threshold to the review budget you can staff, and treat the tool layer as the
  backstop; chasing the last few points of classifier recall buys little once the tool layer is strong.

## 4. Defence-in-depth residual risk

From the same script:

* **a.** `P(evade all inputs) = 0.50 x 0.15 x 0.75 = 0.0563` (**5.6%**).
* **b.** `x (1 - 0.96) = 0.00225` (**0.23%**).
* **c.** `228 attacks/year x 0.00225 = 0.51/year` - about **one successful abuse every ~1.9 years**, *if the layers
  fail independently*.
* **d.** They do not. A fluent paraphrase beats the keyword filter and, because the classifier partly keys off
  surface wording too, lowers it as well: with the classifier at 0.60 on paraphrased attacks, `P(evade inputs) =
  0.50 x 0.40 x 0.75 = 0.150` (**15%**, 2.7x higher). Correlated failure is the real risk, so (i) measure the stack
  on adversarial variants (lab 07), not just the seed corpus, and (ii) rely on the layer that does **not** read the
  text - the tool layer and row filters - because a paraphrase cannot move them. Design target: every attack stopped
  by >= 2 independent layers, at least one model-independent.

## 5. The capability policy for a new contractor tenant

The reference (`ex10_contractor_capability.py`) - two roles, both row-filtered to C-1011/C-1013, both denying the
PII-bearing customer tools, both inheriting the global denied set:

```json
"df_dispatcher": {"domains": ["field_service","products","knowledge","fleet"], "max_risk": "write",
                  "row_filter": "customer_id in tenant.customers",
                  "denied_tools": ["get_customer","list_contacts","list_customer_sites","get_account_manager","get_site"]},
"df_engineer":   {"domains": ["field_service","products","knowledge"], "max_risk": "read",
                  "row_filter": "customer_id in tenant.customers",
                  "denied_tools": ["get_customer","list_contacts","list_customer_sites","get_account_manager","get_site"]}
```

Reasoning: **domains, not tool lists** - the role is what it does (field service), so a new field tool is covered
without editing the role. **`max_risk` caps the blast radius** - the dispatcher's `write` ceiling already excludes
irreversible tools (issue_refund, cancel_order); the engineer's `read` ceiling excludes all writes. **`denied_tools`
removes the PII-bearing customer tools** even though `customers` is not in their domains - defence in depth, in case
products/knowledge ever gains a customer-lookup tool. **`row_filter`** scopes every call to the two customers, the
owner resolved from identifiers. `always_denied_to_agents` (update_contact, cancel_order, release_quality_hold) is
inherited verbatim. The scoped toolsets come out at **40** tools (dispatcher) and **25** (engineer).

Rejected alternative: reuse Keystone's `contractor_lead`/`contractor_engineer`. It grants Keystone's customers and a
slightly different denied set; cloning it would either over-grant (wrong customers) or drift. A per-tenant role
keeps least privilege and keeps the audit legible.

## 6. Security review checklist and incident runbook

**Checklist (fail the PR if any is "no"):**

1. Does every new or changed tool have `meta` (domain, risk, pii, approval_required) and a row-filterable identifier?
2. Is the change's toolset still derived from a capability (no tool added to the copilot outside `scoped_toolset`)?
3. Are new irreversible/value-moving tools in `always_denied_to_agents` or behind an approval role with dual control?
4. Do new external tools/MCP servers pass the supply-chain scan (signed, pinned, scoped, description clean)?
5. Does the regression suite (whole corpus through the stacked screen) still pass, with stacked detection not lower
   than the baseline, and are any new attack patterns added to the corpus?
6. Do output checks cover any new outbound field (links, images, new PII)?
7. Is every new decision audited, and can lab 07's forensic timeline reconstruct a run that uses the new path?

**Incident runbook (on-call):**

1. **Contain**: flip the affected capability/phase to read-only (or disable the tool via the allowlist); a config
   change, not a deploy.
2. **Assess**: run the forensic timeline for the run id (`forensic_timeline`) - inbound source, screen verdict, every
   tool call and the layer that refused it. Confirm whether any state changed (refund row, contact change, outbound
   email).
3. **Attribute**: identify the channel and the untrusted content that carried the instruction; pull the tracer export
   for timing/cost.
4. **Eradicate/recover**: reverse any effect through the system of record (never by asking the agent); rotate any
   exposed secret.
5. **Learn**: add the exact payload (and mutations) to the corpus as a regression case; if a layer missed it, decide
   whether the fix is a new feature, a tool-layer rule, or an allowlist change - prefer the model-independent one.
6. **Report**: who told the agent what, what it tried, which layer stopped it, and what shipped.

## 7. A new capability changes the threat model

Adding `send_email` to the copilot adds the **outbound-channel asset** and turns the third leg of the trifecta back
on. Register changes: send_email jumps to impact 5 (irreversible + PII + exfil channel) and, if the *copilot* holds
it, reach 4 - the single highest-scoring row in the whole catalog. Controls to add **before** enabling it: (i) an
**outbound-email allowlist** - recipients limited to the verified conversation domain or Kestrel, with
lookalike-domain detection (lab 06); (ii) **output DLP** on the body (no other customers, no internal contacts, no
secrets, no data-bearing links/images); (iii) **approval** for any email to a new recipient during rollout; (iv) a
**per-conversation rate limit**. Enable it first for **neither** as a free capability: start with a drafting mode
that composes the email but routes it through `escalate_to_human` for a person to send, then graduate to
auto-send only to the verified sender's own domain once the allowlist and DLP have a measured false-positive rate.
The internal support_manager's agent is *more* dangerous here, not less: it has `rows=all`, so an injected
instruction could email any customer's data anywhere - it needs the same allowlist plus the manager's own approval.

## 8. Description linter

`ex08_description_linter.py` implements four pattern groups (instructs-the-model, addresses-the-assistant,
wants-conversation-or-PII, hidden-importance-tag). Over the six manifests it flags
`kestrel-weather/get_weather_at_site`, `kestrel-ops-tools/search_orders_fast` and
`kestrel-docs-v2/search_knowledge_base`, leaves the 9 clean descriptions alone, and **matches the labels' set of
description-flagged servers exactly**. It belongs in the supply-chain scan because the thing it protects is the model
that reads these descriptions; a model cannot be both the reader under attack and the screen. (a) Two reasons a model
is wrong for this: it is non-deterministic (a review gate must be reproducible), and it is itself susceptible to the
very instructions it is meant to flag. (b) Flagging more than the label headline is fine - a linter's job is to
surface instruction-like text; missing one is the only real failure. (c) The reworded "for best results, run this
before other tools" is caught by the ordering-directive pattern; if a future rewording slips past the linter, the
**signature/pin** and **scope cap** still hold - the linter is one layer, not the boundary.

## 9. Output sanitiser

`ex09_output_sanitizer.py` implements the three passes (images out; out-of-allowlist or data-bearing links out;
sensitive strings and secrets withheld) and **all three cases pass**: the data-bearing image and the secret are
removed, the internal `kestrel-pumps.example` link survives. It is an *output* control because it runs on the reply
the model already wrote, so it does not depend on the model having resisted the attack - it is the exit-side twin of
lab 02's input tagging, and the boundary for the exfiltration cases (ATK-016, ATK-019) the tool layer cannot see.

## 10. The contractor capability, as code

`ex10_contractor_capability.py` builds the tenant from exercise 5 and passes all eight checks: dispatcher schedules
field work but is refused list_contacts, issue_refund and cancel_order; engineer reads a service ticket but is
refused scheduling and get_customer; both are row-filtered to {C-1011, C-1013}. Scoped toolsets: **40** tools for the
dispatcher, **25** for the engineer.

## 11. Add a classifier feature and measure the delta

`ex11_classifier_feature.py` targets `data_exfiltration`. The input stack leaves 11 attacks at *review*; three are
exfiltration-shaped (ATK-016/017/018). The candidate feature (matching "list which other companies", "contact
names", "forward ... audit log", "benchmarking suppliers") fires on **ATK-017 and ATK-018** and on **zero** benign
hard negatives - **projected delta +2 detection, +0 false positives**. Ship it at **review**, not block: zero FP on
12 adversarial negatives is not zero FP on real mail where "contact names" appears legitimately. And the durable fix
for ATK-017/018 is not the regex - it is the **row filter** (other customers' data is out of scope) and the
**outbound-email allowlist** (the law-firm address is refused), which hold whatever the classifier decides. Add the
feature to `classify_text` in the scenario module, measure it live through lab 02, and keep both attacks in the
regression suite (lab 07).

## 12. Why the supply-chain scan is in code

(a) A model is the wrong screen because it is non-deterministic (a gate must reproduce) and because it is itself the
target of the poisoned descriptions - you would be asking the attacked component to judge the attack. (b) No:
"flag more than the label headline" is a superset, which is the safe direction; the review reads every flag. (c) The
reworded description is caught by the ordering-directive pattern, but the real lesson is that linters are a
best-effort layer over *text*; the controls that do not read the text - **signature verification, version pinning,
scope caps, and the allowlist** - are what actually stop a malicious server, which is why lab 05 ranks them first and
treats the linter as one signal among several.
