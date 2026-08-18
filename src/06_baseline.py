"""
Stage 6 - the naive baseline.

The first real number. Everything from here is measured against it.

The prompt is deliberately unhelpful: the full schema DDL, the question, and
nothing else. No glossary, no examples, no column descriptions, no retry. That
is the point - later stages add one component at a time and the delta is the
component's value. A baseline tuned to look good makes every subsequent gain
look small.

One thing the prompt does allow, which most text-to-SQL baselines do not: the
model may decline. Twelve of the gold questions have no correct answer, and a
system that cannot say "I don't know" gets them all wrong by construction.
Giving it the option from the baseline onward means abstention is measured
rather than assumed impossible.

Metrics reported:
  execution accuracy - correct / answerable
  silent error rate  - ran, returned a clean answer, answer was wrong
  visible error rate - crashed, was blocked, or declined
  abstention         - behaviour on the 12 questions with no correct answer

Silent error rate is the one a business cares about. Almost nobody reports it.

Run:
    python src/06_baseline.py --model <slug>
    python src/06_baseline.py --model <slug> --limit 10     # smoke test
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import evaluator  # noqa: E402
import gold as goldmod  # noqa: E402
import llm  # noqa: E402
import sandbox  # noqa: E402

SCHEMA_PATH = Path("reports/schema_ddl.sql")
RUNS_DIR = Path("reports/runs")
ABSTAIN = "CANNOT_ANSWER"

SYSTEM = f"""You write DuckDB SQL.

Given a database schema and a question, reply with a single SQL SELECT \
statement and nothing else. No explanation, no markdown fences, no commentary.

If the question cannot be answered from this schema, or is too ambiguous to \
answer without guessing, reply with exactly:
{ABSTAIN}: <one short sentence saying why>

