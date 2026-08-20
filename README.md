# Conversational Analytics

Ask a Brazilian e-commerce warehouse a question in English and get an answer.
The interesting question is not whether it answers — it always answers — but
how often the answer is right, and how you would ever know.

**[Live demo](https://conversational-analytics-18ls.onrender.com/)** ·

The demo runs on a free instance and sleeps when idle, so the first request
may take a minute.

---

## The result

| Configuration | Accuracy | Delta | Silent errors | Prompt tokens |
|---|---:|---:|---:|---:|
| Schema only | 50.7% | — | 48.9% | 653 |
| + documented schema | 49.3% | −1.4 pts *(below noise)* | ~50% | 3,196 |
| **+ business rules** | **86.7%** | **+36.0 pts** *(13.1× noise)* | **12.4%** | 1,741 |
| + execution retry | 86.7% | +0.0 pts *(never fired)* | 12.4% | 1,741 |

75 answerable questions, three runs per configuration, DeepSeek V4 Pro at
temperature 0 with the provider pinned. **Noise floor 2.7 points** — the
spread across four runs of a byte-identical prompt. Any delta below that has
not been demonstrated.

**Silent errors** are answers that ran without error and returned a clean,
plausible, wrong result. Nothing indicates anything went wrong. This is the
number a business should care about, and it is almost never reported.

---

## Four findings

**Most wrong answers look right.** Of 75 questions on the baseline, one
failed visibly. Thirty-seven returned a confident, incorrect number. A user
would have watched the system answer 74 of 75 questions without a single
error message, and half those answers were wrong.

**One page of definitions was worth 36 points.** What revenue means, which
orders count as sales, what a customer is, which of five date columns is
"delivery". A single rule — exclude cancelled and unavailable orders —
accounted for 24 of the 27 recovered questions. None of that information
exists anywhere in the database. It exists only because someone wrote it
down, and no model can infer it.

**Documenting the schema did nothing.** Foreign keys, sample values, ranges,
null counts and written notes on every ambiguous column: 5.7× the prompt
tokens, materially slower, more prone to rate limiting, no measurable
accuracy change. This is the fix most teams reach for first.

**Retry loops had nothing to fix.** Repair can only trigger on signals
available without the answer — a query that errors, or one that returns
nothing. Across 225 question-runs with the rules in place, no query did
either. Wrongness was semantic, and semantics leave no trace at execution
time.

### And one about measurement

Four runs of an identical prompt at temperature zero spanned 2.7 percentage
points, with five questions flipping between runs. Published text-to-SQL
figures quoted to one decimal place from a single run are reporting more
precision than they have.

---

## How it was measured

The evaluation apparatus was built before the system it evaluates. That
ordering is the point: a test set written after seeing what a model can do is
a test set the model passes.

1. **86 questions written and verified by hand.** Seventy-five answerable,
   each with gold SQL that was executed and signed off. Eleven with **no
   correct answer at all** — no cost data for a profit-margin question, no
   returns table for a return rate — where the correct behaviour is to
   decline. Almost no text-to-SQL benchmark contains that category, which is
   why almost every text-to-SQL system answers such questions confidently.

2. **An evaluator tested in both directions.** 26 cases proving it accepts
   queries that differ from gold only cosmetically — different aliases,
   rounded values, reordered columns — and catches the specific wrong queries
   the question set was built around. Getting this wrong in either direction
   silently corrupts every number downstream.

3. **A failure taxonomy built by hand.** All 37 baseline failures classified
   into named causes, which produced a prediction for each intervention
   *before* any of them were built. The taxonomy predicted business rules
   would recover 37.3 points. Measured: 36.0.

4. **A noise floor measured before any comparison.** Identical prompt, four
   runs, 2.7-point spread. Every delta since is reported against it.

The traps in the question set are structural, drawn from the warehouse
itself: an order has many item rows *and* many payment rows, so joining both
inflates revenue by 4.5%; `customer_id` is issued per order, so counting it
returns the order count; joining `geolocation` multiplies rows 150-fold.
Roughly 36% of answerable questions cross at least one.

---

## Known limits

- **The schema-plus-rules interaction was not measured.** The negative result
  on schema documentation covers a bare prompt only; it does not rule out
  schema detail helping once the semantics are settled. That configuration
  was the slowest in the project and exceeded its time budget.
- **The documented-schema row ran twice, not three times**, with dropped
  calls to provider rate limits.
- **Follow-up questions are unevaluated.** The demo supports them by
  resending the conversation; every figure here is for single questions.
- **Abstention is too noisy to state precisely.** Correct on 7 of 11
  questions, but that metric swung by three questions across identical runs.
- **Two questions regressed when the rules were added.** Both count payment
  rows, and the model applied the status exclusion to them despite the rules
  explicitly exempting payment-row counts. **Stating an exception does not
  stop a model generalising past it.**
- **Gold SQL is DuckDB-flavoured.** The method transfers to any warehouse;
  these particular 75 queries would need porting.
- One glossary sentence was corrected after it was found to contradict the
  gold set. Headline figures are reported against the original wording, so no
  number depends on a change made after seeing results.

---

## Reproducing

```bash
pip install -r requirements-dev.txt

python src/01_build_warehouse.py          # build the DuckDB warehouse
python src/gold.py                        # verify all 86 gold answers
python src/evaluator.py                   # 26 evaluator self-tests
python src/sandbox.py                     # 21 sandbox self-tests

python src/08a_noise_floor.py --model <slug> --provider <name> --repeats 4
python src/11_ablation.py --model <slug> --provider <name> --repeats 3 \
    --configs baseline glossary repair
```

Model responses are cached on disk, so re-running costs nothing. Set
`OPENROUTER_API_KEY` in `.env`.

```bash
docker build -t conv-analytics .
docker run -p 8501:8501 --env-file .env conv-analytics
```

## Layout

```
app/            Streamlit demo
src/            pipeline; numbered files are stages, unnumbered are libraries
  sandbox.py      read-only SQL execution with classified errors
  evaluator.py    result comparison and verdicts
  runner.py       the shared run loop every stage uses
  gold.py         gold set loading and verification
  schema.py       schema rendering at four detail levels
  llm.py          OpenRouter client with an on-disk cache
docs/
  data_semantics.md   what every term means, and why
  glossary.md         the rules injected into the prompt
eval/gold/      86 questions, verified
reports/        ablation, noise floor, error taxonomy, per-run results
data/           the warehouse, committed so this is reproducible
```

## Data

[Brazilian E-Commerce Public Dataset by Olist](https://www.kaggle.com/datasets/olistbr/brazilian-ecommerce)
— 99,441 real orders across 11 tables, 2016 to 2018.
