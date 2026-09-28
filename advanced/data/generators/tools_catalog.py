"""A 120-tool catalog for Kestrel's operations platform - the raw material for Day 2 (tools at scale).

Twelve domains, ten tools each.  The catalog is deliberately realistic about the things that make large
tool sets hard: near-duplicate tools (get_shipment vs track_shipment, issue_refund vs issue_credit_note),
uneven description quality, a handful of always-loaded "core" tools, and metadata that the labs use for
scoping (domain, risk, pii, side_effect, approval).

Output: advanced/data/tools/catalog.json  {"generated_at", "count", "domains": [...], "tools": [...]}
"""

from __future__ import annotations

from pathlib import Path

from .common import AS_OF, write_json

# Property shorthand: "type|description" - a small DSL so 120 schemas stay readable in one file.
P = {
    "order_id": "string|Sales order ID, e.g. SO-10248.",
    "customer_id": "string|Customer ID, e.g. C-1005.",
    "sku": "string|Product SKU, e.g. KP-250-S or MS-250.",
    "serial_number": "string|Unit serial number, e.g. KP250-2608-0002.",
    "shipment_id": "string|Shipment ID, e.g. SH-50210.",
    "invoice_id": "string|Invoice ID, e.g. AR-90210.",
    "rma_id": "string|Return authorisation ID, e.g. RMA-3007.",
    "ticket_id": "string|Service ticket ID, e.g. SVC-4021.",
    "warehouse": "string|Warehouse code: WH-EAST, WH-WEST or WH-EU.",
    "qty": "integer|Quantity of units.",
    "amount_usd": "number|Amount in US dollars.",
    "reason": "string|Free-text reason recorded in the audit log.",
    "date": "string|ISO date, e.g. 2026-09-22.",
    "note": "string|Free-text note.",
    "query": "string|Free-text search query.",
    "limit": "integer|Maximum number of results (default 10).",
    "lot": "string|Manufacturing lot, e.g. seal lot PS-2608-B or board lot VD-2607-C.",
    "site_id": "string|Customer site ID, e.g. SITE-1005-A.",
    "engineer_id": "string|Field-service engineer ID, e.g. FSE-03.",
    "email": "string|Email address.",
    "priority": "string|P1 (safety/outage), P2 (production at risk), P3 (routine), P4 (information).",
    "queue": "string|Human queue: billing, logistics, security, quality, field_service, account_management.",
    "alert_id": "string|Fleet alert ID, e.g. ALR-88012.",
    "fault_code": "string|Controller fault code, e.g. F17 or E42.",
    "contact_id": "string|Contact ID, e.g. CT-1005-2.",
    "document_id": "string|Knowledge-base document ID, e.g. KB-0142.",
    "hold_id": "string|Quality hold ID, e.g. QH-2026-014.",
    "incident_id": "string|Quality incident ID, e.g. INC-2026-031.",
    "task_id": "string|Task ID, e.g. TASK-7781.",
}


def prop(name: str, override: str | None = None) -> tuple[str, dict]:
    kind, _, desc = (override or P[name]).partition("|")
    schema: dict = {"type": kind, "description": desc}
    if kind == "array":
        schema["items"] = {"type": "string"}
    return name, schema


def tool(name: str, description: str, props: list, required: list[str] | None = None, *, risk: str = "read",
         pii: bool = False, approval: bool = False, core: bool = False, examples: list[dict] | None = None) -> dict:
    properties = dict(prop(p) if isinstance(p, str) else prop(*p) for p in props)
    required = list(properties) if required is None else required
    out = {"name": name, "description": description,
           "input_schema": {"type": "object", "properties": properties, "required": required,
                            "additionalProperties": False},
           "meta": {"domain": "", "risk": risk, "side_effect": risk != "read", "pii": pii,
                    "approval_required": approval, "core": core}}
    if examples:
        out["input_examples"] = examples
    return out


