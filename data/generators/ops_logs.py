"""Service logs for the Kestrel Connect order portal (Day 5 SRE investigation agent).

48 hours of JSON-lines logs from four services.  On 2026-09-14 a deploy of order-service
(v2.14.0) lowered the DB connection pool from 50 to 5, exhausting it within minutes and
causing 503s on /api/orders until a rollback.  Red herrings: daily TLS-expiry warnings in
payment-service, occasional slow-query warnings in inventory-service, and a bot scanning
/wp-admin the night before.
"""

from __future__ import annotations

import datetime as dt
import json
import random
from pathlib import Path

START = dt.datetime(2026, 9, 13, 0, 0, 0)
END = dt.datetime(2026, 9, 15, 0, 0, 0)
DEPLOY_BAD = dt.datetime(2026, 9, 14, 9, 12, 3)
POOL_SATURATED = dt.datetime(2026, 9, 14, 9, 15, 0)
ERRORS_START = dt.datetime(2026, 9, 14, 9, 19, 0)
ROLLBACK = dt.datetime(2026, 9, 14, 10, 5, 12)
RECOVERED = dt.datetime(2026, 9, 14, 10, 8, 0)

ROUTES = [("GET", "/api/orders/{id}", "order-service", 0.34), ("GET", "/api/orders", "order-service", 0.14),
          ("POST", "/api/orders", "order-service", 0.05), ("GET", "/api/inventory/{sku}", "inventory-service", 0.24),
          ("GET", "/api/shipments/{id}", "order-service", 0.1), ("POST", "/api/payments", "payment-service", 0.04),
          ("GET", "/api/health", "api-gateway", 0.09)]


