"""Capstone reference - smoke-test the copilot as an HTTP service (FastAPI + uvicorn, local only).

Starts the service on a free local port in a background thread, then plays the mail gateway:
posts emails, re-delivers one (idempotency), tries a request without the API key (401) and an
oversized one (422), and reads /metrics and the review queue. Everything runs on 127.0.0.1.
"""
# test: expect=Smoke test passed
# test: timeout=180

from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import httpx2
import uvicorn

from copilot.service import create_app
from labkit import get_client, header, step

API_KEY = "local-smoke-test-key"          # in production: a secret from your secret store, rotated


@contextmanager
def serve() -> Iterator[str]:
    """Run the app on a free port; yield its base URL; always shut down."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(get_client(), api_key=API_KEY), log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True, name="copilot-http")
    thread.start()
    deadline = time.time() + 20
    while not server.started:
        if time.time() > deadline:
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()


def main() -> None:
    header("Service Desk Copilot - HTTP service smoke test")
    auth = {"X-Api-Key": API_KEY}
    with serve() as base, httpx2.Client(base_url=base, timeout=120, trust_env=False) as http:  # local: no proxy
        step(1, "Health check")
        health = http.get("/healthz").json()
        print(health)
        assert health["status"] == "ok"

        step(2, "Authentication: a request without the API key is rejected")
        denied = http.post("/v1/emails", json={"message_id": "m-0", "from_email": "a@b.example", "body": "hi"})
        print(f"no key -> HTTP {denied.status_code}")
        assert denied.status_code == 401

        step(3, "Input limits: an oversized body is rejected before it costs a single token")
        huge = http.post("/v1/emails", headers=auth, json={"message_id": "m-big", "from_email": "a@b.example",
                                                           "body": "x" * 25_000})
        print(f"25,000-character body -> HTTP {huge.status_code}")
        assert huge.status_code == 422

        step(4, "The gateway posts three emails")
        emails = [
            {"message_id": "gw-1001", "from_email": "dana.whitfield@bluewater-utilities.example",
             "subject": "Seal leak on new KP-250 - warranty",
             "body": "One KP-250 from SO-10243 is leaking at the seal after 5 weeks in clean water. "
                     "Serial KP250-2608-0002. Please replace under warranty."},
            {"message_id": "gw-1002", "from_email": "brian.foster@westfield-health.example",
             "subject": "Smoke from controller cabinet",
             "body": "There is a burning smell and some smoke coming from the KC-1 controller cabinet in our boiler "
                     "room. We switched off the breaker. What should we do?"},
            {"message_id": "gw-1003", "from_email": "rick.albrecht@deltapaper.example",
             "subject": "Late deliveries - contract",
             "body": "SO-10227 was late again. Our lawyers are reviewing the supply contract and we expect a 10% "
                     "credit on that order."},
        ]
        first = {}
        for email in emails:
            out = http.post("/v1/emails", headers=auth, json=email).json()
            first[email["message_id"]] = out
            print(f"{email['message_id']}: route={out['route']:<8} disposition={out['disposition']:<11} "
                  f"escalations={[e['queue'] for e in out['escalations']]} "
                  f"alerts={[a['lot'] for a in out['quality_alerts']]} cost=${out['cost_usd']:.4f}")
        assert first["gw-1002"]["route"] == "safety"
        assert first["gw-1003"]["disposition"] == "review"

        step(5, "At-least-once delivery: the gateway re-sends gw-1001")
        again = http.post("/v1/emails", headers=auth, json=emails[0]).json()
        print(f"duplicate={again['duplicate']}  same trace={again['trace_id'] == first['gw-1001']['trace_id']}  "
              f"(the agent did not run again: no second RMA, no second quality alert)")
        assert again["duplicate"] and again["trace_id"] == first["gw-1001"]["trace_id"]

        step(6, "Staff views: review queue and metrics")
        queue = http.get("/v1/review-queue", headers=auth).json()
        for item in queue:
            print(f"review: {item['ticket_id']} because {item['reasons']}")
        metrics = http.get("/metrics", headers=auth).json()
        print(f"metrics: {metrics}")
        assert metrics["tickets"] == 3 and metrics["by_route"].get("safety") == 1

    print("\nSmoke test passed: auth, input limits, routing, idempotency, review queue and metrics.")


if __name__ == "__main__":
    main()
