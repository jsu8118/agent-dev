"""Lab 01 - Build an MCP server: "kestrel-plant-ops".

Objective
    Expose Kestrel's ERP extract (inventory, orders), maintenance history and product manuals
    through ONE Model Context Protocol server that any MCP host can use: Claude Code, Claude
    Desktop, the Claude Agent SDK, or the agents you write yourself (labs 03, 07).

Concepts
    MCP primitives and who controls them:
      * tools      (model-controlled)       check_stock, get_order_status, list_work_orders
      * resources  (application-controlled) kestrel://manuals, kestrel://manuals/{name}
      * prompts    (user-controlled)        draft_rma_email
    Typed tools: Python type hints -> JSON Schema (inputSchema) and, for pydantic return types,
    an outputSchema + structuredContent.  Tool errors vs protocol errors.  Read-only annotations.
    MCP Python SDK 2.x: `MCPServer` (the 1.x name was `FastMCP`).

Run
    python day5_mcp_agent_sdk/labs/01_mcp_server.py            # in-process self-test (no network, no LLM)
    python day5_mcp_agent_sdk/labs/01_mcp_server.py --serve    # run as a stdio MCP server (for a host)

    To use it from Claude Code, for example:
        claude mcp add kestrel-plant-ops -- python /abs/path/01_mcp_server.py --serve

What to observe
    * The inputSchema of each tool is generated from its signature; `limit` carries bounds.
    * Tools that return a pydantic model also publish an outputSchema, and their results carry
      `structured_content` (machine-readable) next to the text content (model-readable).
    * A bad SKU does not crash anything: it comes back as a result with is_error=True and a
      message that tells the caller how to recover.
    * In --serve mode NOTHING may be printed to stdout: stdout is the JSON-RPC channel.
"""

# test: expect=Self-test passed

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from labkit.config import DATA_DIR
from labkit.data import ops_db

SERVER_NAME = "kestrel-plant-ops"
MANUALS_DIR = DATA_DIR / "manuals"
POLICIES_DIR = DATA_DIR / "company" / "policies"
WORK_ORDERS_CSV = DATA_DIR / "maintenance" / "work_orders.csv"
ASSETS_CSV = DATA_DIR / "maintenance" / "assets.csv"

INSTRUCTIONS = (
    "Read-only access to Kestrel Pumps & Controls operations data (as of 2026-09-15): warehouse stock, "
    "sales-order status, and maintenance work orders for monitored pumps, plus product manuals as "
    "resources. Quantities are units; 'available' = on_hand - reserved. Internal use only: order data is "
    "not identity-checked, so do not connect this server to customer-facing agents."
)

# Every tool here only reads. Hosts may use the hint to parallelise calls or skip confirmations;
# it is a hint, not enforcement - the server itself opens the database read-only.
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)

SKU_RE = re.compile(r"^[A-Z]{2,4}(-[A-Z0-9]{1,5}){1,3}$")
ORDER_RE = re.compile(r"^SO-\d{5}$")


# --------------------------------------------------------------------------- result models
# Returning pydantic models (instead of dicts) gives hosts an outputSchema and a
# structuredContent payload they can validate - the text content is generated from it.
class WarehouseStock(BaseModel):
    warehouse: str
    on_hand: int
    reserved: int
    available: int
    reorder_point: int
    below_reorder_point: bool


class StockReport(BaseModel):
    sku: str
    name: str
    lead_time_days: int
    total_available: int
    warehouses: list[WarehouseStock]


class OrderLine(BaseModel):
    sku: str
    name: str
    qty: int


class Shipment(BaseModel):
    carrier: str | None
    tracking_number: str | None
    ship_date: str | None
    eta_date: str | None
    delivered_date: str | None
    status: str | None
    exception_reason: str | None


class OrderStatus(BaseModel):
    order_id: str
    customer_id: str
    customer_name: str
    status: str
    order_date: str
    promised_date: str | None
    customer_po: str | None
    lines: list[OrderLine]
    shipment: Shipment | None


class WorkOrder(BaseModel):
    work_order_id: str
    date: str
    type: str = Field(description="PM = preventive maintenance, CM = corrective maintenance")
    description: str
    parts_used: str
    technician: str
    downtime_hours: float


class WorkOrderHistory(BaseModel):
    asset_id: str
    site: str
    model: str
    install_date: str
    total_on_record: int
    work_orders: list[WorkOrder] = Field(description="Newest first")


