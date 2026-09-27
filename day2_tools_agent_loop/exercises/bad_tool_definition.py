"""Exercise 12 starter - a badly designed tool (do NOT copy this into production).

This is a composite of real first drafts: one generic tool that wraps the database, written in an afternoon
for the customer-facing support bot.  It "works" in a demo.  Your job (exercises/README.md, exercise 12) is
to list everything wrong with it and design the replacement.

Run
    python day2_tools_agent_loop/exercises/bad_tool_definition.py

It prints the definition, what one call returns, and the TODO list.  No API calls are made.
"""

from __future__ import annotations

import json

from labkit.data import ops_db

BAD_TOOL = {
    "name": "db",
    "description": "Database access for the support bot.",
    "input_schema": {
        "type": "object",
        "properties": {
            "q": {"type": "string"},
            "cust": {"type": "string", "description": "customer id of the person asking"},
            "mode": {"type": "string", "description": "order, invoice, orders_for_customer or refund"},
            "amt": {"type": "number"},
            "all_columns": {"type": "boolean"},
        },
    },
}


def db(q: str = "", cust: str | None = None, mode: str = "order", amt: float | None = None,
       all_columns: bool = True) -> str:
    conn = ops_db()
    if mode == "order":
        rows = conn.execute("SELECT * FROM orders o JOIN customers c USING (customer_id) "
                            "LEFT JOIN shipments s USING (order_id) WHERE order_id = ?", (q,)).fetchall()
        return str([dict(r) for r in rows]) if rows else "ERROR"
    if mode == "invoice":
        rows = conn.execute("SELECT * FROM invoices WHERE invoice_id = ?", (q,)).fetchall()
        return str([dict(r) for r in rows]) if rows else "ERROR"
    if mode == "orders_for_customer":
        rows = conn.execute("SELECT * FROM orders WHERE customer_id = ?", (cust,)).fetchall()
        return str([dict(r) for r in rows])
    if mode == "refund":
        # The real draft INSERTed into `refunds` here - no limit check, no RMA check, no approval.
        return f"Refund of {amt} issued for {q}"
    return "ERROR"


def main() -> None:
    print("The tool definition the model sees:")
    print(json.dumps(BAD_TOOL, indent=2))
    sample = db("SO-10303")
    print(f"\ndb(q='SO-10303') returns {len(sample):,} characters of Python repr (not JSON), e.g.:\n  {sample[:300]}...")
    everything = db(cust="C-1005", mode="orders_for_customer")
    print(f"\ndb(cust='C-1005', mode='orders_for_customer') returns {len(everything):,} characters.")
    print(f"db(q='SO-99999') returns {db('SO-99999')!r}")
    print(f"db(q='RMA-7001', mode='refund', amt=9188.5) returns {db('RMA-7001', mode='refund', amt=9188.5)!r}")
    print("\nTODO (exercise 12):")
    print("  1. List every problem with the definition AND the implementation (aim for 10+).")
    print("  2. Design the replacement toolset: names, descriptions, schemas (strict), results, errors.")
    print("  3. Decide where `refund` belongs and who may approve it.")
    print("  Then compare with solutions/ex12_fix_bad_tool.py and solutions/README.md.")


if __name__ == "__main__":
    main()
