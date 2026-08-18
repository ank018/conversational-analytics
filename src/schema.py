"""
Schema rendering at four levels of detail.

Stage 8 asks a question practitioners actually have: how much schema
description does a model need? The usual assumption is that more context is
better. This makes that testable by rendering the same schema four ways and
running the same gold set against each.

  bare       Column names and types. What stage 6 used.
  keys       Plus the primary and foreign keys that actually hold - measured
             at stage 1, not assumed. products.product_category_name is
             deliberately absent: 13 values have no matching translation row,
             so declaring it as a foreign key would state something false.
  values     Plus sample values, distinct counts and ranges drawn from the
             warehouse.
  described  Plus factual notes from docs/column_notes.yaml.

Each level contains the previous one, so the deltas are cumulative and the
token cost of each addition is visible.

    python src/schema.py            # print sizes for all four levels
    python src/schema.py --level described --show
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import sandbox  # noqa: E402

NOTES_PATH = Path("docs/column_notes.yaml")
LEVELS = ("bare", "keys", "values", "described")

# Verified unique at stage 1. order_reviews.review_id and geolocation are
# absent because neither is unique - saying otherwise would be a lie the
# model would then reason from.
PRIMARY_KEYS: dict[str, list[str]] = {
    "customers": ["customer_id"],
    "orders": ["order_id"],
    "products": ["product_id"],
    "sellers": ["seller_id"],
    "order_items": ["order_id", "order_item_id"],
    "order_payments": ["order_id", "payment_sequential"],
    "product_category_translation": ["product_category_name"],
    "marketing_qualified_leads": ["mql_id"],
}

# Zero orphans at stage 1. products.product_category_name has 13 and is
# excluded; closed_deals.seller_id is mostly null and is excluded.
FOREIGN_KEYS: list[tuple[str, str, str, str]] = [
    ("orders", "customer_id", "customers", "customer_id"),
    ("order_items", "order_id", "orders", "order_id"),
    ("order_items", "product_id", "products", "product_id"),
    ("order_items", "seller_id", "sellers", "seller_id"),
    ("order_payments", "order_id", "orders", "order_id"),
    ("order_reviews", "order_id", "orders", "order_id"),
    ("closed_deals", "mql_id", "marketing_qualified_leads", "mql_id"),
]

MAX_DISTINCT_TO_LIST = 30
SAMPLES = 3


def _load_notes() -> dict[str, dict[str, str]]:
    if not NOTES_PATH.exists():
        return {}
    raw = yaml.safe_load(NOTES_PATH.read_text(encoding="utf-8")) or {}
    return {t: {c: " ".join(str(v).split()) for c, v in cols.items()}
            for t, cols in raw.items()}


def _column_facts(con, table: str, column: str, dtype: str) -> str:
    """A short parenthetical describing what this column holds."""
    q = f'"{table}"."{column}"'
    try:
        n_distinct, n_null = con.execute(
            f'SELECT count(DISTINCT {q}), '
            f'sum(CASE WHEN {q} IS NULL THEN 1 ELSE 0 END) '
            f'FROM "{table}"').fetchone()
    except Exception:
        return ""

    bits = []
    upper = dtype.upper()

    if n_distinct and n_distinct <= MAX_DISTINCT_TO_LIST:
        vals = [r[0] for r in con.execute(
            f'SELECT DISTINCT {q} FROM "{table}" WHERE {q} IS NOT NULL '
            f'ORDER BY 1 LIMIT {MAX_DISTINCT_TO_LIST}').fetchall()]
        bits.append("one of: " + ", ".join(repr(v) for v in vals))
    elif any(t in upper for t in ("INT", "DOUBLE", "DECIMAL", "FLOAT",
                                  "DATE", "TIMESTAMP")):
        lo, hi = con.execute(
            f'SELECT min({q}), max({q}) FROM "{table}"').fetchone()
        bits.append(f"{lo} to {hi}")
        bits.append(f"{n_distinct:,} distinct")
    else:
        vals = [r[0] for r in con.execute(
            f'SELECT DISTINCT {q} FROM "{table}" WHERE {q} IS NOT NULL '
            f'LIMIT {SAMPLES}').fetchall()]
        shown = [str(v)[:24] for v in vals]
        bits.append("e.g. " + ", ".join(repr(v) for v in shown))
        bits.append(f"{n_distinct:,} distinct")

    if n_null:
        bits.append(f"{n_null:,} null")
    return "; ".join(bits)


def build(level: str, con=None) -> str:
    """Render the schema at one level of detail."""
    if level not in LEVELS:
        raise ValueError(f"level must be one of {LEVELS}")

    own = con is None
    con = con or sandbox.connect()
    notes = _load_notes() if level == "described" else {}

    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY table_name").fetchall()]

    out: list[str] = []
    for table in tables:
        cols = con.execute(f'DESCRIBE "{table}"').fetchall()
        # (code, comment) kept apart so the separating comma lands BEFORE the
        # comment. A `--` comment runs to end of line, so a comma appended
        # after one is commented out and the model receives a column list
        # with no separators.
        parts: list[tuple[str, str]] = []
        for name, dtype, *_ in cols:
            trailing = []
            if level in ("values", "described"):
                facts = _column_facts(con, table, name, dtype)
                if facts:
                    trailing.append(facts)
            if level == "described":
                note = notes.get(table, {}).get(name)
                if note:
                    trailing.append(note)
            parts.append((f"  {name} {dtype}", " | ".join(trailing)))

        constraints = []
        if level != "bare":
            pk = PRIMARY_KEYS.get(table)
            if pk:
                constraints.append(f"  PRIMARY KEY ({', '.join(pk)})")
            for child, ccol, parent, pcol in FOREIGN_KEYS:
                if child == table:
                    constraints.append(
                        f"  FOREIGN KEY ({ccol}) REFERENCES {parent}({pcol})")

        rows = [(code, comment) for code, comment in parts]
        lines = []
        for i, (code, comment) in enumerate(rows):
            last = (i == len(rows) - 1) and not constraints
            code = code if last else code + ","
            lines.append(f"{code}    -- {comment}" if comment else code)
        for i, c in enumerate(constraints):
            lines.append(c if i == len(constraints) - 1 else c + ",")

        out.append(f"CREATE TABLE {table} (\n" + "\n".join(lines) + "\n);")

    if own:
        con.close()
    return "\n\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--level", choices=LEVELS, default=None)
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    con = sandbox.connect()
    if args.level:
        text = build(args.level, con)
        if args.show:
            print(text)
        print(f"\n{args.level}: {len(text):,} chars "
              f"(~{len(text)//4:,} tokens)")
        con.close()
        return 0

    print("=" * 62)
    print("SCHEMA SIZE BY LEVEL")
    print("=" * 62)
    base = None
    for level in LEVELS:
        text = build(level, con)
        tok = len(text) // 4
        base = base or tok
        print(f"  {level:<12} {len(text):>8,} chars   ~{tok:>6,} tokens   "
              f"{tok/base:>5.1f}x bare")
    con.close()
    print("\nCost per question scales with these. If accuracy does not move,")
    print("the extra tokens are pure waste and that is the finding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