# --------------------------------------------------------------------------- data helpers
def _normalise_sku(sku: str) -> str:
    sku = (sku or "").strip().upper()
    if not SKU_RE.match(sku):
        # The message is written for the caller (usually a model): say what a valid value looks like.
        raise ToolError(f"'{sku}' is not a Kestrel SKU. SKUs look like MS-250, KP-250-S or BRG-6309.")
    return sku


def _assets() -> dict[str, dict]:
    with ASSETS_CSV.open(encoding="utf-8", newline="") as fh:
        return {row["asset_id"]: row for row in csv.DictReader(fh)}


def _manual_names() -> list[str]:
    return sorted(p.stem for p in MANUALS_DIR.glob("*.md"))


def _title(path: Path) -> str:
    first = path.read_text(encoding="utf-8").splitlines()[0]
    return first.lstrip("# ").strip()


# --------------------------------------------------------------------------- the server
def create_server(**server_kwargs) -> MCPServer:
    """Build the plant-ops server.  Lab 04 calls this with auth settings for the HTTP variant."""
    mcp = MCPServer(SERVER_NAME, instructions=INSTRUCTIONS, version="1.0.0", log_level="WARNING",
                    **server_kwargs)

    # ------------------------------------------------------------------ tools (model-controlled)
    @mcp.tool(annotations=READ_ONLY)
    def check_stock(sku: str) -> StockReport:
        """Current stock of one SKU in every Kestrel warehouse (WH-EAST, WH-WEST, WH-EU).

        Returns on_hand, reserved and available (= on_hand - reserved) per warehouse, whether the
        warehouse is below its reorder point, the total available units, and the replenishment lead time.
        Use it before promising quantities or ship dates. Example SKU: MS-250 (KP-250 seal cartridge kit).
        """
        sku = _normalise_sku(sku)
        with ops_db() as db:
            product = db.execute("SELECT name, lead_time_days FROM products WHERE sku = ?", (sku,)).fetchone()
            if product is None:
                family = sku.split("-")[0]
                similar = [r[0] for r in db.execute("SELECT sku FROM products WHERE sku LIKE ? ORDER BY sku LIMIT 8",
                                                    (family + "-%",))]
                hint = f" Similar SKUs: {', '.join(similar)}." if similar else ""
                raise ToolError(f"Unknown SKU {sku}.{hint}")
            rows = db.execute("SELECT warehouse, on_hand, reserved, reorder_point FROM inventory WHERE sku = ? "
                              "ORDER BY warehouse", (sku,)).fetchall()
        warehouses = [WarehouseStock(warehouse=r["warehouse"], on_hand=r["on_hand"], reserved=r["reserved"],
                                     available=max(r["on_hand"] - r["reserved"], 0), reorder_point=r["reorder_point"],
                                     below_reorder_point=r["on_hand"] < r["reorder_point"]) for r in rows]
        return StockReport(sku=sku, name=product["name"], lead_time_days=product["lead_time_days"],
                           total_available=sum(w.available for w in warehouses), warehouses=warehouses)

    @mcp.tool(annotations=READ_ONLY)
    def get_order_status(order_id: str) -> OrderStatus:
        """Status of one sales order: customer, order/promised dates, lines (SKU and quantity) and the
        shipment (carrier, tracking, ETA, delivered date, exceptions). Order IDs look like SO-10283."""
        order_id = (order_id or "").strip().upper()
        if not ORDER_RE.match(order_id):
            raise ToolError(f"'{order_id}' is not a valid order ID. Kestrel order IDs look like SO-10283.")
        with ops_db() as db:
            order = db.execute("SELECT o.*, c.name AS customer_name FROM orders o JOIN customers c USING (customer_id) "
                               "WHERE order_id = ?", (order_id,)).fetchone()
            if order is None:
                raise ToolError(f"Order {order_id} not found in the ERP extract.")
            lines = [OrderLine(sku=r["sku"], name=r["name"], qty=r["qty"]) for r in db.execute(
                "SELECT l.sku, p.name, l.qty FROM order_lines l JOIN products p USING (sku) WHERE order_id = ? "
                "ORDER BY line_no", (order_id,))]
            ship = db.execute("SELECT * FROM shipments WHERE order_id = ?", (order_id,)).fetchone()
        shipment = Shipment(**{k: ship[k] for k in Shipment.model_fields}) if ship else None
        return OrderStatus(order_id=order_id, customer_id=order["customer_id"], customer_name=order["customer_name"],
                           status=order["status"], order_date=order["order_date"],
                           promised_date=order["promised_date"], customer_po=order["customer_po"], lines=lines,
                           shipment=shipment)

    @mcp.tool(annotations=READ_ONLY)
    def list_work_orders(asset_id: str,
                         limit: Annotated[int, Field(ge=1, le=50, description="Max work orders to return")] = 10,
                         ) -> WorkOrderHistory:
        """Maintenance history (work orders, newest first) of one monitored pump, e.g. HF-KP250-03.

        Each work order has a type (PM preventive / CM corrective), description, parts used
        (e.g. 'MS-250 x1' means one seal kit was fitted), technician and downtime. An empty list
        means nothing is on record - say so rather than guessing.
        """
        asset_id = (asset_id or "").strip().upper()
        assets = _assets()
        asset = assets.get(asset_id)
        if asset is None:
            raise ToolError(f"Unknown asset {asset_id}. Known assets: {', '.join(sorted(assets))}.")
        with WORK_ORDERS_CSV.open(encoding="utf-8", newline="") as fh:
            rows = [r for r in csv.DictReader(fh) if r["asset_id"] == asset_id]
        rows.sort(key=lambda r: r["date"], reverse=True)
        orders = [WorkOrder(work_order_id=r["work_order_id"], date=r["date"], type=r["type"],
                            description=r["description"], parts_used=r["parts_used"] or "", technician=r["technician"],
                            downtime_hours=float(r["downtime_hours"] or 0)) for r in rows[:limit]]
        return WorkOrderHistory(asset_id=asset_id, site=asset["site"], model=asset["model"],
                                install_date=asset["install_date"], total_on_record=len(rows), work_orders=orders)

    # ------------------------------------------------------------------ resources (application-controlled)
    @mcp.resource("kestrel://manuals", name="manual_index", mime_type="application/json",
                  description="Index of the product manuals available as kestrel://manuals/{name} resources.")
    def manual_index() -> str:
        return json.dumps([{"name": p.stem, "title": _title(p), "uri": f"kestrel://manuals/{p.stem}"}
                           for p in sorted(MANUALS_DIR.glob("*.md"))], indent=2)

    # A URI *template*: one registration serves every manual. The SDK's default resource
    # security already rejects parameters such as '../secrets'; the allow-list below makes the
    # server's contract explicit instead of relying on the filesystem to say no.
    @mcp.resource("kestrel://manuals/{name}", mime_type="text/markdown",
                  description="Full text (markdown) of one product manual, e.g. kestrel://manuals/kp250_pump_iom.")
    def manual(name: str) -> str:
        if name not in _manual_names():
            # ResourceNotFoundError -> JSON-RPC -32602 with this message; any *unexpected* exception
            # would reach the client as a generic error that hides internals.
            raise ResourceNotFoundError(f"No manual named {name!r}. Available: {', '.join(_manual_names())}.")
        return (MANUALS_DIR / f"{name}.md").read_text(encoding="utf-8")

    # ------------------------------------------------------------------ prompts (user-controlled)
    @mcp.prompt(title="Draft an RMA email")
    def draft_rma_email(order_id: str, sku: str, reason: str = "defective") -> list[dict]:
        """Draft a customer email that opens a return (RMA) for one order line, following policy RET-002."""
        policy = (POLICIES_DIR / "returns_rma_policy.md").read_text(encoding="utf-8")
        # The prompt embeds the policy as a resource so the host sends the authoritative text,
        # instead of trusting whatever the model remembers about Kestrel's return rules.
        return [
            {"role": "user", "content": {"type": "resource", "resource": {
                "uri": "kestrel://policies/returns_rma_policy", "mime_type": "text/markdown", "text": policy}}},
            {"role": "user", "content": (
                f"Draft a short, friendly email to the customer of order {order_id} about returning {sku} "
                f"(reason: {reason}). First call get_order_status to confirm the order, customer and quantity. "
                "Apply the attached returns policy exactly (fees, deadlines, RMA number format RMA-####) and "
                "leave a placeholder [RMA-####] for the number - do not invent one. Sign as Kestrel Customer Support.")},
        ]

    return mcp


