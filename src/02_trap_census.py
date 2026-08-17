"""
Stage 2 - trap census.

Stage 1 found anomalies. This stage pins each one down to a number, because
every one of them is about to become either a glossary entry or a gold
question, and a gold answer built on a guess is worse than no gold answer.

Each probe prints the competing results side by side. Where two reasonable
queries disagree, the size of the disagreement is the finding.

Run from the repo root:
    python src/02_trap_census.py
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb


def rule(n: int, title: str) -> None:
    print()
    print("=" * 72)
    print(f"{n}. {title}")
    print("=" * 72)


def q(con, sql: str):
    return con.execute(sql).fetchall()


def one(con, sql: str):
    return con.execute(sql).fetchone()[0]


def probe_coverage(con) -> None:
    rule(1, "ORDER COVERAGE - which orders exist in which tables")
    total = one(con, "SELECT count(*) FROM orders")
    for child in ["order_items", "order_payments", "order_reviews"]:
        missing = one(con, f"""
            SELECT count(*) FROM orders o
            WHERE NOT EXISTS (SELECT 1 FROM {child} c WHERE c.order_id = o.order_id)
        """)
        print(f"  orders with no row in {child:<16} {missing:>6,}  "
              f"({missing / total:.2%})")
    print(f"\n  total orders: {total:,}")
    print("  An inner join to any of these silently changes the denominator.")

    print("\n  Status breakdown of orders with no items:")
    for status, n in q(con, """
        SELECT o.order_status, count(*) AS n FROM orders o
        WHERE NOT EXISTS (SELECT 1 FROM order_items i WHERE i.order_id = o.order_id)
        GROUP BY 1 ORDER BY 2 DESC
    """):
        print(f"    {status:<16} {n:>6,}")


def probe_status_dates(con) -> None:
    rule(2, "STATUS vs DELIVERY DATE - do they agree?")
    print("  status           orders   no delivery date   has date but not delivered")
    for status, n, nulls in q(con, """
        SELECT order_status, count(*),
               sum(CASE WHEN order_delivered_customer_date IS NULL THEN 1 ELSE 0 END)
        FROM orders GROUP BY 1 ORDER BY 2 DESC
    """):
        odd = ""
        if status == "delivered" and nulls:
            odd = f"<- {nulls} delivered with no date"
        print(f"  {status:<16} {n:>7,}   {nulls:>16,}   {odd}")

    weird = one(con, """
        SELECT count(*) FROM orders
        WHERE order_status <> 'delivered' AND order_delivered_customer_date IS NOT NULL
    """)
    print(f"\n  not delivered but carrying a delivery date: {weird:,}")


def probe_reviews(con) -> None:
    rule(3, "REVIEW IDS - what does a duplicate mean?")
    rows = one(con, "SELECT count(*) FROM order_reviews")
    ids = one(con, "SELECT count(DISTINCT review_id) FROM order_reviews")
    orders = one(con, "SELECT count(DISTINCT order_id) FROM order_reviews")
    print(f"  review rows:        {rows:,}")
    print(f"  distinct review_id: {ids:,}")
    print(f"  distinct order_id:  {orders:,}")

    spanning = one(con, """
        SELECT count(*) FROM (
            SELECT review_id FROM order_reviews
            GROUP BY 1 HAVING count(DISTINCT order_id) > 1
        )
    """)
    print(f"\n  review_ids attached to more than one order: {spanning:,}")
    print("  'How many reviews' has two answers depending on the count target.")

    print("\n  Score distribution (rows, not distinct reviews):")
    for score, n in q(con, """
        SELECT review_score, count(*) FROM order_reviews GROUP BY 1 ORDER BY 1
    """):
        print(f"    {score}  {n:>7,}")


def probe_payments(con) -> None:
    rule(4, "PAYMENTS - why they exceed price plus freight")
    print("  type            rows      value        share")
    total_val = one(con, "SELECT sum(payment_value) FROM order_payments")
    for ptype, n, val in q(con, """
        SELECT payment_type, count(*), sum(payment_value)
        FROM order_payments GROUP BY 1 ORDER BY 3 DESC
    """):
        print(f"  {ptype:<14} {n:>7,}  {val:>14,.2f}  {val / total_val:>7.2%}")

    print("\n  Instalments:")
    for inst, n in q(con, """
        SELECT payment_installments, count(*) FROM order_payments
        GROUP BY 1 ORDER BY 1 LIMIT 13
    """):
        print(f"    {inst:>3}x  {n:>7,}")

    rule(5, "PER-ORDER RECONCILIATION - items vs payments")
    row = con.execute("""
        WITH i AS (SELECT order_id, sum(price + freight_value) AS v
                   FROM order_items GROUP BY 1),
             p AS (SELECT order_id, sum(payment_value) AS v
                   FROM order_payments GROUP BY 1)
        SELECT count(*),
               sum(CASE WHEN abs(i.v - p.v) < 0.01 THEN 1 ELSE 0 END),
               sum(CASE WHEN p.v > i.v + 0.01 THEN 1 ELSE 0 END),
               sum(CASE WHEN p.v < i.v - 0.01 THEN 1 ELSE 0 END),
               max(abs(i.v - p.v))
        FROM i JOIN p USING (order_id)
    """).fetchone()
    n, match, over, under, worst = row
    print(f"  orders compared:            {n:,}")
    print(f"  payment == price + freight: {match:,}  ({match / n:.2%})")
    print(f"  customer paid more:         {over:,}")
    print(f"  customer paid less:         {under:,}")
    print(f"  largest single gap:         {worst:,.2f}")


def probe_geolocation(con) -> None:
    rule(6, "GEOLOCATION - the worst join in the schema")
    customers = one(con, "SELECT count(*) FROM customers")
    joined = one(con, """
        SELECT count(*) FROM customers c
        JOIN geolocation g
          ON c.customer_zip_code_prefix = g.geolocation_zip_code_prefix
    """)
    print(f"  customers:                    {customers:,}")
    print(f"  after joining geolocation:    {joined:,}  "
          f"({joined / customers:.1f}x)")
    print("\n  Any aggregate computed after this join is multiplied by ~53.")

    mismatch = one(con, """
        SELECT count(*) FROM (
            SELECT DISTINCT c.customer_zip_code_prefix, c.customer_state,
                            g.geolocation_state
            FROM customers c JOIN geolocation g
              ON c.customer_zip_code_prefix = g.geolocation_zip_code_prefix
            WHERE c.customer_state <> g.geolocation_state
        )
    """)
    print(f"  zip prefixes where customer_state disagrees with "
          f"geolocation_state: {mismatch:,}")


def probe_categories(con) -> None:
    rule(7, "CATEGORY JOIN - what falls out")
    total = one(con, "SELECT count(*) FROM products")
    kept = one(con, """
        SELECT count(*) FROM products p
        JOIN product_category_translation t
          ON p.product_category_name = t.product_category_name
    """)
    print(f"  products:              {total:,}")
    print(f"  survive inner join:    {kept:,}  (lost {total - kept:,})")

    rev_all = one(con, "SELECT sum(price) FROM order_items")
    rev_kept = one(con, """
        SELECT sum(oi.price) FROM order_items oi
        JOIN products p ON oi.product_id = p.product_id
        JOIN product_category_translation t
          ON p.product_category_name = t.product_category_name
    """)
    print(f"\n  item revenue, all products:      {rev_all:>14,.2f}")
    print(f"  item revenue, after category join {rev_kept:>14,.2f}")
    print(f"  silently lost:                    "
          f"{rev_all - rev_kept:>14,.2f}  ({1 - rev_kept / rev_all:.2%})")


def probe_time(con) -> None:
    rule(8, "TIME COVERAGE - both end years are partial")
    print("  month     orders")
    for ym, n in q(con, """
        SELECT strftime(order_purchase_timestamp, '%Y-%m') AS ym, count(*)
        FROM orders GROUP BY 1 ORDER BY 1
    """):
        bar = "#" * min(int(n / 200), 40)
        print(f"  {ym}   {n:>6,}  {bar}")
    print("\n  Year-over-year comparisons across 2016 or 2018 are not comparable.")


def probe_marketing(con, tables: set[str]) -> None:
    if not {"marketing_qualified_leads", "closed_deals"} <= tables:
        return
    rule(9, "MARKETING FUNNEL - how much of it connects")
    leads = one(con, "SELECT count(*) FROM marketing_qualified_leads")
    deals = one(con, "SELECT count(*) FROM closed_deals")
    sellers = one(con, "SELECT count(*) FROM sellers")
    matched = one(con, """
        SELECT count(*) FROM closed_deals d
        WHERE EXISTS (SELECT 1 FROM sellers s WHERE s.seller_id = d.seller_id)
    """)
    print(f"  qualified leads:                 {leads:,}")
    print(f"  closed deals:                    {deals:,}")
    print(f"  sellers:                         {sellers:,}")
    print(f"  closed deals matching a seller:  {matched:,}  "
          f"({matched / deals:.1%} of deals, {matched / sellers:.1%} of sellers)")
    print("\n  Funnel questions cover a small, non-random slice of the marketplace.")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("data/olist.duckdb"))
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"{args.db} not found - run 01_build_warehouse.py first")

    con = duckdb.connect(str(args.db), read_only=True)
    tables = {r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main'").fetchall()}

    probe_coverage(con)
    probe_status_dates(con)
    probe_reviews(con)
    probe_payments(con)
    probe_geolocation(con)
    probe_categories(con)
    probe_time(con)
    probe_marketing(con, tables)

    con.close()
    print()
    print("=" * 72)
    print("done - every number above is a candidate glossary entry")


if __name__ == "__main__":
    main()
