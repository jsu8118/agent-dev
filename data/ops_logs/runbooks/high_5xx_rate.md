# Runbook RB-01: High 5xx error rate on Kestrel Connect

**Alert:** `api-gateway 5xx rate > 2% for 5 minutes` (PagerDuty service: kestrel-connect).

## 1. Triage (first 10 minutes)
1. Identify **which upstream service** returns the errors: group api-gateway logs by `upstream` and `status` for the last 30 minutes.
2. Check **recent deploys** (`deploys.csv` / deploy dashboard) for that service in the last 2 hours. A deploy followed by errors is the most likely cause (≈ 60% of our incidents).
3. Check dependencies of the failing service: database (connection pool, slow queries), downstream services, rate limits.

## 2. Mitigate
* If a deploy correlates with the start of the errors: **roll back first, debug later** (RB-04).
* If a dependency is saturated: see RB-02 (DB connection pool) or scale out.

## 3. Communicate
* Post in #incident-kestrel-connect every 15 minutes: impact, current hypothesis, next step.
* Customer-facing status page update if impact > 10 minutes.

## 4. After
* Blameless post-incident review within 3 business days. Record: detection time, time to mitigate, root cause, follow-ups.
