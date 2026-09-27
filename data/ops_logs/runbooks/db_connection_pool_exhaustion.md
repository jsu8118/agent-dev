# Runbook RB-02: Database connection pool exhaustion

**Symptoms:** services log `connection pool exhausted` / `timeout acquiring connection` (after `DB_POOL_TIMEOUT_MS`, default 3000 ms); request latency rises sharply; 503/504 at the gateway; database CPU is often **normal** (the bottleneck is in the application pool, not the database).

## Common causes
1. **Pool size misconfigured** (e.g., `DB_POOL_MAX` lowered by a config change). Our production value is **50** per instance for order-service.
2. Connection leak introduced by a code change (connections not returned on error paths).
3. Slow queries holding connections (check `db.query_ms` p95).
4. Traffic spike beyond capacity.

## Diagnose
* Compare `pool_active` / `pool_max` in the service logs before and after the start of the incident.
* Check the latest deploy's **config diff** for pool settings.
* If `pool_max` is unchanged and `pool_active` climbs steadily without traffic growth: suspect a leak.

## Mitigate
* Config regression: roll back the deploy (RB-04) or hot-fix the configuration.
* Leak: roll back; restart instances as a stop-gap (connections are released on restart).
