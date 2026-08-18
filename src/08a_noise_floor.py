"""
Stage 8a - the noise floor.

Runs the identical prompt several times and reports the spread.

This exists because of an accident. Between two baseline runs the schema
tables were reordered from load order to alphabetical - the same characters,
the same information, nothing added or removed. Accuracy moved 2.7 points and
eight questions flipped in both directions.

Any ablation delta smaller than that is unreadable. Before measuring whether
schema detail is worth 2,400 extra tokens, we need to know how much the
number moves when nothing changes at all.

Temperature is already zero. The variation comes from the provider - batching,
kernel selection, hardware - and cannot be removed, only quantified. Repeat
runs use an identical prompt and differ only in a cache key, so each one is a
genuine fresh call.

    python src/08a_noise_floor.py --model <slug> --provider <name> --repeats 4

Report the noise floor alongside every later delta. A component that moves
accuracy by less than this range has not been shown to do anything.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import runner  # noqa: E402
import schema  # noqa: E402

OUT = Path("reports/noise_floor.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--repeats", type=int, default=4)
    ap.add_argument("--level", choices=schema.LEVELS, default="bare")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--pace", type=float, default=1.5)
    ap.add_argument("--price-in", type=float, default=None)
    ap.add_argument("--price-out", type=float, default=None)
    args = ap.parse_args()

    ddl = schema.build(args.level)

    def prompt_fn(q):
        return [{"role": "system", "content": runner.SYSTEM},
                {"role": "user",
                 "content": f"Schema:\n\n{ddl}\n\nQuestion: {q.question}"}]

    print("=" * 78)
    print(f"NOISE FLOOR   model={args.model}   schema={args.level}   "
          f"temp={args.temperature}")
    print("=" * 78)
    print("  Identical prompt every time. Any spread below is measurement")
    print("  noise, not an effect.\n")

    runs = []
    for i in range(args.repeats):
        res = runner.run_suite(
            tag=f"noise_{args.level}_{i}", prompt_fn=prompt_fn,
            model=args.model, provider=args.provider,
            temperature=args.temperature, max_tokens=args.max_tokens,
            pace=args.pace, cache_bust=i, quiet=True,
            extra_meta={"noise_run": i, "schema_level": args.level})
        s = res.summary
        cost = res.estimated_cost(args.price_in, args.price_out)
        correct = {v.question_id for v in res.verdicts if v.correct}
        runs.append({"i": i, "accuracy": s.execution_accuracy,
                     "correct": sorted(correct),
                     "n_correct": len(correct), "n": s.n,
                     "silent": s.silent_error_rate,
                     "abstention": res.abstention_rate,
                     "failed": res.n_llm_failed, "cost": cost})
        print(f"  run {i}   accuracy {s.execution_accuracy:>6.1%}   "
              f"{len(correct)}/{s.n} correct   "
              f"silent {s.silent_error_rate:>5.1%}   "
              f"abstention {res.abstention_rate:>4.0%}"
              + (f"   ${cost:.4f}" if cost is not None else "")
              + (f"   ({res.n_llm_failed} calls failed)"
                 if res.n_llm_failed else ""))

    # A run with failed calls scored over a smaller set - 34/72 is not
    # comparable with 39/75, and its abstention rate is computed over fewer
    # questions too. Including it widens the apparent noise floor, which would
    # raise the bar a real effect has to clear.
    clean = [r for r in runs if not r["failed"]]
    dropped = [r for r in runs if r["failed"]]
    if dropped:
        print()
        for r in dropped:
            print(f"  EXCLUDED run {r['i']}: {r['failed']} calls failed, "
                  f"scored over {r['n']} questions not "
                  f"{max(x['n'] for x in runs)}")
        print("  Re-run the same command - cached calls are free and only the")
        print("  failures are retried. Raise --pace if it recurs.")
    if not clean:
        raise SystemExit("\nevery run had failed calls; nothing to report")

    accs = [r["accuracy"] for r in clean]
    spread = max(accs) - min(accs)
    stdev = statistics.stdev(accs) if len(accs) > 1 else 0.0

    print()
    print("=" * 78)
    print("SPREAD WITH NOTHING CHANGED")
    print("=" * 78)
    print(f"  runs used   {len(clean)} of {len(runs)}")
    print(f"  mean        {statistics.mean(accs):.1%}")
    print(f"  min - max   {min(accs):.1%} - {max(accs):.1%}")
    print(f"  range       {spread:.1%}")
    print(f"  stdev       {stdev:.1%}")

    # Which questions are unstable. A question that flips between runs is not
    # evidence about anything, and a component that appears to fix one may
    # simply have caught it on a good day.
    counts = Counter()
    for r in clean:
        counts.update(r["correct"])
    always = [q for q, c in counts.items() if c == len(clean)]
    unstable = sorted(q for q, c in counts.items() if 0 < c < len(clean))

    print(f"\n  stable correct    {len(always)} questions correct every time")
    print(f"  unstable          {len(unstable)} questions flipped between runs")
    if unstable:
        print("    " + ", ".join(f"{q}({counts[q]}/{len(clean)})"
                                 for q in unstable))

    print()
    print("  Report this range beside every ablation delta. A component that")
    print(f"  moves accuracy by less than {spread:.1%} has not been shown to")
    print("  do anything, and the unstable questions above should be treated")
    print("  as uninformative individually.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "model": args.model, "provider": args.provider,
        "level": args.level, "temperature": args.temperature,
        "repeats": args.repeats, "runs_used": len(clean),
        "runs_excluded": [r["i"] for r in dropped],
        "mean": statistics.mean(accs), "min": min(accs), "max": max(accs),
        "range": spread, "stdev": stdev,
        "stable_correct": sorted(always), "unstable": unstable,
        "runs": runs,
    }, indent=2), encoding="utf-8")
    print(f"\n  written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
