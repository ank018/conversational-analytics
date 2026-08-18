"""
Stage 7 - error taxonomy.

Turns "50% silent error rate" into a list of named causes with counts. That
list is what predicts whether the glossary at stage 9 moves ten points or two,
and it is the only way to tell a model that misunderstands the business from
one that cannot write SQL.

Two passes.

  python src/07_error_taxonomy.py --build
      Reads the newest run, writes reports/error_taxonomy.yaml with one entry
      per failure: the question, gold SQL, the model's SQL, and a SUGGESTED
      cause derived by diffing the two for known markers. The `cause:` field
      is left blank for you.

  python src/07_error_taxonomy.py --summarise
      Reads the filled file and reports counts, plus a predicted ceiling for
      each intervention.

The suggestions are pattern matches, not judgements. They exist to make the
human pass fast, not to replace it - a suggestion accepted without reading
the SQL is worth nothing, and two of the categories below only a human can
assign.

Two categories deserve particular attention:

  defensible_disagreement - the model's query is a reasonable answer and gold
      encodes a choice we made. q404 windowed from December so January had a
      real growth figure rather than a null; that is arguably better analysis
      than the gold. These are not model failures and inflate the error rate.

  gold_wrong - the gold answer is simply incorrect. Every one found here is a
      question that would have penalised correct behaviour for the rest of the
      project. This is the last cheap opportunity to find them.
"""

from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from collections import Counter
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import gold as goldmod  # noqa: E402

RUNS_DIR = Path("reports/runs")
OUT_PATH = Path("reports/error_taxonomy.yaml")

# cause -> (what it means, which intervention plausibly fixes it)
CAUSES: dict[str, tuple[str, str]] = {
    "missing_status_filter":
        ("Did not exclude cancelled/unavailable orders", "glossary"),
    "wrong_revenue_metric":
        ("Used payment_value or included freight as revenue", "glossary"),
    "wrong_customer_key":
        ("Counted customer_id instead of customer_unique_id", "glossary"),
    "wrong_aggregation_level":
        ("Averaged item rows where the order was the unit", "glossary"),
    "wrong_date_column":
        ("Filtered on the wrong one of the five date columns", "glossary"),
    "wrong_join_type":
        ("Inner join where LEFT was needed, or the reverse", "glossary"),
    "fanout":
        ("A join multiplied rows and inflated an aggregate", "glossary"),
    "missing_distinct":
        ("Counted item rows where the question asked for orders", "glossary"),
    "wrong_column":
        ("Picked a similarly named column measuring something else",
         "schema_detail"),
    "wrong_granularity":
        ("Grouped by year where months were asked for, or similar",
         "few_shot"),
    "missing_threshold":
        ("Ignored a minimum stated in the question", "few_shot"),
    "shape_mismatch":
        ("Right idea, wrong number of rows or columns", "few_shot"),
    "sql_error":
        ("Query did not parse or bind", "self_repair"),
    "empty_or_truncated":
        ("Model returned nothing, or ran out of tokens", "none"),
    "defensible_disagreement":
        ("Model's answer is reasonable; gold encodes our choice", "none"),
    "gold_wrong":
        ("Gold answer is incorrect and must be fixed", "none"),
    "unclear":
        ("Cause not obvious from the SQL", "none"),
}


def _has(sql: str, pattern: str) -> bool:
    return bool(re.search(pattern, sql or "", re.I))