Do not guess when the question is unclear. An honest refusal is better than a \
confident wrong answer."""


def build_prompt(schema_ddl: str, question: str) -> list[dict]:
    """Naive baseline prompt: whole schema, question, nothing else."""
    user = f"Schema:\n\n{schema_ddl}\n\nQuestion: {question}"
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": user}]


def extract_sql(text: str) -> tuple[str | None, str, str]:
    """Return (sql, reason, kind) where kind is sql | abstain | empty.

    An empty response is NOT an abstention. A model that returns nothing has
    failed; a model that says CANNOT_ANSWER has made a judgement. Collapsing
    the two credits silence as principled refusal, which inflates the
    abstention figure and hides a token-budget problem as a model decision.
    """
    body = text.strip()
    if not body:
        return None, "empty response from model", "empty"

    if body.upper().startswith(ABSTAIN):
        reason = body[len(ABSTAIN):].lstrip(": ").strip()
        return None, reason or "(no reason given)", "abstain"

    # Models wrap SQL in fences despite being told not to. Counting that as a
    # failure would measure instruction-following, not SQL ability.
    fence = re.search(r"```(?:sql)?\s*(.+?)```", body, re.S | re.I)
    if fence:
        body = fence.group(1).strip()

    if ABSTAIN in body.upper():
        return None, "(abstained mid-response)", "abstain"
    if not body:
        return None, "empty after fence stripping", "empty"
    return body, "", "sql"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--limit", type=int, default=None,
                    help="run only the first N questions - use for smoke tests")
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--provider", default=None,
                    help="pin one provider. Strongly recommended for any run "
                         "whose numbers will be compared against another.")
    ap.add_argument("--max-tokens", type=int, default=4000,
                    help="Reasoning models spend this budget thinking before "
                         "answering; too low returns an empty response.")
    ap.add_argument("--price-in", type=float, default=None,
                    help="USD per million input tokens, for cost estimation "
                         "when the provider does not report cost.")
    ap.add_argument("--price-out", type=float, default=None,
                    help="USD per million output tokens.")
    ap.add_argument("--pace", type=float, default=1.5,
                    help="seconds to wait before each uncached call. Raise if "
                         "you see 429s; cache hits are unaffected.")
    ap.add_argument("--tag", default="baseline")
    args = ap.parse_args()

    if not SCHEMA_PATH.exists():
        raise SystemExit(f"{SCHEMA_PATH} missing - run src/01_build_warehouse.py")
    schema_ddl = SCHEMA_PATH.read_text(encoding="utf-8")

    questions, problems = goldmod.load_gold(verified_only=True)
    if problems:
        raise SystemExit(f"gold set has {len(problems)} schema problems")
    if args.limit:
        questions = questions[:args.limit]

    con = sandbox.connect()
    cache = llm.Cache()
    gold_cache = evaluator.GoldCache(con)

    verdicts: list[evaluator.Verdict] = []
    abstention: list[dict] = []
    records: list[dict] = []
    n_cached = 0
    n_llm_failed = 0
    tok_in = tok_out = tok_reason = 0
    n_truncated = 0
    empties: list[tuple[str, str, int, int]] = []
    total_cost = 0.0
    cost_known = True

    print("=" * 78)
    print(f"STAGE 6 BASELINE   model={args.model}   temp={args.temperature}")
    print("=" * 78)
    print(f"  schema {len(schema_ddl):,} chars (~{len(schema_ddl)//4:,} tokens) "
          f"in every prompt")
    print(f"  {len(questions)} questions   cache holds {cache.count()}\n")

    for i, q in enumerate(questions, 1):
        res = llm.complete(build_prompt(schema_ddl, q.question),
                           model=args.model, temperature=args.temperature,
                           cache=cache, provider=args.provider,
                           pace_s=args.pace, max_tokens=args.max_tokens)
        n_cached += res.cached
        if res.cost is not None:
            total_cost += res.cost
        else:
            cost_known = False
        tok_in += res.prompt_tokens
        tok_out += res.completion_tokens
        tok_reason += res.reasoning_tokens
        n_truncated += res.finish_reason == "length"
        if not res.text.strip():
            empties.append((q.id, res.finish_reason, res.completion_tokens,
                            res.reasoning_chars))

        if not res.ok:
            n_llm_failed += 1
            print(f"  {i:>3} {q.id:<7} LLM FAILED: {res.error}")
            records.append({"id": q.id, "kind": q.kind, "llm_error": res.error})
            continue

        sql, reason, outcome = extract_sql(res.text)
        record = {
            "id": q.id, "kind": q.kind, "tier": q.tier,
            "traps": q.traps, "rules": q.rules,
            "question": q.question, "raw": res.text,
            "sql": sql, "abstain_reason": reason,
            "provider": res.provider, "resolved_model": res.model,
            "prompt_tokens": res.prompt_tokens,
            "completion_tokens": res.completion_tokens,
            "cost": res.cost, "llm_ms": res.elapsed_ms, "cached": res.cached,
            "reasoning_tokens": res.reasoning_tokens,
            "reasoning_chars": res.reasoning_chars,
            "finish_reason": res.finish_reason,
        }

        if q.kind != "answerable":
            # An empty response is not a decision, so it earns no credit.
            correct = outcome == "abstain"
            abstention.append({"id": q.id, "kind": q.kind, "correct": correct,
                               "reason": reason, "sql": sql})
            record["verdict"] = ("abstained" if correct
                                 else "empty" if outcome == "empty"
                                 else "answered_anyway")
            mark = "ok " if correct else "MISS"
            detail = (reason if correct else
                      "EMPTY RESPONSE" if outcome == "empty"
                      else "produced SQL")[:44]
            print(f"  {i:>3} {q.id:<7} {q.kind:<12} {mark}  {detail}")
        elif sql is None:
            kind = "error" if outcome == "empty" else "abstained"
            v = evaluator.Verdict(q.id, kind, reason)
            verdicts.append(v)
            record["verdict"] = kind
            print(f"  {i:>3} {q.id:<7} tier {q.tier}      VIS  {kind:<12} "
                  f"{reason[:36]}")
        else:
            v = evaluator.evaluate(q, sql, gold_cache)
            verdicts.append(v)
            record["verdict"] = v.kind
            record["detail"] = v.detail
            record["sql_ms"] = v.elapsed_ms
            flag = "OK  " if v.correct else ("SIL " if v.silent else "VIS ")
            print(f"  {i:>3} {q.id:<7} tier {q.tier}      {flag} {v.kind:<12} "
                  f"{v.detail[:36]}")

        records.append(record)

    con.close()
    cache.close()

    # ---- summary --------------------------------------------------------
    summary = evaluator.RunSummary(verdicts)
    print()
    print("=" * 78)
    print("RESULTS")
    print("=" * 78)
    if n_llm_failed:
        print(f"  *** {n_llm_failed} calls never reached the model. These are "
              f"absent from every")
        print(f"  *** figure below, so the accuracy shown is over a partial "
              f"set. Re-run;")
        print(f"  *** cached answers are free, only the missing ones are "
              f"requested.\n")
    print(f"  answerable questions      {summary.n}")
    print(f"  execution accuracy        {summary.execution_accuracy:.1%}")
    print(f"  silent error rate         {summary.silent_error_rate:.1%}"
          f"   <- ran fine, answer wrong")
    print(f"  visible error rate        {summary.visible_error_rate:.1%}")

    print("\n  by verdict")
    for kind, n in sorted(summary.by_kind().items(), key=lambda kv: -kv[1]):
        print(f"    {kind:<16} {n:>4}")

    print("\n  by tier")
    by_id = {q.id: q for q in questions}
    for tier in (1, 2, 3, 4):
        rows = [v for v in verdicts if by_id[v.question_id].tier == tier]
        if rows:
            acc = sum(v.correct for v in rows) / len(rows)
            print(f"    tier {tier}          {acc:>6.1%}   ({len(rows)} questions)")

    print("\n  by trap exposure")
    for label, want in (("carries a trap", True), ("no trap", False)):
        rows = [v for v in verdicts if bool(by_id[v.question_id].traps) is want]
        if rows:
            acc = sum(v.correct for v in rows) / len(rows)
            print(f"    {label:<16} {acc:>6.1%}   ({len(rows)} questions)")

    trap_fail = Counter(t for v in verdicts if not v.correct
                        for t in by_id[v.question_id].traps)
    if trap_fail:
        print("\n  failures by trap")
        for trap, n in trap_fail.most_common():
            print(f"    {trap:<26} {n:>4}")

    if abstention:
        right = sum(a["correct"] for a in abstention)
        print(f"\n  abstention (questions with no correct answer)")
        print(f"    declined correctly  {right}/{len(abstention)} "
              f"({right/len(abstention):.0%})")
        for a in abstention:
            if not a["correct"]:
                print(f"      {a['id']} ({a['kind']}) answered anyway")

    print("\n  cost and latency")
    print(f"    cache hits          {n_cached}/{len(questions)}")
    print(f"    tokens              {tok_in:,} in, {tok_out:,} out"
          + (f" ({tok_reason:,} reasoning)" if tok_reason else ""))
    if empties:
        print(f"\n  EMPTY RESPONSES ({len(empties)}) - why the model returned "
              f"nothing")
        print(f"    {'id':<8} {'finish_reason':<16} {'out_tok':>8} "
              f"{'reasoning_chars':>16}")
        for qid, fr, out, rc in empties:
            print(f"    {qid:<8} {fr or '(none)':<16} {out:>8} {rc:>16}")
        print("    finish_reason 'length' means truncation - raise --max-tokens.")
        print("    reasoning_chars > 0 with no text means the model answered")
        print("    in a reasoning channel the content field never received.")

    if n_truncated:
        print(f"    TRUNCATED           {n_truncated} responses hit the token "
              f"ceiling - raise --max-tokens")
    if cost_known and total_cost:
        print(f"    total cost          ${total_cost:.4f}")
        print(f"    per question        ${total_cost/max(len(questions),1):.5f}")
    elif args.price_in and args.price_out:
        est = tok_in / 1e6 * args.price_in + tok_out / 1e6 * args.price_out
        print(f"    estimated cost      ${est:.4f}   "
              f"(at ${args.price_in}/${args.price_out} per M, not billed data)")
        print(f"    per question        ${est/max(len(questions),1):.5f}")
    else:
        print("    total cost          not reported; pass --price-in/--price-out")
    providers = Counter(r.get("provider") for r in records if r.get("provider"))
    if providers:
        print(f"    providers           "
              f"{', '.join(f'{p} x{n}' for p, n in providers.items())}")
        if len(providers) > 1 and not args.provider:
            print("    WARNING: this run was spread across several providers.")
            print("    Deltas measured against another such run are partly")
            print("    routing noise. Re-run with --provider to pin it.")

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = RUNS_DIR / f"{args.tag}_{stamp}.json"
    out.write_text(json.dumps({
        "tag": args.tag, "model": args.model, "temperature": args.temperature,
        "provider_pinned": args.provider,
        "schema_chars": len(schema_ddl), "timestamp": stamp,
        "execution_accuracy": summary.execution_accuracy,
        "silent_error_rate": summary.silent_error_rate,
        "visible_error_rate": summary.visible_error_rate,
        "records": records,
    }, indent=2), encoding="utf-8")
    print(f"\n  written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
