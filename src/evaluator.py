"""
Evaluation: does a candidate query's result match gold?

"Match" is not obvious, and every choice below moves the headline accuracy
number. They are stated here rather than left to whatever `==` does.

  Column names are ignored. A model writing `AS total_revenue` where gold
  writes `AS revenue` has answered the question. Matching on names would
  measure alias-guessing.

  Column order is ignored. Any assignment of gold's columns onto the
  candidate's is tried. This is permissive - with small integer columns two
  different columns can coincidentally align - so `column_map` is recorded on
  every verdict and a non-identity mapping is worth eyeballing.

  Extra candidate columns are allowed. Gold's columns must all appear; a
  model returning the answer plus a supporting count has still answered.

  Row order matters only when gold says so. Questions with an ORDER BY carry
  `ordered: true`; everything else compares as a bag.

  Numbers compare with relative tolerance 1e-6. A model rounding currency to
  two decimals passes; one rounding a rate from 0.9236 to 0.92 does not. That
  is a real decision and the rounding failures should be counted at stage 7
  rather than quietly tolerated.

  Strings compare case-folded and stripped, so `upper(state)` does not fail.

  NULL equals only NULL. q404's null growth in January is correct and must
  not match a zero.

The verdict split is the point of the project:

  correct      - matches gold
  wrong_value  - ran, right shape, wrong numbers   } SILENT failures: the user
  wrong_shape  - ran, wrong number of rows/columns } sees a clean answer that
  empty        - ran, returned nothing             } happens to be wrong
  error        - SQL failed                        } VISIBLE failures: the
  blocked      - sandbox refused it                } user knows something broke

Silent error rate is the number a business cares about and almost nobody
reports. Execution accuracy alone hides it.

Self-test:
    python src/evaluator.py
"""

from __future__ import annotations

import datetime as dt
import sys
from dataclasses import dataclass, field
from decimal import Decimal
from itertools import permutations
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gold as goldmod  # noqa: E402
import sandbox  # noqa: E402

REL_TOL = 1e-6
MAX_CANDIDATE_COLS_FOR_PERMUTATION = 8

SILENT = {"wrong_value", "wrong_shape", "empty"}
# Every way the sandbox can refuse or fail a query. Derived from sandbox
# rather than listed here, so a new error kind cannot silently fall outside
# both buckets - an earlier version listed only {"error", "blocked"} and
# syntax errors went uncounted, breaking the invariant below.
VISIBLE = set(sandbox.ERROR_KINDS) | {"error"}

# Invariant: every verdict is correct, silent or visible, and the three rates
# sum to 1. Asserted in the self-test.
assert not (SILENT & VISIBLE)


@dataclass
class Verdict:
    question_id: str
    kind: str                       # correct | wrong_value | wrong_shape |
    #                                 empty | error | blocked
    detail: str = ""
    column_map: tuple[int, ...] | None = None
    elapsed_ms: float = 0.0
    candidate_sql: str = ""

    @property
    def correct(self) -> bool:
        return self.kind == "correct"

    @property
    def silent(self) -> bool:
        return self.kind in SILENT

    @property
    def visible(self) -> bool:
        return self.kind in VISIBLE

    def __repr__(self) -> str:
        return f"<{self.question_id} {self.kind}{': ' + self.detail if self.detail else ''}>"


# --------------------------------------------------------------------------
# Cell and row comparison
# --------------------------------------------------------------------------

def _norm(value):
    """Normalise one cell to a comparable form."""
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float, Decimal)):
        return float(value)
    if isinstance(value, (dt.datetime, dt.date)):
        return value.isoformat()
    if isinstance(value, str):
        return value.strip().casefold()
    return str(value).strip().casefold()


def _equal(a, b, tol: float = REL_TOL) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, float) and isinstance(b, float):
        if a == b:
            return True
        return abs(a - b) <= tol * max(abs(a), abs(b), 1e-12)
    if isinstance(a, float) != isinstance(b, float):
        return False
    return a == b