def suggest(gold_sql: str, model_sql: str, verdict: str, detail: str) -> str:
    """Pattern-match a likely cause. Always to be confirmed by a human."""
    g, m = gold_sql or "", model_sql or ""

    if verdict in ("error", "syntax", "unknown_object", "type_error"):
        return "empty_or_truncated" if "empty" in (detail or "") else "sql_error"

    if _has(g, r"order_status\s+NOT\s+IN") and not _has(m, r"order_status"):
        return "missing_status_filter"
    if _has(m, r"geolocation"):
        return "fanout"
    if _has(g, r"oi\.price|order_items") and _has(m, r"payment_value"):
        return "wrong_revenue_metric"
    if _has(g, r"\bprice\b") and _has(m, r"freight_value") \
            and not _has(g, r"freight_value"):
        return "wrong_revenue_metric"
    if _has(g, r"customer_unique_id") and not _has(m, r"customer_unique_id"):
        return "wrong_customer_key"
    if _has(m, r"order_items") and _has(m, r"order_payments"):
        return "fanout"
    if _has(g, r"count\(\s*DISTINCT") and not _has(m, r"count\(\s*DISTINCT"):
        return "missing_distinct"
    if _has(g, r"LEFT\s+JOIN") and not _has(m, r"LEFT\s+JOIN"):
        return "wrong_join_type"
    if _has(g, r"avg\(") and _has(g, r"GROUP BY") and _has(m, r"avg\(") \
            and not _has(m, r"WITH|FROM\s*\("):
        return "wrong_aggregation_level"
    if _has(g, r"order_delivered_customer_date") \
            and _has(m, r"order_purchase_timestamp") \
            and not _has(m, r"order_delivered_customer_date"):
        return "wrong_date_column"
    if verdict == "wrong_shape":
        return "shape_mismatch"
    return "unclear"


def build(run_path: Path) -> None:
    run = json.loads(run_path.read_text(encoding="utf-8"))
    questions, _ = goldmod.load_gold()
    by_id = {q.id: q for q in questions}

    entries = []
    for rec in run["records"]:
        verdict = rec.get("verdict")
        if verdict in (None, "correct", "abstained"):
            continue
        if rec.get("kind") != "answerable" and verdict != "answered_anyway":
            continue

        q = by_id.get(rec["id"])
        gold_sql = (q.sql or "").strip() if q else ""
        model_sql = (rec.get("sql") or "").strip()

        entries.append({
            "id": rec["id"],
            "tier": rec.get("tier"),
            "verdict": verdict,
            "question": rec.get("question", ""),
            "detail": rec.get("detail", rec.get("abstain_reason", "")),
            "rules": rec.get("rules", []),
            "traps": rec.get("traps", []),
            "suggested_cause": suggest(gold_sql, model_sql, verdict,
                                       rec.get("detail", "")),
            "cause": None,
            "notes": "",
            "gold_sql": gold_sql,
            "model_sql": model_sql,
        })

    header = (
        "# Stage 7 - error taxonomy\n"
        f"# Built from {run_path.name}\n"
        f"# Model: {run.get('model')}\n"
        "#\n"
        "# Fill in `cause:` for every entry. `suggested_cause` is a pattern\n"
        "# match on the SQL, not a judgement - read the two queries before\n"
        "# accepting it.\n"
        "#\n"
        "# Valid causes:\n"
        + "".join(f"#   {k:<26} {v[0]}\n" for k, v in CAUSES.items())
        + "#\n"
        "# Use defensible_disagreement when the model's answer is reasonable\n"
        "# and gold encodes a choice. Use gold_wrong when gold is incorrect -\n"
        "# those questions must then be fixed in eval/gold/.\n\n"
    )
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(
        header + yaml.dump(entries, sort_keys=False, allow_unicode=True,
                           default_flow_style=False, width=88),
        encoding="utf-8")

    print(f"{len(entries)} failures written to {OUT_PATH}")
    print("\nsuggested causes (to be confirmed):")
    for cause, n in Counter(e["suggested_cause"] for e in entries).most_common():
        print(f"  {cause:<28} {n:>3}   {CAUSES[cause][0]}")
    print(f"\nFill in `cause:` for each, then run --summarise")