mcp = create_server()


# --------------------------------------------------------------------------- self-test (default run)
async def self_test() -> None:
    from mcp import Client
    from mcp.types import TextContent

    from labkit import header, step

    header("Lab 01 - the kestrel-plant-ops MCP server (in-process self-test)")
    # Client(mcp) connects in-process: no subprocess, no JSON framing - ideal for unit tests.
    async with Client(mcp) as client:
        step(1, "Server identity and capabilities (from the connection handshake)")
        print(f"server={client.server_info.name} v{client.server_info.version}  protocol={client.protocol_version}")
        caps = client.server_capabilities
        print(f"capabilities: tools={caps.tools is not None} resources={caps.resources is not None} "
              f"prompts={caps.prompts is not None}")
        print(f"instructions: {client.instructions[:110]}...")

        step(2, "Tools (model-controlled): schemas generated from type hints")
        tools = (await client.list_tools()).tools
        for t in tools:
            params = ", ".join(f"{k}:{v.get('type')}" for k, v in t.input_schema.get("properties", {}).items())
            hint = "read-only" if t.annotations and t.annotations.read_only_hint else "may write"
            print(f"- {t.name}({params})  outputSchema={'yes' if t.output_schema else 'no'}  [{hint}]")
        limit = next(t for t in tools if t.name == "list_work_orders").input_schema["properties"]["limit"]
        print(f"  list_work_orders.limit schema: {json.dumps(limit)}")

        step(3, "Call every tool")
        stock = await client.call_tool("check_stock", {"sku": "ms-250"})
        report = stock.structured_content
        print(f"check_stock(MS-250): total_available={report['total_available']}  " + "  ".join(
            f"{w['warehouse']}={w['available']}" for w in report["warehouses"]))
        order = (await client.call_tool("get_order_status", {"order_id": "SO-10283"})).structured_content
        print(f"get_order_status(SO-10283): {order['customer_name']}, {order['status']}, lines="
              f"{[(l['sku'], l['qty']) for l in order['lines']]}")
        history = (await client.call_tool("list_work_orders", {"asset_id": "HF-KP250-03", "limit": 3})).structured_content
        print(f"list_work_orders(HF-KP250-03): {history['total_on_record']} on record, newest: " + "; ".join(
            f"{w['date']} {w['type']} {w['description'][:40]}" for w in history["work_orders"]))
        text_block = stock.content[0]
        assert isinstance(text_block, TextContent)
        print(f"(the same result as text for the model: {text_block.text[:70].replace(chr(10), ' ')}...)")

        step(4, "Tool errors are results, not exceptions")
        bad = await client.call_tool("check_stock", {"sku": "MS-999"})
        print(f"check_stock(MS-999): is_error={bad.is_error}  text={bad.content[0].text!r}")
        invalid = await client.call_tool("list_work_orders", {"asset_id": "HF-KP250-03", "limit": 500})
        detail = " | ".join(line.strip() for line in invalid.content[0].text.splitlines()[1:3])
        print(f"list_work_orders(limit=500): is_error={invalid.is_error}  validation: {detail!r}")

        step(5, "Resources (application-controlled): a static resource and a URI template")
        for r in (await client.list_resources()).resources:
            print(f"- resource {r.uri}  ({r.mime_type})")
        for tpl in (await client.list_resource_templates()).resource_templates:
            print(f"- template {tpl.uri_template}  ({tpl.mime_type})")
        index = json.loads((await client.read_resource("kestrel://manuals")).contents[0].text)
        print(f"manual index: {[m['name'] for m in index]}")
        seal = (await client.read_resource("kestrel://manuals/mechanical_seal_guide")).contents[0]
        print(f"kestrel://manuals/mechanical_seal_guide -> {seal.mime_type}, {len(seal.text)} chars: "
              f"{seal.text.splitlines()[0]!r}")

        step(6, "Prompts (user-controlled templates)")
        for p in (await client.list_prompts()).prompts:
            args = ", ".join(f"{a.name}{'' if a.required else '?'}" for a in p.arguments or [])
            print(f"- prompt {p.name}({args}): {p.description}")
        rendered = await client.get_prompt("draft_rma_email", {"order_id": "SO-10283", "sku": "MS-250"})
        for m in rendered.messages:
            kind = m.content.type
            preview = m.content.resource.uri if kind == "resource" else m.content.text[:90]
            print(f"  {m.role}/{kind}: {preview}")

    print("\nSelf-test passed: 3 tools, 1 resource + 1 template, 1 prompt.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--serve", action="store_true", help="run as a stdio MCP server")
    args = parser.parse_args()
    if args.serve:
        # stdio transport: JSON-RPC on stdin/stdout. Logs must go to stderr, never stdout.
        mcp.run(transport="stdio")
    else:
        asyncio.run(self_test())


if __name__ == "__main__":
    main()
