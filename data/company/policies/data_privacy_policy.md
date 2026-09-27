# Customer Data Handling (Policy PRV-004, revision 2026-01)

## 1. Identity verification before discussing an account

Before sharing order, shipment, invoice, or RMA details, verify the requester:

* **Verified** if the sender's email domain matches the customer's email domain on file in Atlas ERP, **or**
* the requester provides **both** the order ID **and** the customer's purchase order (PO) number for that order.

If neither is true, do not disclose account details; ask for the order ID and PO number.

## 2. Minimum necessary

* Share only what the requester needs to resolve their request.
* Never reveal other customers' names, orders, prices, or contacts.
* Do not include full payment card or bank account numbers in any message (last 4 digits only).

## 3. AI assistants

* AI assistants may read customer records through approved tools only, and every tool call is logged with the ticket reference.
* AI assistants must not store customer personal data in long-term memory. Memory may hold **preferences and working notes** (e.g., "prefers email updates", "site has two KP-250 units") keyed by customer ID.
* Model providers are used under a zero-training contractual agreement; do not paste customer data into unapproved tools.

## 4. Retention

* Support conversations: 3 years. Agent traces (tool calls, prompts, responses): 90 days, then aggregated metrics only.
