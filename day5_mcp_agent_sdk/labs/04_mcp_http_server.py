"""Lab 04 - The same MCP server over Streamable HTTP, protected like a production resource server.

Objective
    Serve lab 01's plant-ops server over Streamable HTTP (uvicorn, background thread, free port),
    require OAuth-style bearer tokens with a scope, connect a client over HTTP, look at the raw HTTP
    exchanges, and shut everything down cleanly.  Then see what it would take to let the Claude
    API itself call this server (the MCP connector).

Concepts
    Streamable HTTP transport (one endpoint: POST for requests, optional SSE streams); stateful
    (Mcp-Session-Id, legacy handshake) vs stateless (2026-07-28) operation; the MCP server as an
    OAuth 2.1 *resource server*: 401 + WWW-Authenticate -> Protected Resource Metadata (RFC 9728)
    -> token from your authorization server -> scopes checked per request; token audience
    (RFC 8707 resource indicators) and why tokens must never be passed through; DNS-rebinding
    protection; graceful shutdown.

Run
    python day5_mcp_agent_sdk/labs/04_mcp_http_server.py

What to observe
    * Without a token the server answers 401 and tells the client where to discover how to get one.
    * A valid token with the wrong scope gets 403 insufficient_scope - authorization, not just authentication.
    * With the right token, the same MCP calls as lab 02 flow over plain HTTP POSTs.
    * In legacy mode the server hands out an Mcp-Session-Id and the client DELETEs it on close; in
      2026-07-28 mode each request stands alone, which is what lets you load-balance replicas.
    * The server thread stops and the port is released at the end (no leaked listeners).
"""

# test: expect=Server stopped cleanly

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import httpx2
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings

from _mcp_common import load_lab, tool_result_text
from labkit import MODEL, header, step

server_lab = load_lab("01_mcp_server")
SCOPE = "plant-ops:read"
ISSUER = "https://auth.kestrel.example"          # Kestrel's (fictional) authorization server / IdP


class StaticTokenVerifier:
    """Stand-in for real token validation.

    Production: verify a JWT signature against the IdP's JWKS (issuer, audience, expiry) or call the
    IdP's introspection endpoint (RFC 7662), and cache the result for a short time.
    """

    def __init__(self, resource: str) -> None:
        self._tokens = {
            "tok-ops-bot": AccessToken(token="tok-ops-bot", client_id="ops-assistant", scopes=[SCOPE], resource=resource),
            "tok-reporting": AccessToken(token="tok-reporting", client_id="bi-dashboard", scopes=["reports:read"],
                                         resource=resource),
            "tok-other-api": AccessToken(token="tok-other-api", client_id="ops-assistant", scopes=[SCOPE],
                                         resource="https://billing.kestrel.example/mcp"),
        }

    async def verify_token(self, token: str) -> AccessToken | None:
        return self._tokens.get(token)