def _sort_key(row: tuple):
    """Stable ordering for bag comparison across mixed types."""
    key = []
    for cell in row:
        if cell is None:
            key.append((0, 0.0, ""))
        elif isinstance(cell, float):
            key.append((1, cell, ""))
        else:
            key.append((2, 0.0, str(cell)))
    return tuple(key)


def _rows_match(gold_rows: list[tuple], cand_rows: list[tuple],
                ordered: bool, tol: float) -> bool:
    if len(gold_rows) != len(cand_rows):
        return False
    if not ordered:
        gold_rows = sorted(gold_rows, key=_sort_key)
        cand_rows = sorted(cand_rows, key=_sort_key)
    for g_row, c_row in zip(gold_rows, cand_rows):
        for g, c in zip(g_row, c_row):
            if not _equal(g, c, tol):
                return False
    return True


def compare(gold_result: sandbox.QueryResult,
            cand_result: sandbox.QueryResult,
            ordered: bool,
            tol: float = REL_TOL) -> tuple[str, str, tuple[int, ...] | None]:
    """Return (kind, detail, column_map)."""
    if not cand_result.rows:
        return "empty", "candidate returned no rows", None

    n_gold_cols = len(gold_result.columns)
    n_cand_cols = len(cand_result.columns)

    if n_cand_cols < n_gold_cols:
        return ("wrong_shape",
                f"{n_cand_cols} columns, gold has {n_gold_cols}", None)

    if len(cand_result.rows) != len(gold_result.rows):
        return ("wrong_shape",
                f"{len(cand_result.rows)} rows, gold has "
                f"{len(gold_result.rows)}", None)

    gold_rows = [tuple(_norm(v) for v in r) for r in gold_result.rows]
    cand_rows = [tuple(_norm(v) for v in r) for r in cand_result.rows]

    # Try column assignments. Identity first - it is nearly always right, and
    # trying it first stops a coincidental permutation being reported.
    candidates: list[tuple[int, ...]] = [tuple(range(n_gold_cols))]
    if n_cand_cols <= MAX_CANDIDATE_COLS_FOR_PERMUTATION:
        candidates += [p for p in permutations(range(n_cand_cols), n_gold_cols)
                       if p != tuple(range(n_gold_cols))]

    for mapping in candidates:
        if max(mapping, default=-1) >= n_cand_cols:
            continue
        projected = [tuple(row[i] for i in mapping) for row in cand_rows]
        if _rows_match(gold_rows, projected, ordered, tol):
            return "correct", "", mapping

    # Shape was right, so the failure is in the values. Report the first
    # positional disagreement - stage 7 needs something countable.
    detail = "values differ"
    for r, (g_row, c_row) in enumerate(zip(gold_rows, cand_rows)):
        for c in range(n_gold_cols):
            if c < len(c_row) and not _equal(g_row[c], c_row[c], tol):
                detail = (f"row {r} col {c}: gold {g_row[c]!r} "
                          f"vs {c_row[c]!r}")
                break
        else:
            continue
        break
    return "wrong_value", detail, None


# --------------------------------------------------------------------------
# Running an evaluation
# --------------------------------------------------------------------------

class GoldCache:
    """Executes each gold query once and holds the result."""

    def __init__(self, con):
        self.con = con
        self._cache: dict[str, sandbox.QueryResult] = {}

    def get(self, question: goldmod.GoldQuestion) -> sandbox.QueryResult:
        if question.id not in self._cache:
            res = sandbox.run(question.sql, con=self.con)
            if not res.ok:
                raise RuntimeError(
                    f"gold SQL for {question.id} does not run: {res.error}")
            self._cache[question.id] = res
        return self._cache[question.id]


