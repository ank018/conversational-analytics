"""
Stage 9 - what is the glossary worth?

The central experiment. Same schema, same questions, same model; the only
change is a page of business rules in the prompt.

Those rules come from docs/glossary.md, distilled from a semantics document
written at stage 2 - before any model had been run. None was written by
looking at which questions failed. A glossary fitted to the failure list
would produce a large gain that proves only that the answers had been seen.

The stage 7 taxonomy attributes 28 of 37 answerable failures to missing
business rules, predicting accuracy moves from ~50% to ~88%. The stage 8a
noise floor is 2.7 points. A gain below that has not been demonstrated; a
gain near 38 points would confirm the taxonomy; anything between is the
interesting case, and the size of the shortfall is the finding.

Each configuration is run several times because a single run carries ±2.7
points of noise. Comparing two single runs would be comparing two draws.

    python src/09_glossary.py --model <slug> --provider <name> --repeats 3
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gold as goldmod  # noqa: E402
import runner  # noqa: E402
import schema  # noqa: E402

GLOSSARY_PATH = Path("docs/glossary.md")
OUT = Path("reports/glossary_ablation.json")


def build_prompt_fn(ddl: str, glossary: str | None):
    def prompt_fn(q):
        parts = [f"Schema:\n\n{ddl}"]
        if glossary:
            parts.append(f"Business rules:\n\n{glossary}")
        parts.append(f"Question: {q.question}")
        return [{"role": "system", "content": runner.SYSTEM},
                {"role": "user", "content": "\n\n".join(parts)}]
    return prompt_fn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--level", choices=schema.LEVELS, default="bare")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--pace", type=float, default=3.0)
    ap.add_argument("--price-in", type=float, default=None)
    ap.add_argument("--price-out", type=float, default=None)
    args = ap.parse_args()

    if not GLOSSARY_PATH.exists():
        raise SystemExit(f"{GLOSSARY_PATH} not found")
    glossary = GLOSSARY_PATH.read_text(encoding="utf-8")
    ddl = schema.build(args.level)

    questions, _ = goldmod.load_gold(verified_only=True)
    by_id = {q.id: q for q in questions}

    configs = [("without glossary", None), ("with glossary", glossary)]
    results = {}

    print("=" * 78)
    print(f"STAGE 9 GLOSSARY   model={args.model}   schema={args.level}")
    print("=" * 78)
    print(f"  glossary {len(glossary):,} chars (~{len(glossary)//4:,} tokens)")
    print(f"  {args.repeats} repeats per configuration; noise floor is 2.7 "
          f"points\n")

    for label, gloss in configs:
        runs = []
        for i in range(args.repeats):
            res = runner.run_suite(
                tag=f"glossary_{'on' if gloss else 'off'}_{i}",
                prompt_fn=build_prompt_fn(ddl, gloss),
                model=args.model, provider=args.provider,
                temperature=args.temperature, max_tokens=args.max_tokens,
                pace=args.pace, cache_bust=i, quiet=True,
                extra_meta={"glossary": bool(gloss), "repeat": i,
                            "schema_level": args.level})
            s = res.summary
            cost = res.estimated_cost(args.price_in, args.price_out)
            runs.append({
                "i": i, "accuracy": s.execution_accuracy,
                "silent": s.silent_error_rate,
                "visible": s.visible_error_rate,
                "abstention_correct": sum(a["correct"] for a in res.abstention),
                "abstention_n": len(res.abstention),
                "correct": sorted(v.question_id for v in res.verdicts
                                  if v.correct),
                "failed": res.n_llm_failed, "n": s.n,
                "tok_in": res.tok_in, "cost": cost,
                "rule_failures": dict(Counter(
                    r for v in res.verdicts if not v.correct
                    for r in by_id[v.question_id].rules)),
            })
            flag = f"  ({res.n_llm_failed} calls failed)" if res.n_llm_failed else ""
            print(f"  {label:<18} run {i}   {s.execution_accuracy:>6.1%}   "
                  f"silent {s.silent_error_rate:>5.1%}   "
                  f"abstain {runs[-1]['abstention_correct']}/"
                  f"{runs[-1]['abstention_n']}"
                  + (f"   ${cost:.4f}" if cost is not None else "") + flag)
        results[label] = [r for r in runs if not r["failed"]] or runs
        print()

    off, on = results["without glossary"], results["with glossary"]
    m_off = statistics.mean(r["accuracy"] for r in off)
    m_on = statistics.mean(r["accuracy"] for r in on)
    delta = m_on - m_off

    print("=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"  {'configuration':<20} {'accuracy':>10} {'silent':>9} "
          f"{'prompt tok':>11}")
    for label, runs in (("without glossary", off), ("with glossary", on)):
        acc = statistics.mean(r["accuracy"] for r in runs)
        sil = statistics.mean(r["silent"] for r in runs)
        tok = statistics.mean(r["tok_in"] for r in runs) / max(
            statistics.mean(r["n"] for r in runs), 1)
        print(f"  {label:<20} {acc:>9.1%} {sil:>8.1%} {tok:>11,.0f}")

    print(f"\n  delta                {delta:>+9.1%}")
    print(f"  noise floor          {2.7:>9.1f}%")
    verdict = ("BELOW the noise floor - not demonstrated"
               if abs(delta) < 0.027 else
               f"{abs(delta)/0.027:.1f}x the noise floor")
    print(f"  {verdict}")
    print(f"\n  taxonomy predicted   +37.3% (50.7% -> 88.0%)")
    print(f"  shortfall            {delta - 0.373:>+9.1%}")

    # Which rules stopped being violated. The headline delta says whether the
    # glossary works; this says which sentence of it did the work.
    print("\n  failures by rule invoked")
    print(f"    {'rule':<26} {'without':>9} {'with':>7} {'change':>9}")
    rules = set()
    for runs in (off, on):
        for r in runs:
            rules |= set(r["rule_failures"])
    for rule in sorted(rules):
        a = statistics.mean(r["rule_failures"].get(rule, 0) for r in off)
        b = statistics.mean(r["rule_failures"].get(rule, 0) for r in on)
        print(f"    {rule:<26} {a:>9.1f} {b:>7.1f} {b - a:>+9.1f}")

    stable_off = {q for q in off[0]["correct"]
                  if all(q in r["correct"] for r in off)}
    stable_on = {q for q in on[0]["correct"]
                 if all(q in r["correct"] for r in on)}
    gained, lost = sorted(stable_on - stable_off), sorted(stable_off - stable_on)
    print(f"\n  questions correct in EVERY run")
    print(f"    without glossary  {len(stable_off)}")
    print(f"    with glossary     {len(stable_on)}")
    print(f"    gained ({len(gained)}): {', '.join(gained) or 'none'}")
    print(f"    lost   ({len(lost)}): {', '.join(lost) or 'none'}")
    if lost:
        print("    Questions lost matter as much as questions gained - a rule")
        print("    can be applied where it does not belong.")

    a_off = statistics.mean(r["abstention_correct"] for r in off)
    a_on = statistics.mean(r["abstention_correct"] for r in on)
    print(f"\n  abstention (of {off[0]['abstention_n']} no-answer questions)")
    print(f"    without glossary  {a_off:.1f}")
    print(f"    with glossary     {a_on:.1f}")
    print("    This metric swung 7-10 of 11 across identical runs at stage 8a,")
    print("    so treat a change of one or two as uninformative.")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "model": args.model, "provider": args.provider,
        "schema_level": args.level, "repeats": args.repeats,
        "glossary_chars": len(glossary),
        "mean_accuracy_off": m_off, "mean_accuracy_on": m_on,
        "delta": delta, "noise_floor": 0.027,
        "runs": results,
    }, indent=2), encoding="utf-8")
    print(f"\n  written to {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
