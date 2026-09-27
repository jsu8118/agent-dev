"""Step 8: an internal MCP server exposing the copilot's read-only capabilities to staff (Day 5).

Support leads and quality engineers use Claude Code or Claude Desktop. Instead of building them a UI,
we expose three things over MCP:

    tool      preview_triage       what the pipeline WOULD do with an email (screen, triage, route) - no side effects
    tool      quality_hold_check   which held supplier lots a unit or order contains
    resource  kestrel://quality-holds          the holds and their incidents
    prompt    field_evidence_summary(lot)      a ready-made request for a quality engineer

Nothing here writes: no replies, escalations or RMAs. The customer-facing pipeline stays the only
writer, which keeps its audit trail complete. Run it as a stdio server for Claude Code with:

    claude mcp add kestrel-copilot -- python day7_capstone/reference/copilot/mcp_server.py --serve
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

if __package__ in (None, ""):                         # started as a script (stdio server for Claude Code)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    __package__ = "copilot"

from labkit import get_client, runs_dir                      # noqa: E402
from labkit.data import ops_db, read_text                    # noqa: E402

from .config import DEFAULT, QUALITY_HOLDS                   # noqa: E402
from .gate import decide_route                               # noqa: E402
from .screen import SERIAL_RE, screen_email                  # noqa: E402
from .triage import InboundEmail, triage_email               # noqa: E402

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)
ORDER_RE = re.compile(r"^SO-\d{5}$")


class TriagePreview(BaseModel):
    route: str = Field(description="safety | security | human | agent")
    reasons: list[str]
    review_required: bool = Field(description="True when the agent's draft would be held for a human")
    security_flags: list[str]
    safety_hits: list[str]
    category: str | None
    priority: str | None
    requires_human: bool | None
    summary: str | None


class HeldUnit(BaseModel):
    serial_number: str
    sku: str
    order_id: str
    customer_id: str
    build_date: str
    lot: str
    part: str
    incident: str


class HoldCheck(BaseModel):
    checked: str
    units_checked: int
    held_units: list[HeldUnit]


def create_server() -> MCPServer:
    mcp = MCPServer("kestrel-copilot", version="1.0.0", log_level="WARNING", instructions=(
        "Internal, read-only tools of Kestrel's Service Desk Copilot (data as of 2026-09-15). preview_triage "
        "shows how the pipeline would route an email without acting on it; quality_hold_check finds units built "
        "with supplier lots on quality hold. For Kestrel staff only: results include customer and build data."))
    client = None

    def _client():
        nonlocal client
        client = client or get_client()
        return client

    @mcp.tool(annotations=READ_ONLY)
    async def preview_triage(from_email: str, subject: str, body: str) -> TriagePreview:
        """Screen and triage an email and show the route the copilot would take (safety, security, human
        or agent), with reasons. Nothing is sent, escalated or written. Use it to check how a tricky email
        would be handled, or why an email was routed the way it was."""
        email = InboundEmail(from_email.strip().lower(), subject, body, "preview")
        screen = screen_email(email.from_email, subject, body)
        # Like the pipeline: suspicious text never reaches a model. The LLM call runs in a worker thread
        # so the server's event loop stays responsive.
        triage = None if screen.suspicious else await anyio.to_thread.run_sync(
            lambda: triage_email(_client(), email, config=DEFAULT))
        decision = decide_route(screen, triage)
        return TriagePreview(route=decision.route, reasons=decision.reasons, review_required=decision.review,
                             security_flags=screen.security_flags, safety_hits=screen.safety_hits,
                             category=triage.category if triage else None, priority=triage.priority if triage else None,
                             requires_human=triage.requires_human if triage else None,
                             summary=triage.summary if triage else None)

    @mcp.tool(annotations=READ_ONLY)
    def quality_hold_check(order_id: str | None = None, serial_number: str | None = None) -> HoldCheck:
        """Check whether the units of an order (e.g. SO-10272) or one serial number (e.g. KP250-2608-0006)
        were built with a supplier lot on quality hold. Returns the held units with lot, part and incident."""
        if bool(order_id) == bool(serial_number):
            raise ToolError("Give exactly one of order_id (SO-#####) or serial_number (e.g. KP250-2608-0006).")
        if order_id:
            key = order_id.strip().upper()
            if not ORDER_RE.match(key):
                raise ToolError(f"'{order_id}' is not an order ID. Kestrel order IDs look like SO-10272.")
            where, param = "b.order_id = ?", key
        else:
            key = serial_number.strip().upper()
            if not SERIAL_RE.fullmatch(key):
                raise ToolError(f"'{serial_number}' is not a serial number. They look like KP250-2608-0006.")
            where, param = "b.serial_number = ?", key
        conn = ops_db()
        try:
            rows = conn.execute("SELECT b.*, o.customer_id FROM build_records b JOIN orders o USING (order_id) "
                                f"WHERE {where}", (param,)).fetchall()
        finally:
            conn.close()
        if not rows:
            raise ToolError(f"No build records for {key}. Only Kestrel-built pumps and controllers have them.")
        held = [HeldUnit(serial_number=r["serial_number"], sku=r["sku"], order_id=r["order_id"],
                         customer_id=r["customer_id"], build_date=r["build_date"], lot=lot,
                         part=QUALITY_HOLDS[lot]["part"], incident=QUALITY_HOLDS[lot]["incident"])
                for r in rows for lot in (r["seal_lot"], r["board_lot"]) if lot in QUALITY_HOLDS]
        return HoldCheck(checked=key, units_checked=len(rows), held_units=held)

    @mcp.resource("kestrel://quality-holds", name="quality_holds", mime_type="application/json",
                  description="Supplier lots on quality hold, with part, supplier, incident and risk.")
    def quality_holds() -> str:
        return json.dumps(QUALITY_HOLDS, indent=2)

    @mcp.prompt(title="Field-evidence summary for a held lot")
    def field_evidence_summary(lot: str) -> list[dict]:
        """Ask for a summary of customer-reported field evidence on one held lot, for the incident review."""
        if lot not in QUALITY_HOLDS:
            raise ValueError(f"Unknown lot {lot!r}. Held lots: {', '.join(QUALITY_HOLDS)}.")
        hold = QUALITY_HOLDS[lot]
        incident = read_text("quality", "incident_reports", f"{hold['incident']}.md")
        outbox = runs_dir("capstone") / "quality_alerts.jsonl"
        alerts = [a for a in (json.loads(l) for l in outbox.read_text(encoding="utf-8").splitlines() if l.strip())
                  if a["lot"] == lot] if outbox.exists() else []
        return [
            {"role": "user", "content": {"type": "resource", "resource": {
                "uri": f"kestrel://incidents/{hold['incident']}", "mime_type": "text/markdown", "text": incident}}},
            {"role": "user", "content": (
                f"Summarize the field evidence on {hold['part']} lot {lot} ({hold['supplier']}) for the review of "
                f"{hold['incident']}. Copilot alerts so far ({len(alerts)}):\n{json.dumps(alerts, indent=1)[:6000]}\n\n"
                "For each alert: customer, serials, symptom. Then: how many distinct customers and units, whether "
                "the symptoms match the failure mode in the incident report, and which other shipped units "
                "(use quality_hold_check) should be contacted proactively. Facts only; mark any inference as such.")},
        ]

    return mcp


mcp = create_server()

if __name__ == "__main__":
    if "--serve" in sys.argv:
        mcp.run(transport="stdio")          # JSON-RPC on stdin/stdout; logs go to stderr
    else:
        print("Use --serve to run as a stdio MCP server, or run ../run_mcp_selftest.py to try it in-process.")
