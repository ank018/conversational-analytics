"""
Stage 1 - build the Olist warehouse and profile its traps.

Loads the Olist CSVs into a DuckDB file, then runs an integrity report.

The integrity report is the point. This project's central claim is about
answers that are wrong without looking wrong, and on this schema those come
from a small number of structural traps: one order has many items AND many
payment rows, so joining both fans out and silently multiplies revenue; one
zip prefix has many geolocation rows; several date columns are nullable in
ways that quietly drop rows from an inner join.

Measure them now, before writing a single gold question. Every number this
prints is a candidate glossary entry or a candidate hard question.

Requires: pip install duckdb

Place the unzipped Kaggle CSVs in data/raw/ then run from the repo root:
    python src/01_build_warehouse.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb

RAW = Path("data/raw")
MB = 1024 ** 2

# Source filename -> warehouse table name. The olist_ prefix and _dataset
# suffix are dropped: they carry no information and every table would share
# them, which is noise in a schema the model has to read.
TABLE_MAP = {
    "olist_customers_dataset.csv": "customers",
    "olist_geolocation_dataset.csv": "geolocation",
    "olist_order_items_dataset.csv": "order_items",
    "olist_order_payments_dataset.csv": "order_payments",
    "olist_order_reviews_dataset.csv": "order_reviews",
    "olist_orders_dataset.csv": "orders",
    "olist_products_dataset.csv": "products",
    "olist_sellers_dataset.csv": "sellers",
    "product_category_name_translation.csv": "product_category_translation",
    "olist_marketing_qualified_leads_dataset.csv": "marketing_qualified_leads",
    "olist_closed_deals_dataset.csv": "closed_deals",
}

# Candidate keys to test for uniqueness. Tested, not assumed - geolocation is
# deliberately absent because it has no key, and that is itself a finding.
CANDIDATE_KEYS = {
    "customers": ["customer_id"],
    "orders": ["order_id"],
    "products": ["product_id"],
    "sellers": ["seller_id"],
    "order_items": ["order_id", "order_item_id"],
    "order_payments": ["order_id", "payment_sequential"],
    "order_reviews": ["review_id"],
    "product_category_translation": ["product_category_name"],
}

# child table, child column, parent table, parent column
REFERENCES = [
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("order_items", "seller_id", "sellers", "seller_id"),
    ("order_payments", "order_id", "orders", "order_id"),
    ("order_reviews", "order_id", "orders", "order_id"),
    ("products", "product_category_name", "product_category_translation",
     "product_category_name"),
]


def rule(text: str) -> None:
    print()
    print("=" * 72)
    print(text)
    print("=" * 72)


def load(con: duckdb.DuckDBPyConnection) -> list[str]:
    rule("LOADING")
    loaded = []
    for filename, table in TABLE_MAP.items():
        path = RAW / filename
        if not path.exists():
            print(f"  {table:<30} MISSING ({filename})")
            continue
        con.execute(
            f'CREATE OR REPLACE TABLE "{table}" AS '
            f"SELECT * FROM read_csv_auto('{path.as_posix()}', header=true)"
        )
        n = con.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
        c = len(con.execute(f'DESCRIBE "{table}"').fetchall())
        print(f"  {table:<30} {n:>10,} rows   {c:>3} cols")
        loaded.append(table)
    return loaded


def schema_report(con: duckdb.DuckDBPyConnection, tables: list[str]) -> None:
    rule("SCHEMA AND PROMPT COST")
    parts, total_cols = [], 0
    for table in tables:
        cols = con.execute(f'DESCRIBE "{table}"').fetchall()
        total_cols += len(cols)
        body = ",\n".join(f"  {c[0]} {c[1]}" for c in cols)
        parts.append(f"CREATE TABLE {table} (\n{body}\n);")
    ddl = "\n\n".join(parts)

    print(f"tables:                  {len(tables)}")
    print(f"columns (total):         {total_cols}")
    print(f"DDL characters:          {len(ddl):,}")
    print(f"estimated tokens (c/4):  {len(ddl) / 4:,.0f}")

    Path("reports").mkdir(exist_ok=True)
    Path("reports/schema_ddl.sql").write_text(ddl, encoding="utf-8")
    print("full DDL -> reports/schema_ddl.sql")


def key_report(con: duckdb.DuckDBPyConnection, tables: list[str]) -> None:
    rule("CANDIDATE KEY UNIQUENESS")
    print("A key that is not unique is a fan-out waiting to happen.\n")
    for table, keys in CANDIDATE_KEYS.items():
        if table not in tables:
            continue
        cols = ", ".join(f'"{k}"' for k in keys)
        n, d = con.execute(
            f'SELECT count(*), count(DISTINCT ({cols})) FROM "{table}"'
        ).fetchone()
        verdict = "UNIQUE" if n == d else f"NOT UNIQUE ({n - d:,} dupes)"
        label = f"{table}({', '.join(keys)})"
        print(f"  {label:<52} {verdict}")

    if "geolocation" in tables:
        n, d = con.execute(
            "SELECT count(*), count(DISTINCT geolocation_zip_code_prefix) "
            "FROM geolocation"
        ).fetchone()
        print(f"\n  geolocation: {n:,} rows across {d:,} zip prefixes "
              f"({n / d:.1f} rows per prefix)")


def fanout_report(con: duckdb.DuckDBPyConnection, tables: list[str]) -> None:
    rule("FAN-OUT PER ORDER")
    for table in ["order_items", "order_payments", "order_reviews"]:
        if table not in tables:
            continue
        avg, mx, n = con.execute(
            f"SELECT avg(c), max(c), count(*) FROM "
            f'(SELECT order_id, count(*) AS c FROM "{table}" GROUP BY 1)'
        ).fetchone()
        print(f"  {table:<20} {avg:>5.2f} rows/order   max {mx:>3}   "
              f"{n:,} distinct orders")


def revenue_report(con: duckdb.DuckDBPyConnection, tables: list[str]) -> None:
    if not {"order_items", "order_payments"} <= set(tables):
        return
    rule("REVENUE: FOUR DEFENSIBLE-LOOKING ANSWERS, ONE WRONG")

    items = con.execute("SELECT sum(price) FROM order_items").fetchone()[0]
    items_freight = con.execute(
        "SELECT sum(price + freight_value) FROM order_items").fetchone()[0]
    payments = con.execute(
        "SELECT sum(payment_value) FROM order_payments").fetchone()[0]
    naive = con.execute(
        "SELECT sum(oi.price) FROM order_items oi "
        "JOIN order_payments op ON oi.order_id = op.order_id"
    ).fetchone()[0]

    print(f"  sum(price)                       {items:>18,.2f}   item revenue")
    print(f"  sum(price + freight_value)       {items_freight:>18,.2f}   "
          f"incl. freight")
    print(f"  sum(payment_value)               {payments:>18,.2f}   "
          f"what customers paid")
    print(f"  items JOIN payments, sum(price)  {naive:>18,.2f}   "
          f"** INFLATED **")
    print()
    print(f"  the naive join overstates item revenue by "
          f"{(naive / items - 1) * 100:.1f}%")
    print(f"  payments vs price+freight differ by "
          f"{(payments / items_freight - 1) * 100:.2f}%")
    print()
    print("  All four queries run without error. Three are defensible answers")
    print("  to 'what was total revenue'. The fourth is simply wrong.")


def status_and_dates(con: duckdb.DuckDBPyConnection, tables: list[str]) -> None:
    if "orders" not in tables:
        return
    rule("ORDER STATUS")
    rows = con.execute(
        "SELECT order_status, count(*) AS n FROM orders GROUP BY 1 ORDER BY 2 DESC"
    ).fetchall()
    total = sum(r[1] for r in rows)
    for status, n in rows:
        print(f"  {status:<16} {n:>8,}   {n / total:>6.2%}")
    print("\n  Does 'sales' include cancelled and unavailable orders?")
    print("  The question will not say. The glossary must.")

    rule("ORDER DATE COLUMNS")
    # Detect by declared type, not by name. order_approved_at ends in neither
    # _date nor timestamp; a name-based rule drops it silently.
    described = con.execute("DESCRIBE orders").fetchall()
    date_cols = [c[0] for c in described
                 if any(t in str(c[1]).upper() for t in ("DATE", "TIMESTAMP"))]
    if not date_cols:
        print("  no DATE/TIMESTAMP columns detected - the CSV sniffer read them")
        print("  as text. Check reports/schema_ddl.sql before trusting anything.")
    for col in date_cols:
        nulls, lo, hi = con.execute(
            f'SELECT sum(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END), '
            f'min("{col}"), max("{col}") FROM orders'
        ).fetchone()
        print(f"  {col:<34} nulls {nulls:>6,}   {lo}  ->  {hi}")
    print("\n  Five dates. 'Delivery time' is at least four different metrics.")


def referential_report(con: duckdb.DuckDBPyConnection, tables: list[str]) -> None:
    rule("REFERENTIAL INTEGRITY")
    print("Orphans break inner joins silently by dropping rows.\n")
    for child, ccol, parent, pcol in REFERENCES:
        if child not in tables or parent not in tables:
            continue
        orphans = con.execute(
            f'SELECT count(*) FROM "{child}" c '
            f'WHERE c."{ccol}" IS NOT NULL AND NOT EXISTS '
            f'(SELECT 1 FROM "{parent}" p WHERE p."{pcol}" = c."{ccol}")'
        ).fetchone()[0]
        nulls = con.execute(
            f'SELECT count(*) FROM "{child}" WHERE "{ccol}" IS NULL'
        ).fetchone()[0]
        flag = "ok" if orphans == 0 else f"{orphans:,} ORPHANS"
        label = f"{child}.{ccol}"
        print(f"  {label:<40} -> {parent:<30} {flag:<16} nulls {nulls:,}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("data/olist.duckdb"))
    args = ap.parse_args()

    if not RAW.exists():
        raise SystemExit(f"{RAW.resolve()} not found - unzip the Kaggle CSVs there")

    args.db.parent.mkdir(parents=True, exist_ok=True)
    if args.db.exists():
        args.db.unlink()

    con = duckdb.connect(str(args.db))
    tables = load(con)
    if not tables:
        raise SystemExit("no expected CSVs found in data/raw")

    schema_report(con, tables)
    key_report(con, tables)
    fanout_report(con, tables)
    revenue_report(con, tables)
    status_and_dates(con, tables)
    referential_report(con, tables)
    con.close()

    rule("DONE")
    print(f"warehouse: {args.db}  ({args.db.stat().st_size / MB:,.1f} MB)")


if __name__ == "__main__":
    main()
