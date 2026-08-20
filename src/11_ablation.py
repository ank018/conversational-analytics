"""
Stage 11 - the ablation table.

Runs every configuration with repeats and produces the table the README is
built on. Configurations already run at earlier stages come from cache and
cost nothing; only the ones never measured are paid for.

    baseline           bare schema, no rules
    schema             described schema (keys, sample values, column notes)
    glossary           bare schema plus the business rules
    schema+glossary    both
    repair             glossary plus execution feedback and retry

The four-cell design matters. Running only baseline and glossary tells you
what the rules are worth on a bare schema; it does not tell you whether
schema detail is worthless in general or merely redundant once the rules are
present. The schema+glossary cell is what separates those.

Predictions on record, from the stage 7 taxonomy:

    schema            zero. Not one failure was attributed to picking the
                      wrong column.
    glossary          +37.3 points. Measured at +36.0.
    repair            zero. Measured at zero - it never fired.

Every delta is reported against the stage 8a noise floor of 2.7 points. A
delta below that has not been demonstrated, however plausible it looks.

    python src/11_ablation.py --model <slug> --provider <name> --repeats 3
    python src/11_ablation.py ... --configs baseline glossary   # subset
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import gold as goldmod  # noqa: E402
import runner  # noqa: E402
import schema  # noqa: E402

GLOSSARY_PATH = Path("docs/glossary.md")
NOISE_PATH = Path("reports/noise_floor.json")
OUT_JSON = Path("reports/ablation.json")
OUT_MD = Path("reports/ablation.md")

# name -> (schema level, use glossary, max attempts)
CONFIGS: dict[str, tuple[str, bool, int]] = {
    "baseline": ("bare", False, 1),
    "schema": ("described", False, 1),
    "glossary": ("bare", True, 1),
    "schema+glossary": ("described", True, 1),
    "repair": ("bare", True, 3),
}


def build_prompt_fn(ddl: str, glossary: str | None):
    def prompt_fn(q):
        parts = [f"Schema:\n\n{ddl}"]
        if glossary:
            parts.append(f"Business rules:\n\n{glossary}")
        parts.append(f"Question: {q.question}")
        return [{"role": "system", "content": runner.SYSTEM},
                {"role": "user", "content": "\n\n".join(parts)}]
    return prompt_fn


def build_repair_fn(ddl: str, glossary: str):
    def repair_prompt_fn(q, sql, probe, attempt):
        problem = ("The query ran but returned no rows. That is almost "
                   "certainly wrong for this question." if probe.ok
                   else f"The query failed with: {probe.error}")
        return [{"role": "system", "content": runner.SYSTEM},
                {"role": "user", "content":
                    f"Schema:\n\n{ddl}\n\nBusiness rules:\n\n{glossary}\n\n"
                    f"Question: {q.question}"},
                {"role": "assistant", "content": sql},
                {"role": "user", "content":
                    f"{problem}\n\nRewrite the query. Reply with SQL only."}]
    return repair_prompt_fn


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--provider", default=None)
    ap.add_argument("--repeats", type=int, default=3)
    ap.add_argument("--configs", nargs="+", default=list(CONFIGS),
                    choices=list(CONFIGS))
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=4000)
    ap.add_argument("--pace", type=float, default=3.0)
    ap.add_argument("--price-in", type=float, default=None)
    ap.add_argument("--price-out", type=float, default=None)
    args = ap.parse_args()

    glossary_text = GLOSSARY_PATH.read_text(encoding="utf-8")
    noise = 0.027
    if NOISE_PATH.exists():
        noise = json.loads(NOISE_PATH.read_text(encoding="utf-8"))["range"]

    questions, _ = goldmod.load_gold(verified_only=True)
    n_answerable = sum(1 for q in questions if q.kind == "answerable")

    print("=" * 78)
    print(f"STAGE 11 ABLATION   model={args.model}   "
          f"repeats={args.repeats}   noise floor {noise:.1%}")
    print("=" * 78)
    print(f"  {n_answerable} answerable questions, "
          f"{len(questions) - n_answerable} with no correct answer\n")

    rows = []
    for name in args.configs:
        level, use_gloss, attempts = CONFIGS[name]
        ddl = schema.build(level)
        gloss = glossary_text if use_gloss else None
        prompt_fn = build_prompt_fn(ddl, gloss)
        repair_fn = build_repair_fn(ddl, glossary_text) if attempts > 1 else None

        runs = []
        for i in range(args.repeats):
            # Cache keys must match the earlier stages exactly, or a
            # configuration already paid for is charged again and - worse -
            # its numbers stop matching what was reported before.
            tag = {"baseline": f"noise_bare_{i}",
                   "glossary": f"glossary_on_{i}",
                   "repair": f"repair_3_{i}"}.get(name, f"abl_{name}_{i}")
            res = runner.run_suite(
                tag=tag, prompt_fn=prompt_fn, model=args.model,
                provider=args.provider, temperature=args.temperature,
                max_tokens=args.max_tokens, pace=args.pace, cache_bust=i,
                quiet=True, max_attempts=attempts,
                repair_prompt_fn=repair_fn,
                extra_meta={"config": name, "schema_level": level,
                            "glossary": use_gloss, "attempts": attempts})
            s = res.summary
            runs.append({
                "i": i, "accuracy": s.execution_accuracy,
                "silent": s.silent_error_rate,
                "visible": s.visible_error_rate,
                "abstention_correct": sum(a["correct"] for a in res.abstention),
                "abstention_n": len(res.abstention),
                "prompt_tokens": res.tok_in / max(len(res.records), 1),
                "cost": res.estimated_cost(args.price_in, args.price_out),
                "failed": res.n_llm_failed,
                "repairs": len({r["id"] for r in res.repairs}),
                "correct": sorted(v.question_id for v in res.verdicts
                                  if v.correct),
            })
            print(f"  {name:<18} run {i}   {s.execution_accuracy:>6.1%}   "
                  f"silent {s.silent_error_rate:>5.1%}"
                  + (f"   ({res.n_llm_failed} failed)"
                     if res.n_llm_failed else ""))

        good = [r for r in runs if not r["failed"]] or runs
        costs = [r["cost"] for r in good if r["cost"] is not None]
        rows.append({
            "config": name, "level": level, "glossary": use_gloss,
            "attempts": attempts, "runs": good, "excluded": len(runs) - len(good),
            "accuracy": statistics.mean(r["accuracy"] for r in good),
            "silent": statistics.mean(r["silent"] for r in good),
            "visible": statistics.mean(r["visible"] for r in good),
            "abstention": statistics.mean(r["abstention_correct"] for r in good),
            "abstention_n": good[0]["abstention_n"],
            "prompt_tokens": statistics.mean(r["prompt_tokens"] for r in good),
            "cost": statistics.mean(costs) if costs else None,
            "repairs": statistics.mean(r["repairs"] for r in good),
        })
        print()

    base = next((r for r in rows if r["config"] == "baseline"), rows[0])

    print("=" * 78)
    print("ABLATION")
    print("=" * 78)
    print(f"  {'configuration':<18} {'accuracy':>9} {'delta':>8} {'vs noise':>9} "
          f"{'silent':>8} {'tokens':>8} {'cost/q':>9}")
    for r in rows:
        d = r["accuracy"] - base["accuracy"]
        ratio = "-" if r is base else (
            f"{abs(d)/noise:.1f}x" if abs(d) >= noise else "below")
        cost = f"${r['cost']/75:.5f}" if r["cost"] else "-"
        print(f"  {r['config']:<18} {r['accuracy']:>8.1%} {d:>+8.1%} "
              f"{ratio:>9} {r['silent']:>7.1%} "
              f"{r['prompt_tokens']:>8,.0f} {cost:>9}")

    print(f"\n  noise floor {noise:.1%}. A delta below it has not been "
          f"demonstrated.")

    # Interaction: is schema detail worthless, or merely redundant once the
    # rules are present? Only the four-cell design answers that.
    have = {r["config"]: r for r in rows}
    if {"baseline", "schema", "glossary", "schema+glossary"} <= set(have):
        s_alone = have["schema"]["accuracy"] - have["baseline"]["accuracy"]
        s_on_top = have["schema+glossary"]["accuracy"] - have["glossary"]["accuracy"]
        print(f"\n  schema detail on a bare prompt      {s_alone:>+7.1%}")
        print(f"  schema detail on top of the rules   {s_on_top:>+7.1%}")
        if abs(s_alone) < noise and abs(s_on_top) < noise:
            print("  Neither clears the noise floor: describing the schema is")
            print("  not worth its tokens, with or without the rules.")

    OUT_JSON.parent.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(
        {"model": args.model, "provider": args.provider,
         "repeats": args.repeats, "noise_floor": noise,
         "n_answerable": n_answerable, "rows": rows}, indent=2),
        encoding="utf-8")

    md = ["# Ablation", "",
          f"Model `{args.model}` via {args.provider or 'any provider'}, "
          f"temperature {args.temperature}, {args.repeats} runs per "
          f"configuration, {n_answerable} answerable questions.", "",
          f"Noise floor **{noise:.1%}** - the spread across repeated runs of "
          f"an identical prompt. Any delta below that has not been "
          f"demonstrated.", "",
          "| configuration | accuracy | delta | silent errors | prompt tokens |",
          "|---|---:|---:|---:|---:|"]
    for r in rows:
        d = r["accuracy"] - base["accuracy"]
        md.append(f"| {r['config']} | {r['accuracy']:.1%} | {d:+.1f} pts | "
                  f"{r['silent']:.1%} | {r['prompt_tokens']:,.0f} |")
    md += ["",
           "`silent errors` are answers that ran without error and returned a "
           "clean, plausible, wrong result. They are invisible to the user, "
           "and they are the number a business should care about."]
    OUT_MD.write_text("\n".join(md) + "\n", encoding="utf-8")

    print(f"\n  written to {OUT_JSON} and {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
