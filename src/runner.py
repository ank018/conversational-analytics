"""
The run loop, shared by every stage that scores the gold set.

Stages 6 to 11 each ask the same question of a different prompt: how many
answers are right, how many are silently wrong, how many questions with no
answer were correctly declined. If each stage carried its own copy of the
loop the runs would drift apart, and a difference between two numbers would
no longer mean a difference between two prompts.

A stage supplies only a `prompt_fn(question) -> messages`. Everything else -
extraction, scoring, abstention handling, token accounting - happens here.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
import evaluator  # noqa: E402
import gold as goldmod  # noqa: E402
import llm  # noqa: E402
import sandbox  # noqa: E402

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


def extract_sql(text: str) -> tuple[str | None, str, str]:
    """Return (sql, reason, kind) where kind is sql | abstain | empty.

    An empty response is NOT an abstention. A model that returns nothing has
    failed; one that says CANNOT_ANSWER has made a judgement.
    """
    body = text.strip()
    if not body:
        return None, "empty response from model", "empty"
    if body.upper().startswith(ABSTAIN):
        reason = body[len(ABSTAIN):].lstrip(": ").strip()
        return None, reason or "(no reason given)", "abstain"

    # Models fence their SQL despite being told not to. Failing that would
    # measure instruction-following, not SQL.
    fence = re.search(r"```(?:sql)?\s*(.+?)```", body, re.S | re.I)
    if fence:
        body = fence.group(1).strip()
    if ABSTAIN in body.upper():
        return None, "(abstained mid-response)", "abstain"
    if not body:
        return None, "empty after fence stripping", "empty"
    return body, "", "sql"


@dataclass
class SuiteResult:
    tag: str
    model: str
    provider: str | None
    verdicts: list[evaluator.Verdict] = field(default_factory=list)
    abstention: list[dict] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    prompt_chars: int = 0
    tok_in: int = 0
    tok_out: int = 0
    tok_reason: int = 0
    cost: float = 0.0
    cost_reported: bool = True
    n_cached: int = 0
    n_llm_failed: int = 0
    n_truncated: int = 0
    empties: list[tuple] = field(default_factory=list)
    repairs: list[dict] = field(default_factory=list)
    path: Path | None = None

    @property
    def summary(self) -> evaluator.RunSummary:
        return evaluator.RunSummary(self.verdicts)

    @property
    def accuracy(self) -> float:
        return self.summary.execution_accuracy

    @property
    def abstention_rate(self) -> float:
        if not self.abstention:
            return 0.0
        return sum(a["correct"] for a in self.abstention) / len(self.abstention)

    def estimated_cost(self, price_in: float | None,
                       price_out: float | None) -> float | None:
        if self.cost_reported and self.cost:
            return self.cost
        if price_in and price_out:
            return self.tok_in / 1e6 * price_in + self.tok_out / 1e6 * price_out
        return None


def run_suite(tag: str,
              prompt_fn: Callable[[goldmod.GoldQuestion], list[dict]],
              model: str,
              provider: str | None = None,
              temperature: float = 0.0,
              max_tokens: int = 4000,
              pace: float = 1.5,
              limit: int | None = None,
              cache_bust: int = 0,
              max_attempts: int = 1,
              repair_prompt_fn=None,
              quiet: bool = False,
              extra_meta: dict | None = None) -> SuiteResult:
    questions, problems = goldmod.load_gold(verified_only=True)
    if problems:
        raise SystemExit(f"gold set has {len(problems)} schema problems")
    if limit:
        questions = questions[:limit]

    con = sandbox.connect()
    cache = llm.Cache()
    gold_cache = evaluator.GoldCache(con)
    res = SuiteResult(tag=tag, model=model, provider=provider)

    for i, q in enumerate(questions, 1):
        messages = prompt_fn(q)
        res.prompt_chars = max(res.prompt_chars,
                               sum(len(m["content"]) for m in messages))
        out = llm.complete(messages, model=model, temperature=temperature,
                           cache=cache, provider=provider, pace_s=pace,
                           max_tokens=max_tokens, cache_bust=cache_bust)
        res.n_cached += out.cached
        if out.cost is not None:
            res.cost += out.cost
        else:
            res.cost_reported = False
        res.tok_in += out.prompt_tokens
        res.tok_out += out.completion_tokens
        res.tok_reason += out.reasoning_tokens
        res.n_truncated += out.finish_reason == "length"

        if not out.ok:
            res.n_llm_failed += 1
            if not quiet:
                print(f"  {i:>3} {q.id:<7} LLM FAILED: {out.error}")
            res.records.append({"id": q.id, "kind": q.kind,
                                "llm_error": out.error})
            continue

        if not out.text.strip():
            res.empties.append((q.id, out.finish_reason,
                                out.completion_tokens, out.reasoning_chars))

        sql, reason, outcome = extract_sql(out.text)
        rec = {"id": q.id, "kind": q.kind, "tier": q.tier,
               "traps": q.traps, "rules": q.rules, "question": q.question,
               "raw": out.text, "sql": sql, "abstain_reason": reason,
               "provider": out.provider, "resolved_model": out.model,
               "prompt_tokens": out.prompt_tokens,
               "completion_tokens": out.completion_tokens,
               "reasoning_tokens": out.reasoning_tokens,
               "finish_reason": out.finish_reason,
               "cost": out.cost, "llm_ms": out.elapsed_ms,
               "cached": out.cached}

        if q.kind != "answerable":
            correct = outcome == "abstain"
            res.abstention.append({"id": q.id, "kind": q.kind,
                                   "correct": correct, "reason": reason})
            rec["verdict"] = ("abstained" if correct
                              else "empty" if outcome == "empty"
                              else "answered_anyway")
            if not quiet:
                mark = "ok " if correct else "MISS"
                detail = (reason if correct else "EMPTY RESPONSE"
                          if outcome == "empty" else "produced SQL")[:44]
                print(f"  {i:>3} {q.id:<7} {q.kind:<12} {mark}  {detail}")
        elif sql is None:
            kind = "error" if outcome == "empty" else "abstained"
            v = evaluator.Verdict(q.id, kind, reason)
            res.verdicts.append(v)
            rec["verdict"] = kind
            if not quiet:
                print(f"  {i:>3} {q.id:<7} tier {q.tier}      VIS  "
                      f"{kind:<12} {reason[:36]}")
        else:
            # Self-repair. The trigger must be something the system can see
            # WITHOUT the gold answer: the query crashed, or it returned no
            # rows. A query that runs and returns a plausible wrong number is
            # indistinguishable from a correct one at this point, which is
            # precisely why repair cannot touch silent errors.
            attempts = 1
            if max_attempts > 1 and repair_prompt_fn is not None:
                for attempt in range(2, max_attempts + 1):
                    probe = sandbox.run(sql, con=con)
                    if probe.ok and probe.rows:
                        break
                    trigger = (probe.error_kind if not probe.ok
                               else "empty_result")
                    fix = llm.complete(
                        repair_prompt_fn(q, sql, probe, attempt),
                        model=model, temperature=temperature, cache=cache,
                        provider=provider, pace_s=pace, max_tokens=max_tokens,
                        cache_bust=cache_bust * 1000 + attempt)
                    res.tok_in += fix.prompt_tokens
                    res.tok_out += fix.completion_tokens
                    res.tok_reason += fix.reasoning_tokens
                    if fix.cost is not None:
                        res.cost += fix.cost
                    res.n_cached += fix.cached
                    if not fix.ok:
                        break
                    new_sql, _, new_outcome = extract_sql(fix.text)
                    res.repairs.append({
                        "id": q.id, "attempt": attempt, "trigger": trigger,
                        "error": probe.error, "before": sql,
                        "after": new_sql, "accepted": new_outcome == "sql"})
                    if new_outcome != "sql":
                        break
                    sql = new_sql
                    attempts = attempt
            rec["attempts"] = attempts
            v = evaluator.evaluate(q, sql, gold_cache)
            rec["final_sql"] = sql
            res.verdicts.append(v)
            rec["verdict"] = v.kind
            rec["detail"] = v.detail
            rec["sql_ms"] = v.elapsed_ms
            if not quiet:
                flag = "OK  " if v.correct else ("SIL " if v.silent else "VIS ")
                print(f"  {i:>3} {q.id:<7} tier {q.tier}      {flag} "
                      f"{v.kind:<12} {v.detail[:36]}")

        res.records.append(rec)

    con.close()
    cache.close()

    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    res.path = RUNS_DIR / f"{tag}_{stamp}.json"
    res.path.write_text(json.dumps({
        "tag": tag, "model": model, "provider_pinned": provider,
        "temperature": temperature, "timestamp": stamp,
        "prompt_chars": res.prompt_chars,
        "execution_accuracy": res.accuracy,
        "silent_error_rate": res.summary.silent_error_rate,
        "visible_error_rate": res.summary.visible_error_rate,
        "abstention_rate": res.abstention_rate,
        **(extra_meta or {}),
        "records": res.records,
    }, indent=2), encoding="utf-8")
    return res


def print_summary(res: SuiteResult, price_in: float | None = None,
                  price_out: float | None = None) -> None:
    questions, _ = goldmod.load_gold(verified_only=True)
    by_id = {q.id: q for q in questions}
    s = res.summary

    print()
    print("=" * 78)
    print("RESULTS")
    print("=" * 78)
    if res.n_llm_failed:
        print(f"  *** {res.n_llm_failed} calls never reached the model and are "
              f"absent from every figure below. Re-run.\n")

    print(f"  answerable questions      {s.n}")
    print(f"  execution accuracy        {s.execution_accuracy:.1%}")
    print(f"  silent error rate         {s.silent_error_rate:.1%}"
          f"   <- ran fine, answer wrong")
    print(f"  visible error rate        {s.visible_error_rate:.1%}")

    print("\n  by verdict")
    for kind, n in sorted(s.by_kind().items(), key=lambda kv: -kv[1]):
        print(f"    {kind:<16} {n:>4}")

    print("\n  by tier")
    for tier in (1, 2, 3, 4):
        rows = [v for v in res.verdicts if by_id[v.question_id].tier == tier]
        if rows:
            print(f"    tier {tier}          "
                  f"{sum(v.correct for v in rows)/len(rows):>6.1%}   "
                  f"({len(rows)} questions)")

    print("\n  by trap exposure")
    for label, want in (("carries a trap", True), ("no trap", False)):
        rows = [v for v in res.verdicts
                if bool(by_id[v.question_id].traps) is want]
        if rows:
            print(f"    {label:<16} "
                  f"{sum(v.correct for v in rows)/len(rows):>6.1%}   "
                  f"({len(rows)} questions)")

    fails = Counter(t for v in res.verdicts if not v.correct
                    for t in by_id[v.question_id].traps)
    if fails:
        print("\n  failures by trap")
        for trap, n in fails.most_common():
            print(f"    {trap:<26} {n:>4}")

    rule_fails = Counter(r for v in res.verdicts if not v.correct
                         for r in by_id[v.question_id].rules)
    if rule_fails:
        print("\n  failures by rule invoked")
        for rule, n in rule_fails.most_common():
            total = sum(1 for v in res.verdicts
                        if rule in by_id[v.question_id].rules)
            print(f"    {rule:<26} {n:>4} of {total}")

    if res.abstention:
        right = sum(a["correct"] for a in res.abstention)
        print(f"\n  abstention (questions with no correct answer)")
        print(f"    declined correctly  {right}/{len(res.abstention)} "
              f"({right/len(res.abstention):.0%})")
        for a in res.abstention:
            if not a["correct"]:
                print(f"      {a['id']} ({a['kind']}) answered anyway")

    print("\n  cost and latency")
    print(f"    cache hits          {res.n_cached}/{len(res.records)}")
    print(f"    prompt size         ~{res.prompt_chars//4:,} tokens")
    print(f"    tokens              {res.tok_in:,} in, {res.tok_out:,} out"
          + (f" ({res.tok_reason:,} reasoning)" if res.tok_reason else ""))
    if res.n_truncated:
        print(f"    TRUNCATED           {res.n_truncated} hit the ceiling")
    est = res.estimated_cost(price_in, price_out)
    if est is not None:
        label = "total cost" if res.cost_reported and res.cost \
            else "estimated cost"
        print(f"    {label:<20}${est:.4f}")
        print(f"    per question        ${est/max(len(res.records),1):.5f}")
    else:
        print("    cost                not reported; pass --price-in/--price-out")

    if res.repairs:
        fired = {r["id"] for r in res.repairs}
        print(f"\n  self-repair")
        print(f"    triggered on       {len(fired)} questions "
              f"({len(res.repairs)} attempts)")
        for r in res.repairs:
            print(f"      {r['id']:<7} attempt {r['attempt']} "
                  f"trigger={r['trigger']:<14} "
                  f"{'new sql' if r['accepted'] else 'no usable sql'}")

    if res.empties:
        print(f"\n  EMPTY RESPONSES ({len(res.empties)})")
        for qid, fr, out_tok, rc in res.empties:
            print(f"    {qid:<8} finish={fr or '(none)':<10} out={out_tok:>5} "
                  f"reasoning_chars={rc}")

    print(f"\n  written to {res.path}")
