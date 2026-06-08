"""Offline tests for M2 P2 (data dictionary) + P3 (multi-file merge).

No Docker/DB needed — builds tiny workbooks with openpyxl and drives the
profiler/generators directly.  Run: python tests/test_offline_m2m3.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import openpyxl

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "onboarding"))

from generators import build_artifact, generate_schema_context  # noqa: E402
from profiler import merge_profiles, profile_frames, profile_workbook  # noqa: E402

_passed = _failed = 0
TMP = Path(tempfile.mkdtemp())


def check(name, cond, detail=""):
    global _passed, _failed
    if cond:
        _passed += 1; print(f"  [PASS] {name}")
    else:
        _failed += 1; print(f"  [FAIL] {name} — {detail}")


def _wb(path, sheets: dict):
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in sheets.items():
        ws = wb.create_sheet(name)
        for r in rows:
            ws.append(r)
    wb.save(path)


def test_data_dictionary():
    print("P2 — data dictionary import")
    p = TMP / "books_dict.xlsx"
    _wb(p, {
        "authors": [["author_id", "name"], ["A1", "Le Guin"], ["A2", "Murakami"]],
        "books": [["book_id", "title", "author_id", "price"],
                  ["B1", "Earthsea", "A1", 12.5], ["B2", "1Q84", "A2", 19.0]],
        "data_dictionary": [
            ["table", "column", "business_name", "synonyms", "unit", "description"],
            ["books", "price", "Retail price", "cost, msrp", "USD", "List price per copy"],
            ["books", "", "", "", "", "The catalog of books for sale"],
            ["term", "definition", "", "", "", ""],         # header-ish row ignored (no table)
        ],
    })
    prof = profile_workbook(p)
    names = [t["name"] for t in prof["tables"]]
    check("data_dictionary excluded from tables", "data_dictionary" not in names, names)
    books = next(t for t in prof["tables"] if t["name"] == "books")
    price = next(c for c in books["columns"] if c["name"] == "price")
    check("business_name applied from dict", price["business_name"] == "Retail price", price)
    check("synonyms parsed from dict", price["synonyms"] == ["cost", "msrp"], price["synonyms"])
    check("unit applied from dict", price["unit"] == "USD", price)
    check("table description applied", books.get("description") == "The catalog of books for sale",
          books.get("description"))
    ctx = generate_schema_context(prof)
    check("schema_context carries business name", "Retail price" in ctx)
    check("schema_context carries synonyms", "also known as" in ctx and "msrp" in ctx)
    art = build_artifact(prof, "ds_x")
    bcols = {c["name"]: c for t in art["tables"] if t["name"] == "books" for c in t["columns"]}
    check("artifact carries dict fields", bcols["price"]["business_name"] == "Retail price")


def test_multifile_merge():
    print("\nP3 — multi-file merge (collision prefix + cross-file FK)")
    # file A: customers (PK customer_id)
    a = TMP / "crm.xlsx"
    _wb(a, {
        "customers": [["customer_id", "name"], ["C1", "Acme"], ["C2", "Globex"]],
        "shared": [["k", "v"], ["x", 1]],
    })
    # file B: orders referencing customers.customer_id  +  a colliding 'shared' sheet
    b = TMP / "sales.xlsx"
    _wb(b, {
        "orders": [["order_id", "customer_id", "amount"],
                   ["O1", "C1", 100], ["O2", "C2", 200], ["O3", "C1", 50]],
        "shared": [["k", "v"], ["y", 2]],
    })
    pfa, pfb = profile_frames(a), profile_frames(b)
    for pf, fn in ((pfa, "crm.xlsx"), (pfb, "sales.xlsx")):
        pf["source_file"] = fn
        for t in pf["tables"]:
            t["source_file"] = fn
    prof = merge_profiles([pfa, pfb], "merged")
    names = [t["name"] for t in prof["tables"]]
    check("colliding 'shared' prefixed by file", "crm__shared" in names and "sales__shared" in names, names)
    check("non-colliding tables keep clean names", "customers" in names and "orders" in names, names)
    orders = next(t for t in prof["tables"] if t["name"] == "orders")
    fks = [(fk["column"], fk["ref_table"]) for fk in orders["fks"]]
    check("cross-file FK orders.customer_id -> customers", ("customer_id", "customers") in fks, fks)


if __name__ == "__main__":
    test_data_dictionary()
    test_multifile_merge()
    print(f"\n{_passed} passed, {_failed} failed")
    sys.exit(1 if _failed else 0)
