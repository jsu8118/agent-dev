"""Capstone reference - self-test of the kestrel-copilot MCP server (in-process, no subprocess).

Lists the server's tools, resource and prompt, then calls each one the way Claude Code would.
To use the server from Claude Code instead:

    claude mcp add kestrel-copilot -- python day7_capstone/reference/copilot/mcp_server.py --serve
"""
# test: expect=MCP self-test passed

from __future__ import annotations

import asyncio
import json

from mcp import Client

from copilot.mcp_server import mcp
from labkit import header, step


async def main() -> None:
    header("kestrel-copilot MCP server - self-test")
    async with Client(mcp) as client:
        step(1, "What the server offers")
        print(f"server: {client.server_info.name} v{client.server_info.version}")
        tools = (await client.list_tools()).tools
        for t in tools:
            read_only = bool(t.annotations and t.annotations.read_only_hint)
            print(f"- tool {t.name}({', '.join(t.input_schema.get('properties', {}))})  read_only={read_only}")
        for r in (await client.list_resources()).resources:
            print(f"- resource {r.uri}")
        for p in (await client.list_prompts()).prompts:
            print(f"- prompt {p.name}({', '.join(a.name for a in p.arguments or [])})")
        assert {t.name for t in tools} == {"preview_triage", "quality_hold_check"}

        step(2, "quality_hold_check: which units of an order carry a held lot?")
        check = (await client.call_tool("quality_hold_check", {"order_id": "SO-10272"})).structured_content
        print(f"{check['checked']}: {len(check['held_units'])}/{check['units_checked']} units held")
        for u in check["held_units"]:
            print(f"  {u['serial_number']} {u['sku']} built {u['build_date']}: {u['part']} lot {u['lot']} ({u['incident']})")
        assert len(check["held_units"]) == 3
        clean = (await client.call_tool("quality_hold_check", {"serial_number": "KP400-2512-0001"})).structured_content
        print(f"{clean['checked']}: {len(clean['held_units'])} held units")
        bad = await client.call_tool("quality_hold_check", {"order_id": "10272"})
        print(f"bad input -> is_error={bad.is_error}: {bad.content[0].text}")
        assert bad.is_error

        step(3, "preview_triage: how would the copilot route these emails? (nothing is sent or written)")
        samples = [
            ("aisha.karim@harborfoods.example", "Ammonia leak", "Ammonia is leaking near the KP-250 in our cold store."),
            ("ap@bluewater-utilitles.example", "Vendor update", "Please confirm the remittance address on file."),
            ("aisha.karim@harborfoods.example", "Return", "We over-ordered MS-250 seal kits on SO-10283; can we return 4?"),
        ]
        for sender, subject, body in samples:
            preview = (await client.call_tool("preview_triage", {"from_email": sender, "subject": subject,
                                                                 "body": body})).structured_content
            print(f"{subject!r:16} -> {preview['route']:<8} reasons={preview['reasons']}")
        assert preview["route"] == "agent"

        step(4, "Resource and prompt")
        holds = json.loads((await client.read_resource("kestrel://quality-holds")).contents[0].text)
        print(f"kestrel://quality-holds -> {list(holds)}")
        prompt = await client.get_prompt("field_evidence_summary", {"lot": "PS-2608-B"})
        for m in prompt.messages:
            preview = m.content.resource.uri if m.content.type == "resource" else m.content.text[:100] + "..."
            print(f"  {m.role}/{m.content.type}: {preview}")

    print("\nMCP self-test passed: 2 read-only tools, 1 resource, 1 prompt.")


if __name__ == "__main__":
    asyncio.run(main())