def evaluate(question: goldmod.GoldQuestion,
             candidate_sql: str,
             cache: GoldCache,
             tol: float = REL_TOL) -> Verdict:
    """Score one candidate query against one gold question."""
    if question.kind != "answerable":
        raise ValueError(
            f"{question.id} is {question.kind}; abstention is scored "
            f"separately, not by result comparison")

    cand = sandbox.run(candidate_sql, con=cache.con)
    if not cand.ok:
        return Verdict(question.id, cand.error_kind or "error",
                       cand.error or "", elapsed_ms=cand.elapsed_ms,
                       candidate_sql=candidate_sql)

    gold_res = cache.get(question)
    kind, detail, mapping = compare(gold_res, cand, question.ordered, tol)
    return Verdict(question.id, kind, detail, mapping,
                   cand.elapsed_ms, candidate_sql)


@dataclass
class RunSummary:
    verdicts: list[Verdict] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.verdicts)

    @property
    def execution_accuracy(self) -> float:
        return sum(v.correct for v in self.verdicts) / self.n if self.n else 0.0

    @property
    def silent_error_rate(self) -> float:
        return sum(v.silent for v in self.verdicts) / self.n if self.n else 0.0

    @property
    def visible_error_rate(self) -> float:
        return sum(v.visible for v in self.verdicts) / self.n if self.n else 0.0

    def by_kind(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for v in self.verdicts:
            out[v.kind] = out.get(v.kind, 0) + 1
        return out


# --------------------------------------------------------------------------
# Self-test: mutation cases
# --------------------------------------------------------------------------
#
# The evaluator has to do two jobs, and failing either one silently corrupts
# every number this project reports:
#
#   1. Accept queries that differ from gold only cosmetically. If it does not,
#      accuracy is understated and the ablation measures alias-guessing.
#   2. Reject the specific wrong queries the gold set was built around. If it
#      does not, accuracy is overstated and the traps are decorative.
#
# Each EQUIVALENT case must score correct. Each WRONG case must not.

EQUIVALENT: list[tuple[str, str, str]] = [
    ("q001", "different alias", """
        SELECT count(*) AS total_orders FROM orders
        WHERE order_status NOT IN ('canceled', 'unavailable')"""),
    ("q001", "rewritten as NOT (IN)", """
        SELECT count(*) FROM orders
        WHERE NOT (order_status IN ('unavailable', 'canceled'))"""),
    ("q101", "rounded to two decimals", """
        SELECT round(sum(oi.price), 2) FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')"""),
    ("q101", "subquery instead of join", """
        SELECT sum(price) FROM order_items
        WHERE order_id IN (SELECT order_id FROM orders
                           WHERE order_status NOT IN ('canceled','unavailable'))"""),
    ("q010", "state upper-cased", """
        SELECT upper(customer_state), count(DISTINCT customer_unique_id)
        FROM customers c JOIN orders o ON o.customer_id = c.customer_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1"""),
    ("q109", "extra supporting column", """
        SELECT strftime(o.order_purchase_timestamp, '%Y-%m') AS month,
               sum(oi.price) AS revenue, count(DISTINCT o.order_id) AS orders
        FROM order_items oi JOIN orders o ON o.order_id = oi.order_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1"""),
    ("q005", "columns in the other order", """
        SELECT count(*) AS payments, payment_type
        FROM order_payments GROUP BY 2 ORDER BY 1 DESC"""),
]

WRONG: list[tuple[str, str, str]] = [
    ("q001", "status filter dropped", "SELECT count(*) FROM orders"),
    ("q101", "items joined to payments", """
        SELECT sum(oi.price) FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
        JOIN order_payments op ON op.order_id = oi.order_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')"""),
    ("q101", "freight included", """
        SELECT sum(oi.price + oi.freight_value) FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')"""),
    ("q103", "average item, not average order", """
        SELECT avg(oi.price) FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')"""),
    ("q104", "customer_id counted as customers", """
        SELECT count(DISTINCT c.customer_id) FROM customers c
        JOIN orders o ON o.customer_id = c.customer_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')"""),
    ("q508", "inner join to reviews", """
        SELECT count(DISTINCT r.order_id) * 1.0 / count(DISTINCT o.order_id)
        FROM orders o JOIN order_reviews r ON r.order_id = o.order_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')"""),
    ("q501", "DISTINCT dropped", """
        SELECT oi.seller_id, count(*) FROM order_items oi
        JOIN orders o ON o.order_id = oi.order_id
        WHERE o.order_status NOT IN ('canceled', 'unavailable')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 3"""),
    ("q512", "grouped by raw timestamp", """
        SELECT order_purchase_timestamp, count(*) FROM orders
        WHERE order_status NOT IN ('canceled', 'unavailable')
        GROUP BY 1 ORDER BY 2 DESC LIMIT 1"""),
    ("q311", "name length instead of physical length",
     "SELECT avg(product_name_lenght) FROM products"),
    ("q001", "syntactically broken", "SELEC count(*) FROM orders"),
    ("q001", "destructive statement", "DROP TABLE orders"),
]


def _self_test() -> int:
    questions, problems = goldmod.load_gold()
    if problems:
        print("gold set has schema problems; fix those first")
        for p in problems:
            print(f"  {p}")
        return 1

    by_id = {q.id: q for q in questions}
    con = sandbox.connect()
    cache = GoldCache(con)
    failures = 0
    all_verdicts: list[Verdict] = []

    print("=" * 78)
    print("EVALUATOR SELF-TEST")
    print("=" * 78)
    print(f"  relative tolerance {REL_TOL:g}\n")

    print("EQUIVALENT - these must all score correct")
    print("-" * 78)
    for qid, label, sql in EQUIVALENT:
        q = by_id.get(qid)
        if q is None:
            print(f"  SKIP  {qid} not in gold set")
            continue
        v = evaluate(q, sql, cache)
        all_verdicts.append(v)
        ok = v.correct
        failures += not ok
        cols = "" if v.column_map in (None, tuple(range(len(v.column_map or ())))) \
            else f"  cols={v.column_map}"
        print(f"  {'PASS' if ok else 'FAIL'}  {qid} {label:<38} "
              f"{v.kind}{cols}")
        if not ok and v.detail:
            print(f"        {v.detail}")

    print("\nWRONG - these must all be caught")
    print("-" * 78)
    for qid, label, sql in WRONG:
        q = by_id.get(qid)
        if q is None:
            print(f"  SKIP  {qid} not in gold set")
            continue
        v = evaluate(q, sql, cache)
        all_verdicts.append(v)
        ok = not v.correct
        failures += not ok
        tag = "silent" if v.silent else "visible" if v.visible else "?"
        print(f"  {'PASS' if ok else 'FAIL'}  {qid} {label:<38} "
              f"{v.kind} ({tag})")
        if ok and v.detail:
            print(f"        {v.detail[:64]}")

    con.close()
    total = len(EQUIVALENT) + len(WRONG)

    # Every verdict must land in exactly one bucket, or the rates reported at
    # stage 6 will not sum to 1 and failures will go missing from the split.
    print("\nPARTITION - every verdict is correct, silent or visible")
    print("-" * 78)
    unclassified = [v for v in all_verdicts
                    if not (v.correct or v.silent or v.visible)]
    if unclassified:
        failures += len(unclassified)
        for v in unclassified:
            print(f"  FAIL  {v.question_id} kind '{v.kind}' is in neither bucket")
    else:
        n_c = sum(v.correct for v in all_verdicts)
        n_s = sum(v.silent for v in all_verdicts)
        n_v = sum(v.visible for v in all_verdicts)
        print(f"  PASS  {n_c} correct + {n_s} silent + {n_v} visible "
              f"= {len(all_verdicts)}")

    print()
    print("=" * 78)
    print(f"{total - failures} of {total} passed")
    if failures:
        print("The evaluator is not trustworthy until this is clean.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(_self_test())