@contextmanager
def serve_http() -> Iterator[str]:
    """Start the plant-ops server on a free port; yield its MCP endpoint URL; always shut down."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))          # port 0 = let the OS pick a free port (tests run in parallel)
    base = f"http://127.0.0.1:{sock.getsockname()[1]}"
    mcp_url = f"{base}/mcp"
    server = server_lab.create_server(
        token_verifier=StaticTokenVerifier(resource=mcp_url),
        auth=AuthSettings(issuer_url=ISSUER, resource_server_url=mcp_url, required_scopes=[SCOPE],
                          # Reject tokens minted for another resource (RFC 8707 audience binding).
                          validate_token_resource=True))
    app = server.streamable_http_app()   # Starlette ASGI app: mount it in FastAPI or run it behind a proxy
    uv = uvicorn.Server(uvicorn.Config(app, log_level="warning", lifespan="on"))
    thread = threading.Thread(target=uv.run, kwargs={"sockets": [sock]}, daemon=True, name="mcp-http")
    thread.start()
    deadline = time.monotonic() + 15
    while not uv.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError("MCP HTTP server failed to start")
        time.sleep(0.02)
    try:
        yield mcp_url
    finally:
        uv.should_exit = True             # graceful: finish in-flight requests, close SSE streams
        thread.join(timeout=10)
        sock.close()
        print(f"\nServer stopped cleanly: thread alive={thread.is_alive()}")


def http_logger(log: list[str]):
    """httpx2 event hooks: record each HTTP exchange the MCP client makes."""
    async def on_response(response: httpx2.Response) -> None:
        req = response.request
        interesting = {k: v for k, v in req.headers.items() if k.lower().startswith("mcp-") or k.lower() == "authorization"}
        if "authorization" in interesting:
            interesting["authorization"] = interesting["authorization"][:13] + "..."   # never log full tokens
        session = response.headers.get("mcp-session-id")
        log.append(f"{req.method:<6} {req.url.path} -> {response.status_code} "
                   f"{response.headers.get('content-type', '-').split(';')[0]:<17} req-headers={interesting}"
                   + (f"  mcp-session-id={session[:12]}..." if session else ""))
    return {"response": [on_response]}


async def run() -> None:
    header("Lab 04 - MCP over Streamable HTTP with bearer-token auth")
    with serve_http() as url:
        print(f"MCP endpoint: {url}   (bound to 127.0.0.1 only; DNS-rebinding protection is on for localhost)")

        step(1, "No token: 401 + where to find the authorization server")
        async with httpx2.AsyncClient() as http:
            probe = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}
            accept = {"accept": "application/json, text/event-stream"}
            r = await http.post(url, json=probe, headers=accept)
            print(f"POST /mcp without token -> {r.status_code}\n  WWW-Authenticate: {r.headers.get('www-authenticate')}")
            metadata_url = r.headers["www-authenticate"].split('resource_metadata="')[1].rstrip('"')
            prm = (await http.get(metadata_url)).json()
            print(f"GET {metadata_url.split('127.0.0.1')[1].split('/', 1)[1]} -> {json.dumps(prm)}")

            step(2, "Wrong scope and wrong audience are refused too")
            for token, label in (("tok-reporting", "scope reports:read only"),
                                 ("tok-other-api", "token minted for billing's MCP server")):
                r = await http.post(url, json=probe, headers={**accept, "authorization": f"Bearer {token}"})
                print(f"{label:<38} -> {r.status_code} {r.json().get('error')}: {r.json().get('error_description')}")

        step(3, "A properly scoped token: the MCP client over HTTP (2026-07-28, stateless)")
        wire: list[str] = []
        http = httpx2.AsyncClient(headers={"authorization": "Bearer tok-ops-bot"}, event_hooks=http_logger(wire))
        async with Client(streamable_http_client(url, http_client=http)) as client:
            print(f"connected: protocol {client.protocol_version}, server {client.server_info.name}")
            stock = await client.call_tool("check_stock", {"sku": "MS-250"})
            print(f"check_stock -> total_available={stock.structured_content['total_available']}")
            order = await client.call_tool("get_order_status", {"order_id": "SO-10283"})
            print(f"get_order_status -> {tool_result_text(order, 90)}")
        await http.aclose()
        print("HTTP exchanges:")
        for line in wire:
            print("  " + line)

        step(4, "Legacy (2025-11-25) clients: a stateful session with Mcp-Session-Id")
        wire.clear()
        http = httpx2.AsyncClient(headers={"authorization": "Bearer tok-ops-bot"}, event_hooks=http_logger(wire))
        async with Client(streamable_http_client(url, http_client=http), mode="legacy") as client:
            await client.call_tool("check_stock", {"sku": "MS-400"})
        await http.aclose()
        for line in wire:
            print("  " + line)

        step(5, "Raw JSON-RPC over HTTP (what `curl` would send)")
        body = {"jsonrpc": "2.0", "id": 42, "method": "tools/call",
                "params": {"name": "check_stock", "arguments": {"sku": "MS-100"},
                           "_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28",
                                     "io.modelcontextprotocol/clientInfo": {"name": "curl", "version": "8"},
                                     "io.modelcontextprotocol/clientCapabilities": {}}}}
        headers = {"authorization": "Bearer tok-ops-bot", "accept": "application/json, text/event-stream",
                   "mcp-protocol-version": "2026-07-28", "mcp-method": "tools/call", "mcp-name": "check_stock"}
        async with httpx2.AsyncClient() as raw:
            r = await raw.post(url, json=body, headers=headers)
        result = r.json()["result"]
        print(f"HTTP {r.status_code} {r.headers.get('content-type')}: id={r.json()['id']} "
              f"isError={result['isError']} total_available={result['structuredContent']['total_available']}")

    step(6, "Letting the Claude API call this server directly (MCP connector) - request shape only")
    connector_request = {
        "model": MODEL, "max_tokens": 8000, "betas": ["mcp-client-2025-11-20"],
        "mcp_servers": [{"type": "url", "url": "https://plant-ops.kestrel.example/mcp", "name": "kestrel-plant-ops",
                         "authorization_token": "<access token for plant-ops:read, from Kestrel's IdP>"}],
        "tools": [{"type": "mcp_toolset", "mcp_server_name": "kestrel-plant-ops",
                   "default_config": {"enabled": False},                      # allow-list mode
                   "configs": {"check_stock": {"enabled": True}, "get_order_status": {"enabled": True}}}],
        "messages": [{"role": "user", "content": "Can we ship 10 x MS-250 this week?"}],
    }
    print(json.dumps(connector_request, indent=2))
    print("Not sent: Anthropic's servers make the MCP connection, so the URL must be publicly reachable over "
          "HTTPS - our server listens on 127.0.0.1. Only tools are supported through the connector (no resources "
          "or prompts), and you obtain and refresh the OAuth token yourself.")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
