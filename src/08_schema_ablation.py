"""
Stage 8 - does schema detail help?

Same questions, same model, same everything except how much the schema
describes itself:

  bare       column names and types
  keys       plus the primary and foreign keys that actually hold
  values     plus sample values, ranges and null counts
  described  plus factual notes on the ambiguous columns

The prediction, from the stage 7 taxonomy and written down before this runs:
**zero**. Not one of the 37 answerable failures was attributed to picking the
wrong column, so there is nothing here for schema detail to fix. Four levels
of increasingly expensive schema should produce a flat accuracy line and a
rising token bill.

That makes this the cheap test of whether the taxonomy can be trusted. Stage
9 is about to bet 28 failures on the same document's predictions. If schema
detail moves accuracy materially, the taxonomy misattributed something and
the glossary prediction deserves less confidence.

The most likely way the prediction fails: the `values` level lists the
distinct order_status values, including 'canceled' and 'unavailable'. If
merely seeing that those exist is enough to make the model exclude them, the
line moves. Knowing a value exists is not the same as knowing to filter it,
so I expect not - but that is the mechanism to watch.

    python src/08_schema_ablation.py --model <slug> --provider <name>
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import runner  # noqa: E402
import schema  # noqa: E402

OUT = Path("reports/schema_ablation.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--pace", type=float, default=1.5)
    ap.add_argument("--price-in", type=float, default=None)
    ap.add_argument("--price-out", type=float, default=None)
    ap.add_argument("--levels", nargs="+", default=list(schema.LEVELS),
                    choices=schema.LEVELS)
    ap.add_argument("--verbose", action="store_true",
                    help="print every question; off by default because four "
                         "levels is 340 lines of output")
    args = ap.parse_args()

    results = []
    for level in args.levels:
        ddl = schema.build(level)

        def prompt_fn(q, _ddl=ddl):
            return [{"role": "system", "content": runner.SYSTEM},
                    {"role": "user",
                     "content": f"Schema:\n\n{_ddl}\n\nQuestion: {q.question}"}]

        print("=" * 78)
        print(f"LEVEL: {level}   ({len(ddl):,} chars, ~{len(ddl)//4:,} tokens)")
        print("=" * 78)

        res = runner.run_suite(
            tag=f"schema_{level}", prompt_fn=prompt_fn, model=args.model,
            provider=args.provider, temperature=args.temperature,
            max_tokens=args.max_tokens, pace=args.pace,
            quiet=not args.verbose,
            extra_meta={"schema_level": level, "schema_chars": len(ddl)})

        s = res.summary
        cost = res.estimated_cost(args.price_in, args.price_out)
        print(f"  accuracy {s.execution_accuracy:>6.1%}   "
              f"silent {s.silent_error_rate:>6.1%}   "
              f"visible {s.visible_error_rate:>6.1%}   "
              f"abstention {res.abstention_rate:>5.0%}"
              + (f"   ${cost:.4f}" if cost is not None else ""))
        if res.n_llm_failed:
            print(f"  WARNING: {res.n_llm_failed} calls failed; this level's "
                  f"numbers are over a partial set")
        print()

        results.append({
            "level": level,
            "schema_chars": len(ddl),
            "schema_tokens": len(ddl) // 4,
            "accuracy": s.execution_accuracy,
            "silent": s.silent_error_rate,
            "visible": s.visible_error_rate,
            "abstention": res.abstention_rate,
            "correct": sum(v.correct for v in res.verdicts),
            "n": s.n,
            "tok_in": res.tok_in,
            "tok_out": res.tok_out,
            "cost": cost,
            "llm_failed": res.n_llm_failed,
            "run": str(res.path),
            "correct_ids": sorted(v.question_id for v in res.verdicts
                                  if v.correct),
        })

    print("=" * 78)
    print("SCHEMA ABLATION")
    print("=" * 78)
    print(f"  {'level':<12} {'tokens':>8} {'accuracy':>10} {'delta':>8} "
          f"{'silent':>8} {'cost':>9}")
    base = results[0]["accuracy"] if results else 0.0
    for r in results:
        delta = r["accuracy"] - base
        cost = f"${r['cost']:.4f}" if r["cost"] is not None else "-"
        print(f"  {r['level']:<12} {r['schema_tokens']:>8,} "
              f"{r['accuracy']:>9.1%} {delta:>+8.1%} "
              f"{r['silent']:>7.1%} {cost:>9}")

    # Which questions each level fixed or broke relative to bare. A level that
    # fixes three and breaks three nets to zero and looks inert; it is not.
    if len(results) > 1:
        first = set(results[0]["correct_ids"])
        print(f"\n  churn against '{results[0]['level']}'")
        for r in results[1:]:
            now = set(r["correct_ids"])
            gained, lost = sorted(now - first), sorted(first - now)
            print(f"    {r['level']:<12} +{len(gained)} -{len(lost)}"
                  + (f"   gained {', '.join(gained)}" if gained else "")
                  + (f"   lost {', '.join(lost)}" if lost else ""))
        print("\n  A net-zero delta made of offsetting gains and losses is not")
        print("  the same as no effect, and only the churn line shows it.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"model": args.model,
                               "provider": args.provider,
                               "levels": results}, indent=2),
                   encoding="utf-8")
    print(f"\n  written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
