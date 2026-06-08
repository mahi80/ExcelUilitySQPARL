"""Offline test for M4 — config-driven metric fill + matching + injection safety.
Run: python tests/test_offline_m4.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agent"))

from metrics import fill_metric, match_metric, param_literal  # noqa: E402
from validators import validate  # noqa: E402

_passed = _failed = 0


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [PASS] {name}")
    else:
        _failed += 1; print(f"  [FAIL] {name} — {detail}")


REVENUE = {
    "id": "m_1", "name": "revenue by region", "synonyms": ["regional revenue"],
    "sql_template": "SELECT region, SUM(net_value) AS revenue FROM order_header "
                    "GROUP BY region ORDER BY revenue DESC LIMIT {{n}}",
    "params": [{"name": "n", "type": "int", "default": "10"}],
}


def main():
    print("matching")
    check("matches by name", match_metric("show me revenue by region", [REVENUE]) is REVENUE)
    check("matches by synonym", match_metric("regional revenue please", [REVENUE]) is REVENUE)
    check("no match on unrelated q", match_metric("how many products?", [REVENUE]) is None)
    check("short names (<4) ignored", match_metric("the cat", [{"name": "cat"}]) is None)

    print("\nparam fill")
    sql, err = fill_metric(REVENUE, "top 5 regions by revenue by region")
    check("number extracted from question", err is None and "LIMIT 5" in sql, sql)
    sql, err = fill_metric(REVENUE, "revenue by region")
    check("default used when no number", "LIMIT 10" in sql, sql)

    print("\ninjection containment (text param)")
    evil = {"id": "m_2", "name": "lookup", "sql_template": "SELECT * FROM customer WHERE name = {{q}}",
            "params": [{"name": "q", "type": "text", "default": "x'; DROP TABLE customer; --"}]}
    sql, err = fill_metric(evil, "lookup")
    check("single quote escaped (doubled)", "''" in sql, sql)
    r = validate(sql, {"customer"}, {"customer": {"name"}})
    check("filled SQL still a single safe SELECT", r.ok, r.error or "")
    # multi-statement attempt must be caught if it ever broke out
    r2 = validate("SELECT 1; DROP TABLE customer", {"customer"}, {"customer": {"name"}})
    check("multi-statement rejected by validator", not r2.ok)

    print("\nliteral typing")
    check("int literal", param_literal("3,000", "int") == "3000")
    check("text literal quoted", param_literal("Salem", "text") == "'Salem'")

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