def _ts(t: dt.datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%S.") + f"{t.microsecond // 1000:03d}Z"


def build(rng: random.Random, out_dir: Path) -> None:
    log_dir = out_dir / "order-portal"
    log_dir.mkdir(parents=True, exist_ok=True)
    logs: dict[str, list[tuple[dt.datetime, dict]]] = {s: [] for s in
                                                       ("api-gateway", "order-service", "inventory-service", "payment-service")}

    def emit(service: str, t: dt.datetime, level: str, msg: str, **fields) -> None:
        logs[service].append((t, {"ts": _ts(t), "level": level, "service": service, "msg": msg, **fields}))

    def order_version(t: dt.datetime) -> str:
        return "2.14.0" if DEPLOY_BAD <= t < ROLLBACK else "2.13.4"

    t = START
    req = 0
    while t < END:
        hour = t.hour + t.minute / 60
        busy = 0.35 + 0.65 * max(0.0, min(1.0, (hour - 6) / 4)) * max(0.0, min(1.0, (20 - hour) / 4))
        t += dt.timedelta(seconds=rng.expovariate(busy / 40), milliseconds=rng.randint(0, 999))
        if t >= END:
            break
        req += 1
        method, route, upstream, _ = rng.choices(ROUTES, [r[3] for r in ROUTES])[0]
        path = route.replace("{id}", f"SO-{rng.randint(10001, 10310)}").replace("{sku}", rng.choice(
            ["KP-250-S", "MS-250", "BRG-6309", "KV-50-F", "KC-2"]))
        rid = f"req-{t:%d%H%M%S}-{req:05d}"
        incident = upstream == "order-service" and ERRORS_START <= t < RECOVERED
        status, latency = 200, rng.randint(18, 140)
        if upstream == "order-service" and POOL_SATURATED <= t < ERRORS_START:
            latency = rng.randint(400, 2600)
        if incident:
            if rng.random() < 0.62:
                status, latency = 503, rng.randint(3001, 3150)
            else:
                latency = rng.randint(900, 2900)
        elif rng.random() < 0.004:
            status = rng.choice([404, 400, 500])
        emit("api-gateway", t, "INFO" if status < 500 else "ERROR", "request", method=method, path=path,
             status=status, latency_ms=latency, upstream=upstream, request_id=rid)
        if upstream == "order-service":
            active = rng.randint(3, 14)
            pool_max = 5 if DEPLOY_BAD <= t < ROLLBACK else 50
            if pool_max == 5:
                active = 5
            if status == 503:
                emit("order-service", t + dt.timedelta(milliseconds=latency - 5), "ERROR",
                     "timeout acquiring DB connection after 3000ms", request_id=rid, version=order_version(t),
                     pool_active=active, pool_max=pool_max, waiting=rng.randint(18, 64))
            elif rng.random() < 0.35:
                emit("order-service", t + dt.timedelta(milliseconds=latency - 3), "INFO", "order query ok",
                     request_id=rid, version=order_version(t), pool_active=active, pool_max=pool_max,
                     db_query_ms=rng.randint(4, 40) if pool_max == 50 else rng.randint(4, 40))
        elif upstream == "inventory-service" and rng.random() < 0.4:
            slow = rng.random() < 0.03
            emit("inventory-service", t + dt.timedelta(milliseconds=latency - 2), "WARN" if slow else "INFO",
                 "slow query" if slow else "stock lookup ok", request_id=rid,
                 db_query_ms=rng.randint(700, 1100) if slow else rng.randint(3, 25))
        elif upstream == "payment-service":
            emit("payment-service", t + dt.timedelta(milliseconds=latency - 2), "INFO", "payment authorized",
                 request_id=rid, provider="acquirer-x", amount_usd=round(rng.uniform(80, 25000), 2))

    # periodic and one-off events
    for day, days_left in ((13, 12), (14, 11)):
        emit("payment-service", dt.datetime(2026, 9, day, 6, 0, 0), "WARN",
             f"TLS certificate for payments-gw.internal expires in {days_left} days (auto-renewal scheduled)",
             cert_cn="payments-gw.internal")
    emit("inventory-service", dt.datetime(2026, 9, 13, 15, 2, 10), "INFO", "starting inventory-service",
         version="1.8.2")
    emit("payment-service", dt.datetime(2026, 9, 13, 17, 40, 31), "INFO", "starting payment-service", version="3.4.1")
    emit("order-service", DEPLOY_BAD, "INFO", "starting order-service", version="2.14.0",
         config={"db_pool_max": 5, "db_pool_timeout_ms": 3000, "feature_order_history_pagination": True})
    emit("order-service", POOL_SATURATED, "WARN", "connection pool at capacity", version="2.14.0",
         pool_active=5, pool_max=5, waiting=3)
    emit("api-gateway", dt.datetime(2026, 9, 14, 9, 24, 0), "WARN", "5xx rate above threshold",
         window="5m", rate_pct=18.4, upstream="order-service")
    emit("order-service", ROLLBACK, "INFO", "starting order-service", version="2.13.4",
         config={"db_pool_max": 50, "db_pool_timeout_ms": 3000, "feature_order_history_pagination": False})
    emit("api-gateway", dt.datetime(2026, 9, 14, 10, 13, 0), "INFO", "5xx rate back to normal", window="5m",
         rate_pct=0.2, upstream="order-service")
    for i in range(40):                                   # bot scanning the night before
        bt = dt.datetime(2026, 9, 13, 22, 0, 0) + dt.timedelta(seconds=i * 7)
        emit("api-gateway", bt, "INFO", "request", method="GET", path=rng.choice(
            ["/wp-admin/", "/wp-login.php", "/.env", "/admin/config.php"]), status=404, latency_ms=3,
            upstream="api-gateway", request_id=f"req-bot-{i:03d}", client="185.220.101.47")

    for service, entries in logs.items():
        entries.sort(key=lambda e: e[0])
        (log_dir / f"{service}.log").write_text("".join(json.dumps(e[1]) + "\n" for e in entries), encoding="utf-8")

    deploys = [
        ("D-3301", "inventory-service", "1.8.2", "2026-09-13T15:01:40Z", "2026-09-13T15:04:02Z", "s.ito",
         "Add bin-location field to stock lookup", ""),
        ("D-3302", "payment-service", "3.4.1", "2026-09-13T17:39:55Z", "2026-09-13T17:42:10Z", "r.das",
         "Bump acquirer SDK to 5.2", ""),
        ("D-3303", "order-service", "2.14.0", "2026-09-14T09:11:48Z", "2026-09-14T09:13:30Z", "j.moreau",
         "Order history pagination; Helm values cleanup", "db.pool.max: 50 -> 5"),
        ("D-3304", "order-service", "2.13.4", "2026-09-14T10:05:00Z", "2026-09-14T10:07:45Z", "oncall.k.lam",
         "Rollback of D-3303", "db.pool.max: 5 -> 50"),
    ]
    with (out_dir / "deploys.csv").open("w", encoding="utf-8") as fh:
        fh.write("deploy_id,service,version,started_at,finished_at,author,change_summary,config_diff\n")
        for row in deploys:
            fh.write(",".join(f'"{v}"' if "," in v or ";" in v else v for v in row) + "\n")

    truth = {
        "root_cause": "order-service deploy D-3303 (v2.14.0) reduced db.pool.max from 50 to 5; the pool saturated and "
                      "requests timed out acquiring connections (3000 ms), producing 503s at the gateway",
        "impact": "503 errors and ~3 s latency on order-service routes (/api/orders*, /api/shipments/*) from about "
                  "09:19 to 10:08 UTC on 2026-09-14",
        "detection": "gateway 5xx-rate warning at 09:24 UTC",
        "mitigation": "rollback D-3304 to v2.13.4 at 10:05 UTC; recovered by 10:08 UTC",
        "red_herrings": ["payment-service TLS expiry warnings (11-12 days left, auto-renewal scheduled)",
                         "inventory-service occasional slow-query warnings (baseline)",
                         "bot scanning /wp-admin on 2026-09-13 22:00 (404s)"],
        "follow_ups": ["validate Helm values in CI (pool size bounds)", "alert on pool saturation before 5xx",
                       "canary deploys for order-service"],
    }
    (out_dir / "incident_ground_truth.json").write_text(json.dumps(truth, indent=2), encoding="utf-8")
