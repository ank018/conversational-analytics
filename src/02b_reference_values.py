"""
Stage 2b - reference values.

Computes the canonical value of every metric defined in
docs/data_semantics.md, alongside the common wrong alternative, and writes
them to reports/reference_values.json.

Two uses. Now: the numbers fill the open items in the semantics document, so
gold answers are written against measured values rather than assumptions.
Later: the JSON is a regression fixture - if a refactor changes what
"revenue" evaluates to, a test fails instead of a gold answer quietly going
stale.

Run from the repo root:
    python src/02b_reference_values.py
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb

# Applied to every revenue and order-count query. Decision 2.2.
EXCLUDED = "('canceled', 'unavailable')"

# label -> (sql, note). Where a definition has a common wrong alternative,
# both are computed so the size of the error is on record.
METRICS: dict[str, tuple[str, str]] = {
    "revenue": (
        f"""SELECT sum(oi.price) FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            WHERE o.order_status NOT IN {EXCLUDED}""",
        "canonical: product price, excluding cancelled/unavailable",
    ),
    "revenue_all_statuses": (
        "SELECT sum(price) FROM order_items",
        "wrong: no status filter",
    ),
    "revenue_with_freight": (
        f"""SELECT sum(oi.price + oi.freight_value) FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            WHERE o.order_status NOT IN {EXCLUDED}""",
        "variant: includes freight",
    ),
    "total_paid": (
        "SELECT sum(payment_value) FROM order_payments",
        "what customers paid, all statuses",
    ),
    "revenue_naive_payment_join": (
        f"""SELECT sum(oi.price) FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            JOIN order_payments op ON op.order_id = oi.order_id
            WHERE o.order_status NOT IN {EXCLUDED}""",
        "WRONG: fans out on multi-payment orders",
    ),
    "order_count": (
        f"SELECT count(*) FROM orders WHERE order_status NOT IN {EXCLUDED}",
        "canonical order count",
    ),
    "order_count_with_items": (
        f"""SELECT count(DISTINCT o.order_id) FROM orders o
            JOIN order_items oi ON oi.order_id = o.order_id
            WHERE o.order_status NOT IN {EXCLUDED}""",
        "orders that actually contain something",
    ),
    "customers_unique": (
        f"""SELECT count(DISTINCT c.customer_unique_id)
            FROM customers c JOIN orders o ON o.customer_id = c.customer_id
            WHERE o.order_status NOT IN {EXCLUDED}""",
        "canonical: distinct people",
    ),
    "customers_by_customer_id": (
        f"""SELECT count(DISTINCT c.customer_id)
            FROM customers c JOIN orders o ON o.customer_id = c.customer_id
            WHERE o.order_status NOT IN {EXCLUDED}""",
        "WRONG: customer_id is per order",
    ),
    "repeat_customers": (
        f"""SELECT count(*) FROM (
              SELECT c.customer_unique_id FROM customers c
              JOIN orders o ON o.customer_id = c.customer_id
              WHERE o.order_status NOT IN {EXCLUDED}
              GROUP BY 1 HAVING count(DISTINCT o.order_id) > 1)""",
        "people with more than one order",
    ),
    "average_order_value": (
        f"""SELECT avg(v) FROM (
              SELECT o.order_id, sum(oi.price) AS v FROM orders o
              JOIN order_items oi ON oi.order_id = o.order_id
              WHERE o.order_status NOT IN {EXCLUDED} GROUP BY 1)""",
        "canonical: aggregate to order first",
    ),
    "average_item_price": (
        f"""SELECT avg(oi.price) FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            WHERE o.order_status NOT IN {EXCLUDED}""",
        "WRONG for AOV: item value, not order value",
    ),
    "items_sold": (
        f"""SELECT count(*) FROM order_items oi
            JOIN orders o ON o.order_id = oi.order_id
            WHERE o.order_status NOT IN {EXCLUDED}""",
        "item lines sold",
    ),
    "avg_delivery_days": (
        """SELECT avg(date_diff('day', order_purchase_timestamp,
                                order_delivered_customer_date))
           FROM orders WHERE order_status = 'delivered'
             AND order_delivered_customer_date IS NOT NULL""",
        "purchase to customer delivery, delivered only",
    ),
    "on_time_rate": (
        """SELECT avg(CASE WHEN order_delivered_customer_date
                            <= order_estimated_delivery_date
                      THEN 1.0 ELSE 0.0 END)
           FROM orders WHERE order_status = 'delivered'
             AND order_delivered_customer_date IS NOT NULL""",
        "share delivered by the estimate",
    ),
    "avg_review_score": (
        "SELECT avg(review_score) FROM order_reviews",
        "over review rows",
    ),
    "avg_review_score_dedup": (
        """SELECT avg(s) FROM (
             SELECT review_id, max(review_score) AS s
             FROM order_reviews GROUP BY 1)""",
        "over distinct review_id",
    ),
    "revenue_lost_to_inner_category_join": (
        f"""SELECT
              (SELECT sum(oi.price) FROM order_items oi
                 JOIN orders o ON o.order_id = oi.order_id
                 WHERE o.order_status NOT IN {EXCLUDED})
            - (SELECT sum(oi.price) FROM order_items oi
                 JOIN orders o ON o.order_id = oi.order_id
                 JOIN products p ON p.product_id = oi.product_id
                 JOIN product_category_translation t
                   ON t.product_category_name = p.product_category_name
                 WHERE o.order_status NOT IN {EXCLUDED})""",
        "silent loss from an inner category join",
    ),
    "payments_on_itemless_orders": (
        """SELECT sum(op.payment_value) FROM order_payments op
           WHERE NOT EXISTS (SELECT 1 FROM order_items oi
                             WHERE oi.order_id = op.order_id)""",
        "open item 2: does this explain the R$165k gap?",
    ),
}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=Path("data/olist.duckdb"))
    args = ap.parse_args()

    if not args.db.exists():
        raise SystemExit(f"{args.db} not found - run 01_build_warehouse.py first")

    con = duckdb.connect(str(args.db), read_only=True)
    results: dict[str, float | int | None] = {}

    print("=" * 72)
    print("REFERENCE VALUES")
    print("=" * 72)
    for label, (sql, note) in METRICS.items():
        value = con.execute(sql).fetchone()[0]
        results[label] = float(value) if value is not None else None
        shown = f"{value:,.2f}" if isinstance(value, float) else f"{value:,}"
        print(f"  {label:<38} {shown:>18}   {note}")
    con.close()

    print()
    print("-" * 72)
    print("CONSEQUENCES")
    print("-" * 72)
    rev = results["revenue"]
    print(f"  status exclusion costs "
          f"{results['revenue_all_statuses'] - rev:,.2f} "
          f"({1 - rev / results['revenue_all_statuses']:.2%} of unfiltered)")
    print(f"  naive payment join overstates by "
          f"{results['revenue_naive_payment_join'] / rev - 1:.2%}")
    print(f"  counting customer_id overstates customers by "
          f"{results['customers_by_customer_id'] / results['customers_unique'] - 1:.2%}")
    print(f"  average item price is "
          f"{1 - results['average_item_price'] / results['average_order_value']:.2%} "
          f"below average order value")
    print(f"  repeat customers are "
          f"{results['repeat_customers'] / results['customers_unique']:.2%} of people")

    Path("reports").mkdir(exist_ok=True)
    out = Path("reports/reference_values.json")
    out.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\nwritten to {out}  (commit this - it is a regression fixture)")


if __name__ == "__main__":
    main()