DOMAINS: dict[str, tuple[str, list[dict]]] = {
    "orders": ("Sales orders and their lines.", [
        tool("get_order", "Return one sales order with its lines, status, promised date and customer PO.", ["order_id"], core=True,
             examples=[{"order_id": "SO-10248"}]),
        tool("list_orders", "List a customer's orders, newest first. Filter by status (open, shipped, delivered, cancelled).",
             ["customer_id", ("status", "string|Order status filter."), "limit"], ["customer_id"], pii=True),
        tool("search_orders", "Search orders by customer PO number, SKU or free text in the notes.", ["query", "limit"], ["query"]),
        tool("get_order_lines", "Lines (SKU, qty, unit price, configured/special-order flags) of one order.", ["order_id"]),
        tool("get_order_status_history", "Every status change of an order with timestamps and actors.", ["order_id"]),
        tool("update_order_notes", "Append a note to the order (visible to logistics and billing).", ["order_id", "note"], risk="write"),
        tool("hold_order", "Put an unshipped order on hold (credit, quality or customer request).", ["order_id", "reason"], risk="write"),
        tool("release_order_hold", "Release a held order back to fulfilment.", ["order_id", "reason"], risk="write"),
        tool("cancel_order", "Cancel an unshipped order or line. Irreversible; shipped lines must go through an RMA.",
             ["order_id", ("line_no", "integer|Line number to cancel; omit to cancel the whole order."), "reason"], ["order_id", "reason"],
             risk="irreversible", approval=True),
        tool("apply_order_discount", "Apply a percentage discount to an open order (max 15% without approval).",
             ["order_id", ("percent", "number|Discount percentage, 0-100."), "reason"], risk="write", approval=True),
    ]),
    "logistics": ("Shipments, carriers, customs.", [
        tool("get_shipment", "Return a shipment: carrier, tracking number, ship date, ETA, delivered date, exception reason.", ["shipment_id"]),
        tool("track_shipment", "Live carrier tracking events for a tracking number (scan history, current location, exceptions).",
             [("tracking_number", "string|Carrier tracking number, e.g. NLF1972237794.")]),
        tool("list_shipments_for_order", "All shipments (including partials) created for one order.", ["order_id"], core=True),
        tool("get_carrier_rates", "Quote carriers for a lane: origin warehouse, destination country, weight, service level.",
             ["warehouse", ("country", "string|ISO country code."), ("weight_kg", "number|Total weight in kg."),
              ("service", "string|standard, express or freight.")], ["warehouse", "country", "weight_kg"]),
        tool("create_shipment", "Create a shipment for unshipped lines of an order from a warehouse with a carrier.",
             ["order_id", "warehouse", ("carrier", "string|Carrier name."), ("service", "string|standard, express or freight.")],
             ["order_id", "warehouse", "carrier"], risk="write"),
        tool("reroute_shipment", "Change the delivery address of an in-transit shipment (carrier fee applies).",
             ["shipment_id", ("address", "string|New delivery address."), "reason"], risk="write", approval=True),
        tool("schedule_pickup", "Book a carrier pickup at a warehouse or a customer site for a date.",
             [("location", "string|Warehouse code or site ID."), "date", ("pieces", "integer|Number of pieces.")], risk="write"),
        tool("file_carrier_claim", "File a loss/damage claim with the carrier for a shipment.",
             ["shipment_id", "amount_usd", ("description", "string|What was lost or damaged.")], risk="write"),
        tool("get_customs_status", "Customs clearance status and any duties owed for an international shipment.", ["shipment_id"]),
        tool("update_delivery_window", "Set or change the requested delivery window communicated to the carrier.",
             ["shipment_id", ("window_start", "string|ISO datetime."), ("window_end", "string|ISO datetime.")], risk="write"),
    ]),
    "billing": ("Invoices, payments, credits and refunds.", [
        tool("get_invoice", "Return an invoice: amount, paid amount, due date, status, related order.", ["invoice_id"], core=True),
        tool("list_invoices", "List a customer's invoices; filter by status (open, paid, overdue, disputed).",
             ["customer_id", ("status", "string|Invoice status filter."), "limit"], ["customer_id"], pii=True),
        tool("get_account_balance", "Outstanding balance, overdue amount and days-past-due for a customer.", ["customer_id"], pii=True),
        tool("get_credit_limit", "Credit limit, used credit and available credit for a customer.", ["customer_id"], pii=True),
        tool("get_payment_terms", "Contractual payment terms (net days, early-payment discount) for a customer.", ["customer_id"]),
        tool("record_payment", "Record a payment received against an invoice.", ["invoice_id", "amount_usd", ("reference", "string|Bank or cheque reference.")],
             risk="write"),
        tool("issue_credit_note", "Issue a credit note against an invoice (reduces what the customer owes; no money moves).",
             ["invoice_id", "amount_usd", "reason"], risk="write", approval=True),
        tool("issue_refund", "Refund money to the customer's original payment method. Over $2,500 needs a support manager; over $10,000 the finance director.",
             ["order_id", "amount_usd", "reason", ("rma_id", "string|RMA the refund settles, if any.")], ["order_id", "amount_usd", "reason"],
             risk="irreversible", approval=True),
        tool("apply_late_fee_waiver", "Waive late fees on an overdue invoice.", ["invoice_id", "reason"], risk="write"),
        tool("generate_statement", "Generate a PDF account statement for a period and return a download link.",
             ["customer_id", ("from_date", "string|ISO date."), ("to_date", "string|ISO date.")], risk="write", pii=True),
    ]),
    "returns": ("Return authorisations (RMAs).", [
        tool("get_rma", "Return an RMA: order, SKU, qty, reason, status, refund due.", ["rma_id"]),
        tool("list_open_rmas", "Open RMAs for a customer or for all customers when customer_id is omitted.", ["customer_id", "limit"], []),
        tool("check_return_eligibility", "Apply the returns policy to a delivered line: window, restocking fee, exclusions (configured, special order).",
             ["order_id", "sku", "qty", ("reason_code", "string|no_longer_needed, wrong_item, damaged_in_transit, defective, other.")], core=True),
        tool("get_restocking_fee", "Restocking fee that would apply to a return, by product line and reason.", ["sku", "qty", ("reason_code", "string|Return reason code.")]),
        tool("create_rma", "Open a return authorisation for delivered units.", ["order_id", "sku", "qty", ("reason_code", "string|Return reason code."), "note"],
             ["order_id", "sku", "qty", "reason_code"], risk="write"),
        tool("update_rma_status", "Move an RMA to a new status (approved, received, inspected, closed, rejected).",
             ["rma_id", ("status", "string|New status."), "note"], ["rma_id", "status"], risk="write"),
        tool("get_rma_inspection", "Inspection findings for units received under an RMA.", ["rma_id"]),
        tool("schedule_return_pickup", "Book a carrier pickup for RMA units at the customer's site.", ["rma_id", "date"], risk="write"),
        tool("close_rma", "Close an RMA after refund or replacement is settled.", ["rma_id", "note"], ["rma_id"], risk="write"),
        tool("reopen_rma", "Reopen a closed RMA (disputes, late-found damage).", ["rma_id", "reason"], risk="write", approval=True),
    ]),
    "field_service": ("Warranty, service tickets and field engineers.", [
        tool("check_warranty", "Warranty status of a unit by serial or of a SKU shipped on a date; includes advance-replacement eligibility.",
             ["serial_number", "sku", ("ship_date", "string|ISO date the unit shipped.")], [], core=True),
        tool("register_warranty", "Register a unit's installation date to start its warranty clock.", ["serial_number", "site_id", "date"], risk="write"),
        tool("create_service_ticket", "Open a field-service ticket for a site with a priority and a fault description.",
             ["customer_id", "site_id", "priority", ("description", "string|Fault description, fault codes, symptoms."), "serial_number"],
             ["customer_id", "site_id", "priority", "description"], risk="write"),
        tool("get_service_ticket", "Return a service ticket with its history and assigned engineer.", ["ticket_id"]),
        tool("list_service_tickets", "Service tickets for a customer or site; filter by status.", ["customer_id", "site_id", ("status", "string|open, scheduled, closed.")], []),
        tool("get_engineer_availability", "Free slots of field engineers in a region between two dates.",
             [("region", "string|US-EAST, US-WEST, EU, APAC."), ("from_date", "string|ISO date."), ("to_date", "string|ISO date."),
              ("skill", "string|Required skill, e.g. seal_replacement, controller_firmware, atex.")], ["region", "from_date", "to_date"]),
        tool("schedule_field_visit", "Book an engineer slot for a service ticket at a site.", ["ticket_id", "engineer_id", ("slot_start", "string|ISO datetime.")],
             risk="write"),
        tool("assign_engineer", "Assign or reassign an engineer to a ticket without booking a slot.", ["ticket_id", "engineer_id"], risk="write"),
        tool("get_service_history", "Past service visits and parts used for a serial number.", ["serial_number"]),
        tool("close_service_ticket", "Close a ticket with a resolution code and parts used.",
             ["ticket_id", ("resolution", "string|Resolution code and summary."), ("parts_used", "array|SKUs consumed.")], ["ticket_id", "resolution"],
             risk="write"),
    ]),
    "inventory": ("Stock, reservations and reorder points.", [
        tool("get_stock", "On-hand, reserved and available stock of a SKU per warehouse.", ["sku", "warehouse"], ["sku"]),
        tool("list_low_stock", "SKUs at or below their reorder point in a warehouse.", ["warehouse", "limit"], ["warehouse"]),
        tool("get_lead_time", "Replenishment lead time in days for a SKU (from the supplier or the plant).", ["sku"]),
        tool("get_reorder_point", "Reorder point and reorder quantity of a SKU in a warehouse.", ["sku", "warehouse"]),
        tool("set_reorder_point", "Change the reorder point of a SKU in a warehouse.", ["sku", "warehouse", ("reorder_point", "integer|New reorder point.")],
             risk="write", approval=True),
        tool("reserve_stock", "Reserve units for an order or a service ticket so they cannot be sold twice.",
             ["sku", "warehouse", "qty", ("reference", "string|Order, RMA or ticket ID the reservation is for.")], risk="write"),
        tool("release_reservation", "Release a stock reservation.", [("reservation_id", "string|Reservation ID, e.g. RSV-20081.")], risk="write"),
        tool("transfer_stock", "Move units between warehouses (creates an internal transfer order).", ["sku", ("from_warehouse", "string|Source warehouse code."),
             ("to_warehouse", "string|Destination warehouse code."), "qty"], risk="write"),
        tool("get_warehouse", "Warehouse address, cut-off times and carriers served.", ["warehouse"]),
        tool("list_warehouses", "All warehouses with their regions.", [], []),
    ]),
    "products": ("Catalog, compatibility, configuration and documentation.", [
        tool("get_product", "Product master data: name, line, list price, warranty months, lead time, returnable/configurable flags.", ["sku"]),
        tool("search_products", "Search the catalog by name, family or application (e.g. 'ATEX pump 22 kW').", ["query", "limit"], ["query"]),
        tool("get_price", "Customer-specific price for a SKU (list price minus tier discount and contract pricing).", ["sku", "customer_id"]),
        tool("get_bom", "Bill of materials (spare parts and their quantities) of a pump or controller SKU.", ["sku"]),
        tool("get_compatible_parts", "Spare parts and accessories compatible with a SKU or serial number.", ["sku", "serial_number"], []),
        tool("get_datasheet", "Datasheet link and key ratings (flow, head, power, materials) for a SKU.", ["sku"]),
        tool("get_manual_section", "A section of the installation/operation manual for a product family.",
             [("family", "string|Product family, e.g. KP-250 or KC-2."), ("section", "string|Section title or number, e.g. '6.2 Seal replacement'.")]),
        tool("list_product_revisions", "Hardware/firmware revisions of a SKU with dates and change notes.", ["sku"]),
        tool("get_configuration_options", "Configurable options (voltage, protocol, enclosure) of a controller SKU.", ["sku"]),
        tool("validate_configuration", "Validate a controller/pump configuration for compatibility before quoting.",
             ["sku", ("options", "string|JSON object of option name -> value.")]),
    ]),
    "quality": ("Build records, lots, holds and incidents.", [
        tool("get_build_record", "Build record of a unit: SKU, order, build date, plant, seal lot, board lot, test result.", ["serial_number"], core=True),
        tool("list_units_by_lot", "Every serial number built with a seal lot or board lot, with the order and customer each shipped to.", ["lot"], pii=True),
        tool("get_lot_summary", "Units built, plants, date range and open incidents for a lot.", ["lot"]),
        tool("get_test_results", "Factory acceptance test results (pressure, vibration, leak) of a unit.", ["serial_number"]),
        tool("list_quality_holds", "Open quality holds and the lots/SKUs they cover.", [("status", "string|open or released.")], []),
        tool("open_quality_hold", "Block shipment of units of a lot or SKU pending investigation.", ["lot", "sku", "reason"], ["reason"],
             risk="write", approval=True),
        tool("release_quality_hold", "Release a quality hold after investigation.", ["hold_id", "note"], ["hold_id"], risk="write", approval=True),
        tool("get_incident_report", "A quality incident report (field failure, root cause, corrective action).", ["incident_id"]),
        tool("create_incident_report", "Open a quality incident from a field failure.",
             ["serial_number", ("summary", "string|What failed and how."), ("severity", "string|low, medium, high, safety.")], risk="write"),
        tool("link_incident_to_lot", "Associate an incident with a suspect lot for containment.", ["incident_id", "lot"], risk="write"),
    ]),
    "customers": ("Accounts, contacts, sites and contracts.", [
        tool("get_customer", "Account profile: name, industry, tier, country, account manager, credit limit.", ["customer_id"], pii=True, core=True),
        tool("search_customers", "Find customers by name, email domain or industry.", ["query", "limit"], ["query"], pii=True),
        tool("list_contacts", "Contacts at a customer with roles, emails and phone numbers.", ["customer_id"], pii=True),
        tool("update_contact", "Update a contact's email, phone or role.", ["contact_id", ("field", "string|email, phone or role."), ("value", "string|New value.")],
             risk="write", pii=True, approval=True),
        tool("add_contact_note", "Add a note to a contact's CRM record.", ["contact_id", "note"], risk="write", pii=True),
        tool("get_account_manager", "Account manager and their contact details for a customer.", ["customer_id"], pii=True),
        tool("get_customer_tier", "Service tier (strategic, key, standard) and what it entitles the customer to.", ["customer_id"]),
        tool("get_sla_terms", "Contractual SLA: response times by priority, on-site hours, spares commitment.", ["customer_id"]),
        tool("list_customer_sites", "Sites (plants, pump stations) of a customer with addresses and time zones.", ["customer_id"], pii=True),
        tool("get_site", "One site: address, contacts, installed units, access instructions.", ["site_id"], pii=True),
    ]),
    "fleet": ("Telemetry, alerts and fault codes from connected controllers.", [
        tool("get_pump_telemetry", "Latest readings (flow, head, power, vibration, seal temperature) for a unit.", ["serial_number"]),
        tool("get_vibration_trend", "Vibration RMS trend for a unit over a number of days.", ["serial_number", ("days", "integer|Look-back window in days.")]),
        tool("get_runtime_hours", "Total and since-last-service runtime hours of a unit.", ["serial_number"]),
        tool("list_fleet_alerts", "Open alerts for a customer or site, most severe first.", ["customer_id", "site_id", ("severity", "string|info, warning, critical.")], []),
        tool("get_alert", "One fleet alert with the readings that triggered it.", ["alert_id"]),
        tool("acknowledge_alert", "Acknowledge an alert with a note so it stops paging.", ["alert_id", "note"], risk="write"),
        tool("get_fault_codes", "Fault codes logged by a controller in the last N days.", ["serial_number", ("days", "integer|Look-back window in days.")]),
        tool("decode_fault_code", "Meaning, likely causes and recommended action for a controller fault code.", ["fault_code", ("family", "string|Controller family, KC-1 or KC-2.")], ["fault_code"]),
        tool("set_alert_threshold", "Change an alert threshold (e.g. vibration mm/s) for a unit.",
             ["serial_number", ("metric", "string|vibration_rms, seal_temp_c, power_kw, flow_m3h."), ("threshold", "number|New threshold.")],
             risk="write", approval=True),
        tool("get_site_health", "Roll-up of unit health, open alerts and runtime for a site.", ["site_id"]),
    ]),
    "knowledge": ("Policies, manuals, runbooks, bulletins and past incidents.", [
        tool("search_knowledge_base", "Full-text search across policies, manuals, runbooks and bulletins; returns passages with citations.",
             ["query", "limit"], ["query"], core=True),
        tool("get_document", "Full text of a knowledge-base document by ID.", ["document_id"]),
        tool("list_policies", "Titles and IDs of customer-facing policies (returns, warranty, shipping, privacy).", [], []),
        tool("get_policy", "A policy document by name (returns, warranty, shipping, refund_approval, data_handling).", [("name", "string|Policy name.")]),
        tool("get_runbook", "An operations runbook by name (e.g. 'seal failure triage', 'recall outreach').", [("name", "string|Runbook name.")]),
        tool("search_incidents", "Search past quality incidents and field failures by symptom, SKU or lot.", ["query", "limit"], ["query"]),
        tool("list_bulletins", "Technical and safety bulletins issued in a period.", [("since", "string|ISO date.")], []),
        tool("get_bulletin", "A technical or safety bulletin by ID (e.g. TSB-2026-09).", [("bulletin_id", "string|Bulletin ID.")]),
        tool("get_faq", "Frequently asked questions and their approved answers for a topic.", [("topic", "string|Topic, e.g. warranty, returns, atex.")]),
        tool("get_training_material", "Training modules and videos for a product family or a task.", [("topic", "string|Family or task.")]),
    ]),
    "communications": ("Outbound messages, approvals, tasks and hand-offs to people.", [
        tool("send_email", "Send an email from support@kestrel-pumps.example. External side effect: cannot be unsent.",
             [("to", "string|Recipient email."), ("subject", "string|Subject line."), ("body", "string|Plain-text body.")], risk="irreversible", pii=True),
        tool("send_sms", "Send a text message to a contact's mobile number.", ["contact_id", ("text", "string|Message text, max 480 characters.")],
             risk="irreversible", pii=True),
        tool("post_teams_message", "Post to an internal Teams channel (logistics, billing, quality, field-service).",
             [("channel", "string|Channel name."), ("text", "string|Message text.")], risk="write"),
        tool("create_ticket_comment", "Add an internal comment to a support or service ticket.", ["ticket_id", ("text", "string|Comment text.")], risk="write"),
        tool("notify_account_manager", "Notify the customer's account manager with a summary and a link.", ["customer_id", ("summary", "string|One-paragraph summary.")],
             risk="write"),
        tool("request_approval", "Ask a named approver role to approve an action; returns an approval ID to poll.",
             [("approver_role", "string|support_manager, finance_director, quality_lead, security."), ("action", "string|What is being approved."),
              ("justification", "string|Why.")], risk="write"),
        tool("create_calendar_hold", "Place a tentative hold on a person's calendar.", ["email", ("start", "string|ISO datetime."), ("end", "string|ISO datetime."),
             ("title", "string|Event title.")], risk="write", pii=True),
        tool("create_task", "Create a follow-up task for a team with a due date.", [("team", "string|Team name."), ("title", "string|Task title."),
             ("due_date", "string|ISO date."), "note"], ["team", "title", "due_date"], risk="write"),
        tool("log_call", "Log a phone call with a contact (summary and outcome).", ["contact_id", ("summary", "string|Call summary.")], risk="write", pii=True),
        tool("escalate_to_human", "Hand the conversation to a human queue with a priority and a summary; ends the agent's involvement.",
             ["queue", "priority", ("summary", "string|What the human needs to know.")], risk="write", core=True),
    ]),
}


def build(out_dir: Path) -> dict:
    tools: list[dict] = []
    domains = []
    for domain, (blurb, items) in DOMAINS.items():
        assert len(items) == 10, (domain, len(items))
        for t in items:
            t["meta"]["domain"] = domain
            tools.append(t)
        domains.append({"name": domain, "description": blurb, "tools": [t["name"] for t in items]})
    names = [t["name"] for t in tools]
    assert len(names) == len(set(names)) == 120, len(names)
    catalog = {"generated_at": AS_OF.isoformat(), "count": len(tools), "domains": domains, "tools": tools}
    write_json(out_dir / "catalog.json", catalog)
    return catalog
