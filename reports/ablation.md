# Ablation

Model `deepseek/deepseek-v4-pro-0813`, pinned to the Fireworks provider,
temperature 0, on 75 answerable questions plus 11 with no correct answer.
Three runs per configuration unless noted.

**Noise floor: 2.7 points.** That is the spread across four runs of a
byte-identical prompt (`reports/noise_floor.json`). Temperature is zero; the
variation comes from the provider. Any delta below 2.7 points has not been
demonstrated, however plausible it looks.

## Results

| Configuration | Accuracy | Delta | vs noise | Silent errors | Prompt tokens | Cost/question |
|---|---:|---:|---:|---:|---:|---:|
| baseline | 50.7% | — | — | 48.9% | 653 | $0.0021 |
| + schema detail ¹ | 49.3% | −1.4 pts | below | ~50% | 3,196 | — |
| + business rules | **86.7%** | **+36.0 pts** | **13.1×** | **12.4%** | 1,741 | $0.0048 |
| + execution retry ² | 86.7% | +36.0 pts | 13.1× | 12.4% | 1,741 | $0.0048 |

¹ Two runs rather than three, with 10 and 4 dropped calls to provider rate
limits. Individual runs scored 49.2% and 49.3%. The configuration was not
completed because its prompts are 5.7× the baseline size, making it the
slowest and most rate-limit-prone in the project; a third run exceeded the
time budget. The effect is far enough inside the noise floor that a third
run could not change the conclusion.

² Identical to the business-rules row in every figure because retry **never
fired**. Across 225 question-runs, every generated query executed and
returned rows.

**Not measured: schema detail *combined with* business rules.** So this table
shows that describing the schema does not help on a bare prompt. It cannot
rule out that schema detail helps once the semantics are settled — the 12.4%
of answers that remain silently wrong could in principle include column-level
confusion. That cell was dropped for the same time-budget reason as the third
schema run.

## What the columns mean

**Silent errors** are answers that ran without error and returned a clean,
plausible, wrong result. The user sees a number and a table; nothing
indicates anything went wrong. This is the number a business should care
about, and it is almost never reported.

**Delta** is measured against the baseline row.

## Readings

**Business rules are the whole story.** One page of definitions — what
revenue means, which orders count as sales, what a customer is, which of five
date columns is "delivery" — moved accuracy 36 points and cut silent errors
from roughly half of all answers to one in eight. None of that information
exists anywhere in the database. It exists only because someone wrote it
down.

**Documenting the schema did not help.** Foreign keys, sample values, ranges,
null counts and written notes on every ambiguous column: 5.7× the prompt
tokens, materially slower, more prone to rate limiting, and no measurable
accuracy change. This is the fix most teams reach for first.

**Execution retry addressed a failure class that does not exist here.** Retry
can only trigger on signals available without the answer — a query that
errors, or one that returns no rows. With the rules in place, no query did
either. Wrongness is semantic, and semantics leave no trace at execution
time.

**A single run is not a measurement.** Four runs of an identical prompt at
temperature zero spanned 2.7 points. Published text-to-SQL figures quoted to
one decimal from a single run are reporting less precision than they appear
to.

## Reproducing

```
python src/08a_noise_floor.py --model <slug> --provider <name> --repeats 4
python src/11_ablation.py --model <slug> --provider <name> --repeats 3 \
    --configs baseline glossary repair
```

Responses are cached on disk, so a repeat run costs nothing. Per-run detail
is in `reports/runs/`, the failure taxonomy in `reports/error_taxonomy.yaml`,
and the evaluation set in `eval/gold/`.
