# Returns, RMAs and Refunds (Policy RET-002, revision 2026-05)

## 1. Returns of non-defective items

* Standard catalog items may be returned within **30 calendar days of delivery**.
* Items must be **unused and in original packaging**.
* A **15% restocking fee** (of the returned line value) applies to non-defective returns.
* The restocking fee is **waived** when the return is caused by Kestrel: wrong item shipped, or damage in transit reported within 5 days of delivery.

## 2. Non-returnable items

* **Configured controllers** — KC-1 / KC-2 units shipped with a customer-specific parameter set (the order line is flagged "configured"). These can only be handled under warranty (Policy WAR-001).
* **Services** (SKU prefix SVC-).
* Items marked **special order** on the quote or order.
* Electrical components (controllers, sensors) **once installed**, except under warranty.

## 3. Damaged in transit

* Report within **5 calendar days** of delivery, with photos of the packaging and item.
* Kestrel files the carrier claim; the customer does not need to.
* A replacement ships immediately (no need to wait for the damaged item to come back). No restocking fee.

## 4. Wrong item shipped

* Kestrel provides a prepaid return label.
* The correct item ships as soon as the RMA is approved.

## 5. RMA process

1. Every return needs an **RMA number** (format `RMA-####`). Returns without an RMA are refused at the dock.
2. Support creates the RMA in Atlas ERP with: order ID, SKU, quantity, reason code (`defective`, `wrong_item`, `damaged_in_transit`, `no_longer_needed`, `warranty_claim`).
3. The customer ships within 15 days of RMA approval.

## 6. Refunds

* Refunds are issued to the original payment method within **10 business days** after the returned item is received and inspected.
* **Refund approval limits (hard rule):**

| Refund amount (per request) | Who may approve |
|---|---|
| up to **$2,500** | Support agent (including the AI support assistant, where enabled) |
| **$2,500.01 – $10,000** | Support Manager |
| above **$10,000** | Finance Director |

* A refund may never exceed the amount paid for the returned lines, minus any applicable restocking fee.
* Splitting one refund into several smaller refunds to stay under a limit is prohibited.
