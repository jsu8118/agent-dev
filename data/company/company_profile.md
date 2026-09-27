# Kestrel Pumps & Controls — Company Profile (fictional)

> **All companies, people, products, and data in this course are fictional.** Any
> resemblance to real organizations is coincidental. Email domains use the reserved
> `.example` top-level domain.

## At a glance

| Item | Detail |
|---|---|
| Founded | 1987, Port Aldren |
| Employees | ~1,400 |
| Revenue | ~$410M (FY2025) |
| Business | Designs and manufactures industrial centrifugal pumps, valves, pump controllers, sensors, and spare parts; provides installation and field service |
| Customers | ~2,300 B2B accounts: water utilities, chemical & process plants, food & beverage, mining, HVAC distributors, data centers, marine/offshore |
| "As-of" date for all course data | **2026-09-15** (treat this as "today" in every lab) |

## Sites

| Code | Site | Role |
|---|---|---|
| P1 | Riverside plant | Castings machining, impellers, shafts, pump casings |
| P2 | Lakeside plant | Pump assembly, mechanical seal installation, hydrostatic testing |
| P3 | Hillview plant | Controllers (KC-1/KC-2) electronics assembly, sensor kits, final test |
| WH-EAST | Port Aldren warehouse | Domestic distribution (east) |
| WH-WEST | Sierra Vista warehouse | Domestic distribution (west) |
| WH-EU | Rotterdam warehouse | EU and UK distribution |

## Product lines

| Line | Families | Notes |
|---|---|---|
| Pumps | KP-100 (compact end-suction), KP-250 (end-suction, 22 kW), KP-250-X (ATEX explosion-proof), KP-400 (horizontal split-case, 75 kW), KP-600 (multistage high-pressure, 110 kW) | Built to order except KP-100 and KP-250-S |
| Valves | KV-20 ball, KV-50 butterfly, KV-80 gate | Stocked |
| Controllers | KC-1 VFD pump controller, KC-2 smart controller (IoT, Modbus/TCP) | Often *configured* with customer parameter sets (non-returnable once configured) |
| Sensors | VS-10 vibration, PT-40 pressure, FT-60 flow | Stocked |
| Spare parts | Seal kits (MS-100/250/400), impellers, bearings, couplings, gaskets, strainers, grease | Stocked |
| Services | SVC-INSTALL (installation day), SVC-ALIGN (laser alignment) | Not returnable |

## Customer tiers

| Tier | Discount off list | Service level |
|---|---|---|
| Strategic | 12% | Named account manager, advance replacement on warranty claims, 24/7 escalation line |
| Key | 8% | Named account manager, priority RMA handling |
| Standard | 0% | Standard support hours |

## Customer support organization

* **Channels:** email (support@kestrel-pumps.example), customer portal *Kestrel Connect*, phone.
* **Hours:** Mon–Fri 07:00–19:00 ET; P1 (safety or critical outage) 24/7 via on-call Field Service Engineer (FSE).
* **Volume:** ~1,900 tickets/month; 38% order status & shipping, 17% technical, 14% returns & warranty, 12% billing, 19% other.
* **Systems:** *Atlas ERP* (orders, shipments, invoices, RMAs), *Kestrel Connect* portal, knowledge base of product manuals and policies.

## Why Kestrel is investing in AI agents (the course's running case study)

1. **Support backlog:** median first response time is 9.5 business hours; customers want minutes. Most tickets require looking up ERP data and applying a policy — a good fit for tool-using agents.
2. **Field service knowledge:** senior technicians are retiring; troubleshooting knowledge lives in manuals and maintenance logs that junior techs struggle to search.
3. **Accounts payable:** ~1,200 supplier invoices/month are matched manually against purchase orders and goods receipts; exceptions are slow and error-prone.
4. **Reliability engineering:** the *Kestrel Connect* order portal had 4 incidents last quarter; on-call engineers spend the first 30 minutes of each incident just reading logs.

Leadership's constraints (these shape every design decision in the course):

* No agent may issue refunds above policy limits or change supplier bank details without a human.
* Every agent action on customer data must be auditable (who/what/when/why).
* Cost per resolved ticket must stay below $0.40 on average; latency for customer-facing replies under 30 seconds.
* Safety-related tickets must always reach a human within 1 hour.