def summarise() -> None:
    if not OUT_PATH.exists():
        raise SystemExit(f"{OUT_PATH} not found - run --build first")
    entries = [e for e in yaml.safe_load(OUT_PATH.read_text(encoding="utf-8"))
               if isinstance(e, dict)]

    unfilled = [e["id"] for e in entries if not e.get("cause")]
    if unfilled:
        print(f"WARNING: {len(unfilled)} entries have no cause: "
              f"{', '.join(unfilled[:12])}"
              f"{' ...' if len(unfilled) > 12 else ''}\n")

    filled = [e for e in entries if e.get("cause")]
    unknown = {e["cause"] for e in filled} - set(CAUSES)
    if unknown:
        raise SystemExit(f"unrecognised causes: {sorted(unknown)}")

    print("=" * 74)
    print("ERROR TAXONOMY")
    print("=" * 74)
    print(f"  {len(filled)} of {len(entries)} failures classified\n")

    counts = Counter(e["cause"] for e in filled)
    for cause, n in counts.most_common():
        agree = sum(1 for e in filled
                    if e["cause"] == cause and e["cause"] == e["suggested_cause"])
        print(f"  {cause:<28} {n:>3}   ({agree} matched the suggestion)")

    # Abstention failures come from the 12 no-answer questions, not from
    # the 74 answerable ones. Mixing them into the ceiling arithmetic
    # subtracts them from a total they were never part of.
    abstention = [e for e in filled if e.get("verdict") == "answered_anyway"]
    answerable = [e for e in filled if e.get("verdict") != "answered_anyway"]

    print("\n  by intervention (answerable questions only)")
    by_iv = Counter(CAUSES[e["cause"]][1] for e in answerable)
    for iv, n in by_iv.most_common():
        print(f"    {iv:<20} {n:>3} failures")

    if abstention:
        print(f"\n  abstention failures ({len(abstention)}) - scored against "
              f"the 12 no-answer questions, held out of the ceiling below")
        for e in abstention:
            print(f"    {e['id']:<7} {e['cause']}")

    # Predicted ceilings. Written down before stage 9 runs so the prediction
    # can be checked rather than reconstructed afterwards.
    # Read the denominator from the gold set rather than hardcoding it.
    # Questions move between kinds - q205 was reclassified from unsupported to
    # answerable at stage 7 - and a stale constant silently shifts every
    # percentage below.
    questions, _ = goldmod.load_gold(verified_only=True)
    n_answerable = sum(1 for q in questions if q.kind == "answerable")
    n_correct = n_answerable - len(answerable)
    print(f"\n  predicted ceiling if each intervention fixed every failure "
          f"it plausibly could")
    print(f"    baseline                 {n_correct}/{n_answerable} "
          f"({n_correct/n_answerable:.1%})")
    running = n_correct
    for iv in ("glossary", "schema_detail", "few_shot", "self_repair"):
        running += by_iv.get(iv, 0)
        print(f"    + {iv:<22} {running}/{n_answerable} "
              f"({running/n_answerable:.1%})")
    unfixable = by_iv.get("none", 0)
    print(f"    unfixable by any component: {unfixable} "
          f"(gold errors and defensible disagreements)")

    multi = [e["id"] for e in answerable
             if e["cause"] != e["suggested_cause"]
             and e["suggested_cause"] != "unclear"]
    if len(multi) > len(answerable) // 3:
        print(f"\n  NOTE: {len(multi)}/{len(answerable)} classifications "
              f"differ from the suggestion.")
        print("  suggest() is an ordered chain, first match wins, so a query "
              "with two")
        print("  causes is labelled by whichever is checked first. Individual "
              "cause")
        print("  counts therefore understate; intervention totals are less "
              "affected.")

    gold_wrong = [e["id"] for e in filled if e["cause"] == "gold_wrong"]
    if gold_wrong:
        print(f"\n  GOLD ERRORS TO FIX in eval/gold/: {', '.join(gold_wrong)}")
    disagree = [e["id"] for e in filled
                if e["cause"] == "defensible_disagreement"]
    if disagree:
        print(f"  Defensible disagreements (document, do not fix): "
              f"{', '.join(disagree)}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--build", action="store_true")
    ap.add_argument("--summarise", action="store_true")
    ap.add_argument("--run", default=None, help="run JSON; default is newest")
    args = ap.parse_args()

    if args.summarise:
        summarise()
        return 0

    if args.build or not OUT_PATH.exists():
        runs = sorted(glob.glob(str(RUNS_DIR / "*.json")))
        if not runs:
            raise SystemExit(f"no runs in {RUNS_DIR}")
        build(Path(args.run) if args.run else Path(runs[-1]))
        return 0

    ap.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
