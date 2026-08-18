"""
Gold set loading, validation and verification.

The gold set is the standard every later number is measured against, so it is
guarded harder than anything else in the repo:

- Ids must be unique across all files.
- An answerable question must carry SQL; an ambiguous or unsupported one must
  not. Writing SQL for a question whose correct behaviour is "ask what you
  mean" would smuggle an answer into a question that has none.
- Gold SQL must execute, and must return at least one row. An empty result is
  rejected because an empty result matches any other empty result - a model
  returning nonsense would score a point.
- `verified: false` means a human has not yet confirmed the SQL answers the
  question asked. Unverified questions are excluded from evaluation.

Library:  from gold import load_gold
Verifier: python src/gold.py

Requires: pip install pyyaml
"""

from __future__ import annotations

import sys
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).parent))
import sandbox  # noqa: E402

GOLD_DIR = Path("eval/gold")
KINDS = {"answerable", "ambiguous", "unsupported"}
TIERS = {1, 2, 3, 4}


@dataclass
class GoldQuestion:
    id: str
    question: str
    kind: str
    source_file: str
    tier: int | None = None
    sql: str | None = None
    ordered: bool = False
    tags: list[str] = field(default_factory=list)
    rules: list[str] = field(default_factory=list)
    traps: list[str] = field(default_factory=list)
    answer_columns: int | None = None
    notes: str = ""
    verified: bool = False


def _validate(q: dict, path: Path) -> list[str]:
    """Return a list of problems with one raw question dict."""
    problems = []
    qid = q.get("id", "<no id>")

    for required in ("id", "question", "kind"):
        if not q.get(required):
            problems.append(f"{qid}: missing '{required}'")

    kind = q.get("kind")
    if kind and kind not in KINDS:
        problems.append(f"{qid}: kind '{kind}' not one of {sorted(KINDS)}")

    if kind == "answerable":
        if not q.get("sql"):
            problems.append(f"{qid}: answerable question has no sql")
        if q.get("tier") not in TIERS:
            problems.append(f"{qid}: tier must be one of {sorted(TIERS)}")
    elif kind in {"ambiguous", "unsupported"}:
        if q.get("sql"):
            problems.append(
                f"{qid}: {kind} question must not carry sql - it has no single "
                f"correct answer")

    ac = q.get("answer_columns")
    if ac is not None and (not isinstance(ac, int) or ac < 1):
        problems.append(f"{qid}: answer_columns must be a positive integer")

    for listy in ("tags", "rules", "traps"):
        if not isinstance(q.get(listy, []), list):
            problems.append(f"{qid}: {listy} must be a list")

    return problems


def load_gold(gold_dir: Path = GOLD_DIR,
              verified_only: bool = False) -> tuple[list[GoldQuestion], list[str]]:
    """Load every question in the directory. Returns (questions, problems)."""
    gold_dir = Path(gold_dir)
    if not gold_dir.exists():
        return [], [f"{gold_dir} does not exist"]

    questions: list[GoldQuestion] = []
    problems: list[str] = []
    seen: dict[str, str] = {}

    for path in sorted(gold_dir.glob("*.yaml")):
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(raw, list):
            problems.append(f"{path.name}: top level must be a list")
            continue

        for item in raw:
            problems.extend(_validate(item, path))
            qid = item.get("id")
            if qid in seen:
                problems.append(f"{qid}: duplicate id, also in {seen[qid]}")
                continue
            if qid:
                seen[qid] = path.name
            questions.append(GoldQuestion(
                id=qid or "<no id>",
                question=item.get("question", ""),
                kind=item.get("kind", ""),
                source_file=path.name,
                tier=item.get("tier"),
                sql=item.get("sql"),
                ordered=bool(item.get("ordered", False)),
                tags=item.get("tags", []) or [],
                rules=item.get("rules", []) or [],
                traps=item.get("traps", []) or [],
                answer_columns=item.get("answer_columns"),
                notes=item.get("notes", "") or "",
                verified=bool(item.get("verified", False)),
            ))

    if verified_only:
        questions = [q for q in questions if q.verified]

    return questions, problems


