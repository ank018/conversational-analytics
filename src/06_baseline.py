"""
Stage 6 - the naive baseline.

The first real number; everything after is measured against it.

The prompt is deliberately unhelpful: the bare schema, the question, nothing
else. No glossary, no examples, no column descriptions, no retry. Later
stages add one component at a time and the delta is that component's value.
A baseline tuned to look good makes every subsequent gain look small.

One thing this prompt does allow, which most text-to-SQL baselines do not:
the model may decline. Eleven gold questions have no correct answer, and a
system that cannot say "I don't know" fails them by construction.

    python src/06_baseline.py --model <slug> --provider <name>
    python src/06_baseline.py --model <slug> --limit 10      # smoke test
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import runner  # noqa: E402
import schema  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default=None,
                    help="pin one provider. Strongly recommended for any run "
                         "whose numbers will be compared against another.")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--pace", type=float, default=1.5)
    ap.add_argument("--price-in", type=float, default=None)
    ap.add_argument("--price-out", type=float, default=None)
    ap.add_argument("--level", choices=schema.LEVELS, default="bare",
                    help="schema detail level; the baseline is 'bare'")
    ap.add_argument("--tag", default="baseline")
    args = ap.parse_args()

    ddl = schema.build(args.level)

    def prompt_fn(q):
        return [{"role": "system", "content": runner.SYSTEM},
                {"role": "user",
                 "content": f"Schema:\n\n{ddl}\n\nQuestion: {q.question}"}]

    print("=" * 78)
    print(f"STAGE 6 BASELINE   model={args.model}   schema={args.level}")
    print("=" * 78)
    print(f"  schema {len(ddl):,} chars (~{len(ddl)//4:,} tokens) in every "
          f"prompt\n")

    res = runner.run_suite(
        tag=args.tag, prompt_fn=prompt_fn, model=args.model,
        provider=args.provider, temperature=args.temperature,
        max_tokens=args.max_tokens, pace=args.pace, limit=args.limit,
        extra_meta={"schema_level": args.level, "schema_chars": len(ddl)})

    runner.print_summary(res, args.price_in, args.price_out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
