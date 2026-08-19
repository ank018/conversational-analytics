"""
Stage 10 - does execution self-repair help?

Run the generated SQL. If it fails, show the model its own query and the
error, and let it try again. Retry loops of this shape are everywhere in
agentic tooling and are widely assumed to be worth having.

The constraint that decides the result: **the repair trigger must be
something the system can detect without knowing the answer.** Two signals
qualify - the query raised an error, or it returned no rows. A query that
runs cleanly and returns a plausible wrong number offers nothing to trigger
on. That is what "silent" means.

With the glossary in place the split is:

    execution accuracy   85.8%
    silent errors        13.3%    <- repair cannot see these
    visible errors        0.9%    <- all repair can possibly fix

So the predicted ceiling is under one question in 75, comfortably inside the
2.7-point noise floor. Written down before the run: **no measurable effect.**

If that holds it is a useful null. Retry loops fix syntax; they do nothing
once the failures are semantic. A team whose text-to-SQL system is quietly
wrong 13% of the time will not be helped by adding retries, and this measures
that rather than asserting it.

Cheap to run: the first attempt on every question is the identical glossary
prompt, so it comes from cache. Only repair attempts cost anything.

    python src/10_self_repair.py --model <slug> --provider <name> --repeats 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import runner  # noqa: E402
import schema  # noqa: E402

GLOSSARY_PATH = Path("docs/glossary.md")
OUT = Path("reports/self_repair.json")


def build_prompt_fn(ddl: str, glossary: str):
    def prompt_fn(q):
        return [{"role": "system", "content": runner.SYSTEM},
                {"role": "user", "content":
                    f"Schema:\n\n{ddl}\n\nBusiness rules:\n\n{glossary}\n\n"
                    f"Question: {q.question}"}]
    return prompt_fn


def build_repair_fn(ddl: str, glossary: str):
    def repair_prompt_fn(q, sql, probe, attempt):
        if probe.ok:
            problem = ("The query ran but returned no rows. That is almost "
                       "certainly wrong for this question.")
        else:
            problem = f"The query failed with: {probe.error}"
        return [
            {"role": "system", "content": runner.SYSTEM},
            {"role": "user", "content":
                f"Schema:\n\n{ddl}\n\nBusiness rules:\n\n{glossary}\n\n"
                f"Question: {q.question}"},
            {"role": "assistant", "content": sql},
            {"role": "user", "content":
                f"{problem}\n\nRewrite the query. Reply with SQL only."},
        ]
    return repair_prompt_fn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--attempts", type=int, default=3,
                    help="total attempts per question, including the first")
    ap.add_argument("--level", choices=schema.LEVELS, default="bare")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--pace", type=float, default=3.0)
    ap.add_argument("--price-in", type=float, default=None)
    ap.add_argument("--price-out", type=float, default=None)
    args = ap.parse_args()

    glossary = GLOSSARY_PATH.read_text(encoding="utf-8")
    ddl = schema.build(args.level)
    prompt_fn = build_prompt_fn(ddl, glossary)
    repair_fn = build_repair_fn(ddl, glossary)

    print("=" * 78)
    print(f"STAGE 10 SELF-REPAIR   model={args.model}   "
          f"attempts={args.attempts}")
    print("=" * 78)
    print("  Repair triggers on: query error, or a result with no rows.")
    print("  Neither signal exists for a query that returns a wrong number.\n")

    configs = [("glossary only", 1), (f"+ repair (x{args.attempts})",
                                      args.attempts)]
    results = {}

    for label, attempts in configs:
        runs = []
        for i in range(args.repeats):
            res = runner.run_suite(
                tag=f"repair_{attempts}_{i}", prompt_fn=prompt_fn,
                model=args.model, provider=args.provider,
                temperature=args.temperature, max_tokens=args.max_tokens,
                pace=args.pace, cache_bust=i, quiet=True,
                max_attempts=attempts,
                repair_prompt_fn=repair_fn if attempts > 1 else None,
                extra_meta={"max_attempts": attempts, "repeat": i})
            s = res.summary
            cost = res.estimated_cost(args.price_in, args.price_out)
            fired = {r["id"] for r in res.repairs}
            runs.append({
                "i": i, "accuracy": s.execution_accuracy,
                "silent": s.silent_error_rate,
                "visible": s.visible_error_rate,
                "correct": sorted(v.question_id for v in res.verdicts
                                  if v.correct),
                "repairs_fired": sorted(fired),
                "repair_attempts": len(res.repairs),
                "failed": res.n_llm_failed, "n": s.n, "cost": cost,
            })
            print(f"  {label:<22} run {i}   {s.execution_accuracy:>6.1%}   "
                  f"silent {s.silent_error_rate:>5.1%}   "
                  f"visible {s.visible_error_rate:>5.1%}   "
                  f"repaired {len(fired):>2}"
                  + (f"   ${cost:.4f}" if cost is not None else "")
                  + (f"   ({res.n_llm_failed} failed)"
                     if res.n_llm_failed else ""))
        results[label] = [r for r in runs if not r["failed"]] or runs
        print()

    base_label, rep_label = configs[0][0], configs[1][0]
    base, rep = results[base_label], results[rep_label]
    m_base = statistics.mean(r["accuracy"] for r in base)
    m_rep = statistics.mean(r["accuracy"] for r in rep)
    delta = m_rep - m_base

    print("=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"  {'configuration':<24} {'accuracy':>10} {'silent':>9} "
          f"{'visible':>9}")
    for label, runs in ((base_label, base), (rep_label, rep)):
        print(f"  {label:<24} "
              f"{statistics.mean(r['accuracy'] for r in runs):>9.1%} "
              f"{statistics.mean(r['silent'] for r in runs):>8.1%} "
              f"{statistics.mean(r['visible'] for r in runs):>8.1%}")

    print(f"\n  delta                    {delta:>+9.1%}")
    print(f"  noise floor              {2.7:>9.1f}%")
    print("  " + ("BELOW the noise floor - no effect demonstrated"
                  if abs(delta) < 0.027
                  else f"{abs(delta)/0.027:.1f}x the noise floor"))

    fired = sorted({q for r in rep for q in r["repairs_fired"]})
    attempts_total = statistics.mean(r["repair_attempts"] for r in rep)
    print(f"\n  repair fired on {len(fired)} distinct questions, "
          f"{attempts_total:.1f} extra calls per run")
    if fired:
        print(f"    {', '.join(fired)}")

    silent_base = statistics.mean(r["silent"] for r in base)
    silent_rep = statistics.mean(r["silent"] for r in rep)
    print(f"\n  silent error rate        {silent_base:.1%} -> {silent_rep:.1%}")
    print("  Repair has no signal for a query that runs and returns a wrong")
    print("  number, so this is the rate it cannot reach by construction.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "model": args.model, "provider": args.provider,
        "attempts": args.attempts, "repeats": args.repeats,
        "mean_accuracy_base": m_base, "mean_accuracy_repair": m_rep,
        "delta": delta, "noise_floor": 0.027,
        "repairs_fired": fired, "runs": results,
    }, indent=2), encoding="utf-8")
    print(f"\n  written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
