"""Solution to exercise 9 - add a resource template (and argument completion) to the plant-ops server.

Objective
    Expose each monitored pump as `kestrel://assets/{asset_id}`: the asset record plus its five most
    recent work orders, as JSON. Hosts can list the template, autocomplete the asset ID, and attach the
    resource to a conversation - without the model having to call a tool.

Concepts
    Resource templates (RFC 6570 URI templates), MIME types, ResourceNotFoundError vs crashing, the SDK's
    default path-safety for template parameters, `completion/complete` for template arguments.

Run
    python day5_mcp_agent_sdk/solutions/ex09_asset_resource.py

What to observe
    * The template shows up in resources/templates/list, not resources/list: there is no finite list.
    * A bad ID is a JSON-RPC error with a helpful message; a traversal attempt never reaches our code.
    * Completion narrows asset IDs as the user types - a UX feature for the *user-facing* host.
"""

# test: expect=All checks passed

from __future__ import annotations

import asyncio
import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "labs"))

from mcp import Client, MCPError                                     # noqa: E402
from mcp.server.mcpserver.exceptions import ResourceNotFoundError    # noqa: E402
from mcp.types import Completion, ResourceTemplateReference          # noqa: E402

from _mcp_common import load_lab                                     # noqa: E402

lab01 = load_lab("01_mcp_server")
TEMPLATE = "kestrel://assets/{asset_id}"


def build_server():
    server = lab01.create_server()

    @server.resource(TEMPLATE, name="asset", mime_type="application/json",
                     description="One monitored pump: asset record (site, model, install date, criticality, notes) "
                                 "and its five most recent maintenance work orders.")
    def asset(asset_id: str) -> str:
        assets = lab01._assets()
        record = assets.get(asset_id.upper())
        if record is None:
            raise ResourceNotFoundError(f"Unknown asset {asset_id!r}. Valid IDs: {', '.join(sorted(assets))}.")
        with lab01.WORK_ORDERS_CSV.open(encoding="utf-8", newline="") as fh:
            history = sorted((r for r in csv.DictReader(fh) if r["asset_id"] == record["asset_id"]),
                             key=lambda r: r["date"], reverse=True)
        return json.dumps({"asset": record, "recent_work_orders": history[:5], "work_orders_on_record": len(history)},
                          indent=2)

    @server.completion()
    async def complete(ref, argument, context):
        if isinstance(ref, ResourceTemplateReference) and ref.uri == TEMPLATE and argument.name == "asset_id":
            matches = [a for a in sorted(lab01._assets()) if a.startswith(argument.value.upper())]
            return Completion(values=matches[:100], total=len(matches), has_more=len(matches) > 100)
        return None     # no suggestions for other arguments

    return server


async def run() -> None:
    server = build_server()
    checks = []
    async with Client(server) as client:
        templates = (await client.list_resource_templates()).resource_templates
        mine = next(t for t in templates if t.uri_template == TEMPLATE)
        print(f"template: {mine.uri_template} ({mine.mime_type}) - {mine.description[:70]}...")
        checks.append(mine.mime_type == "application/json")

        body = json.loads((await client.read_resource("kestrel://assets/HF-KP250-03")).contents[0].text)
        print(f"HF-KP250-03: {body['asset']['site']}, {body['asset']['model']}, installed {body['asset']['install_date']}; "
              f"{body['work_orders_on_record']} work orders, newest {body['recent_work_orders'][0]['date']}")
        checks.append(body["asset"]["asset_id"] == "HF-KP250-03" and len(body["recent_work_orders"]) <= 5)

        for uri in ("kestrel://assets/XX-KP999-01", "kestrel://assets/..", "kestrel://assets/../../secrets"):
            try:
                await client.read_resource(uri)
                print(f"{uri}: UNEXPECTEDLY READ")
                checks.append(False)
            except MCPError as exc:
                print(f"{uri}: refused (code {exc.error.code}) {exc.error.message[:90]}")
                checks.append(True)

        result = await client.complete(ref=ResourceTemplateReference(type="ref/resource", uri=TEMPLATE),
                                       argument={"name": "asset_id", "value": "hf"})
        print(f"completion for 'hf': {result.completion.values}")
        checks.append(result.completion.values == ["HF-KP100-04", "HF-KP250-01", "HF-KP250-02", "HF-KP250-03"])

    print(f"\n{'All checks passed' if all(checks) else 'SOME CHECKS FAILED'} ({sum(checks)}/{len(checks)}).")


if __name__ == "__main__":
    asyncio.run(run())
