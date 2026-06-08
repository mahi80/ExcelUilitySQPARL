"""Offline M1 smoke test — no Docker / DB / LLM required.

Exercises the generalisation core end-to-end:
  profile a real workbook → build the agent artifact → run the dynamic-allowlist
  SQL validator over good and adversarial queries.

Run:  python tests/test_offline_m1.py ["path/to/workbook.xlsx"]
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "onboarding"))
sys.path.insert(0, str(ROOT / "agent"))

from profiler import profile_workbook            # noqa: E402
from generators import build_artifact            # noqa: E402
from validators import validate                  # noqa: E402

DEFAULT_XLSX = r"C:\Users\msb80\OneDrive\Documents\aelum\POC_Sample_Data 1.xlsx"

_passed = _failed = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global _passed, _failed
    mark = "PASS" if cond else "FAIL"
    if cond:
        _passed += 1
    else:
        _failed += 1
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not cond else ""))


def main() -> int:
    xlsx = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_XLSX

    print("1. Profile + build artifact")
    profile = profile_workbook(xlsx)
    for t in profile["tables"]:
        if t["name"] == "readme":
            t["include"] = False            # simulate the human review step
    art = build_artifact(profile, "ds_main")
    kt = set(art["known_tables"])
    kc = {t: set(cols) for t, cols in art["known_columns"].items()}

    check("schema context generated", len(art["schema_context"]) > 500)
    check("expected tables present", {"product", "capacity", "plant", "customer"} <= kt,
          f"got {sorted(kt)}")
    check("README excluded from allowlist", "readme" not in kt)
    check("product.sku in allowlist", "sku" in kc.get("product", set()))
    check("synth _row_id for customer_offers", "_row_id" in kc.get("customer_offers", set()))
    check("leading-zero code kept as text",
          any(c["name"] == "sap_matnr" and c["type"] == "text"
              for t in art["tables"] if t["name"] == "product" for c in t["columns"]))

    print("\n2. Validator — good query passes (+ auto-LIMIT)")
    good = ("SELECT c.sku, SUM(c.available_to_use) AS qty "
            "FROM capacity c JOIN plant p ON p.external_id = c.plant "
            "WHERE p.plant_code = 'SAL' GROUP BY c.sku")
    r = validate(good, kt, kc)
    check("valid join query accepted", r.ok, r.error or "")
    check("auto-LIMIT applied", bool(r.notes and any("LIMIT" in n for n in r.notes)))

    print("\n3. Validator — adversarial inputs rejected")
    r = validate("SELECT p.bogus_col FROM product p", kt, kc)
    check("hallucinated column rejected", (not r.ok) and "unknown column" in (r.error or ""), r.error or "")
    r = validate("SELECT * FROM nonexistent_table", kt, kc)
    check("unknown table rejected", (not r.ok) and "unknown table" in (r.error or ""), r.error or "")
    r = validate("DELETE FROM product", kt, kc)
    check("DELETE rejected", not r.ok, r.error or "")
    r = validate("SELECT 1; DROP TABLE product", kt, kc)
    check("multi-statement / DROP rejected", not r.ok, r.error or "")
    r = validate("SELECT pr.sku, pr.name FROM product pr", kt, kc)
    check("real columns accepted", r.ok, r.error or "")

    print(f"\n{_passed} passed, {_failed} failed")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