# --------------------------------------------------------------------------
# Verifier
# --------------------------------------------------------------------------

def _preview(res: sandbox.QueryResult, width: int = 46) -> str:
    if not res.rows:
        return ""
    first = res.rows[0]
    cells = []
    for v in first[:3]:
        cells.append(f"{v:,.2f}" if isinstance(v, float) else str(v))
    text = " | ".join(cells)
    return text[:width]


def main() -> int:
    questions, problems = load_gold()

    print("=" * 78)
    print("GOLD SET VERIFICATION")
    print("=" * 78)

    if problems:
        print("\nSCHEMA PROBLEMS")
        for p in problems:
            print(f"  {p}")

    if not questions:
        print("\nno questions found")
        return 1

    con = sandbox.connect()
    failures = 0
    unverified = 0

    print(f"\n{'id':<7} {'kind':<12} {'t':<2} {'v':<2} {'rows':>6}  result / issue")
    print("-" * 78)

    for q in questions:
        flag = "y" if q.verified else "-"
        unverified += not q.verified
        tier = str(q.tier) if q.tier else "-"

        if q.kind != "answerable":
            print(f"{q.id:<7} {q.kind:<12} {tier:<2} {flag:<2} {'-':>6}  "
                  f"(no sql by design)")
            continue

        res = sandbox.run(q.sql, con=con)
        if not res.ok:
            failures += 1
            print(f"{q.id:<7} {q.kind:<12} {tier:<2} {flag:<2} {'ERR':>6}  "
                  f"{res.error_kind}: {(res.error or '')[:40]}")
        elif res.row_count == 0:
            failures += 1
            print(f"{q.id:<7} {q.kind:<12} {tier:<2} {flag:<2} {0:>6}  "
                  f"EMPTY RESULT - not a usable gold answer")
        else:
            trunc = "*" if res.truncated else " "
            print(f"{q.id:<7} {q.kind:<12} {tier:<2} {flag:<2} "
                  f"{res.row_count:>5,}{trunc} {_preview(res)}")

    con.close()

    # ---- coverage -------------------------------------------------------
    answerable = [q for q in questions if q.kind == "answerable"]
    with_traps = [q for q in answerable if q.traps]

    print()
    print("=" * 78)
    print("COVERAGE")
    print("=" * 78)

    print("\n  by kind")
    for kind, n in sorted(Counter(q.kind for q in questions).items()):
        print(f"    {kind:<14} {n:>4}")

    print("\n  by tier (answerable only)")
    for tier, n in sorted(Counter(q.tier for q in answerable).items(),
                          key=lambda kv: (kv[0] is None, kv[0])):
        print(f"    tier {tier}         {n:>4}")

    print("\n  rules invoked (glossary conventions)")
    for rule_tag, n in Counter(r for q in answerable for r in q.rules).most_common():
        print(f"    {rule_tag:<28} {n:>4}")

    print("\n  traps crossed (structural hazards)")
    trap_counts = Counter(t for q in answerable for t in q.traps)
    for trap, n in trap_counts.most_common():
        print(f"    {trap:<28} {n:>4}")

    share = len(with_traps) / len(answerable) if answerable else 0
    rule_share = (sum(1 for q in answerable if q.rules) / len(answerable)
                  if answerable else 0)
    print(f"\n    carrying a trap:  {len(with_traps)}/{len(answerable)} "
          f"({share:.0%})   target ~40%")
    print(f"    invoking a rule:  "
          f"{sum(1 for q in answerable if q.rules)}/{len(answerable)} "
          f"({rule_share:.0%})")

    print()
    print("=" * 78)
    print(f"{len(questions)} questions   {failures} failing   "
          f"{unverified} awaiting human verification")
    if failures:
        print("Fix failing SQL before verifying anything.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
