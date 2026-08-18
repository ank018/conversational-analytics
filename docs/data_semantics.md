# Data Semantics and Metric Definitions

Stage 2 of the build. This document is the authoritative record of what the
warehouse's terms mean. It exists because the central finding of stage 1 is
that this schema answers ordinary business questions several different ways,
all of them defensible, and a question in English does not say which one it
wants.

Two consumers depend on it. Every gold answer in `eval/gold/` is written
against the definitions below, so a change here invalidates gold answers.
From stage 9, a compact form of section 3 is injected into the model's
prompt. **These must not be allowed to drift apart** — the same failure the
credit scorecard avoided by sharing one `features.py` between the pipeline,
the API and the registry.

Measured against `data/olist.duckdb` built by `src/01_build_warehouse.py`.
Reference values are produced by `src/02b_reference_values.py` and committed
to `reports/reference_values.json`.

---

## 1. Why this document exists

Asked for total revenue, four queries run without error:

| Query | Result |
|---|---:|
| `sum(price)` over order items | R$13,591,643.70 |
| `sum(price + freight_value)` | R$15,843,553.24 |
| `sum(payment_value)` over payments | R$16,008,872.12 |
| items joined to payments, `sum(price)` | R$14,209,115.34 |

The first three are legitimate answers to different questions and span 17.8%.
The fourth is wrong — it double-counts orders that have both multiple items
and multiple payment rows — and overstates item revenue by 4.5%, which is far
too small to notice and far too large to ignore.

No amount of schema information fixes this. Only a definition does.

---

## 2. Decisions

Three judgement calls were made deliberately. They are business decisions, not
technical ones, and every downstream number depends on them.

### 2.1 Revenue means product price, excluding freight

Freight is a pass-through to the carrier rather than merchandise value, so
product price is what the marketplace's sellers actually earned.

**Consequence, stated so it is not later mistaken for a bug:** revenue will
never reconcile to what customers paid. The gap is roughly 18% and is
composed of freight (~R$2.25M) plus payments on orders that were cancelled or
never fulfilled (~R$165k). Both are expected.

### 2.2 Cancelled and unavailable orders are excluded

`canceled` and `unavailable` orders are removed from every revenue and order
count. Most of them contain nothing at all — 603 of 609 unavailable and 164 of
625 cancelled orders have no rows in `order_items` — but a minority do, so the
exclusion is not a no-op and must be written into the query rather than
assumed.

Other non-delivered statuses (`shipped`, `invoiced`, `processing`, `created`,
`approved`) **are** included: those orders are live commerce that has not yet
completed.

**Scope.** The exclusion applies to revenue, order counts, item counts, and
customer counts derived through orders. It does **not** apply to counts of
payment rows or review rows aggregated from their own tables without reference
to order status — q005 and q111 are correct without it. Where a question is
about the customers table itself rather than about ordering behaviour (q511),
the exclusion also does not apply.

### 2.3 The marketing funnel tables are retained

`marketing_qualified_leads` and `closed_deals` are kept despite covering only
a fraction of the marketplace — 380 of 842 closed deals match a seller in
`sellers`, which is 12.3% of sellers. They are retained precisely because of
that: they support questions where the correct answer is that the data does
not support a conclusion, and a system that answers such questions confidently
is exhibiting the failure this project measures.

**No funnel figure may be presented as marketplace-wide.**

---

## 3. Definitions

Canonical SQL for each term. Reference values in
`reports/reference_values.json`.

### Revenue

```sql
SELECT sum(oi.price)
FROM order_items oi
JOIN orders o ON o.order_id = oi.order_id
WHERE o.order_status NOT IN ('canceled', 'unavailable')
```

**Reference value: R$13,494,400.74.** The status exclusion costs R$97,242.96,
0.72% of the unfiltered total.

Never join `order_payments` in the same query as `order_items`. If both are
needed, aggregate each to order level in separate CTEs first.

### Order count

An order is one row in `orders`, excluding cancelled and unavailable.
Orders with no items still count as orders — they are simply worth nothing.

### Customer

**`customer_unique_id`, not `customer_id`.** `customer_id` is issued per
order; counting it returns the order count under another name. `customer_id`
is the join key to `orders`; `customer_unique_id` is the person.

A repeat customer is a `customer_unique_id` appearing on more than one
non-excluded order.

### Average order value

Aggregate to order level first, then average. Averaging `order_items.price`
directly gives average *item* value, which is a different and smaller number.

### Delivery time

Days from `order_purchase_timestamp` to `order_delivered_customer_date`,
restricted to orders with status `delivered` and a non-null delivery date.

Four other intervals are constructible from the five date columns
(approval lag, carrier handover, estimated versus actual). None of them is
"delivery time" without qualification.

### On-time delivery

`order_delivered_customer_date <= order_estimated_delivery_date`, over
delivered orders only. Undelivered orders are excluded rather than counted
as late.

### Review score

Average `review_score` over review **rows**. Note that `review_id` is not
unique — 99,224 rows carry 98,410 distinct ids, and 789 ids attach to more
than one order. "How many reviews" therefore has two answers; the row count
is used unless a question says otherwise.

### Product category

Join `products` to `product_category_translation` with a **LEFT** join.
An inner join silently drops 623 products carrying R$185,050 of revenue,
1.36% of the total. Uncategorised products are reported as `unknown`, not
omitted.

---

## 4. Structural traps

Joins and counts that produce wrong answers without producing errors.
Severity is measured under the canonical definitions in §3, not asserted.

| Trap | Measured effect | Severity |
|---|---|---|
| `customers` joined to `geolocation` | 99,441 rows become 15,083,455 (**151.7x**) | severe |
| Averaging item price for AOV (`aov_item_vs_order`) | 12.40% understated | high |
| `order_items` joined to `order_payments` (`payment_fanout`) | revenue 4.53% overstated | high |
| Counting `customer_id` as customers | 3.39% overstated | high |
| `timestamp_vs_date` | Grouping by a raw timestamp makes almost every row its own group; the answer becomes "one order, at 14:32:07" | high |
| `order_line_vs_order` | Counting item rows where the question asks for orders | high |
| `duplicate_key_fanout` | Joining on a key that repeats in the parent table — a seller recruited via two leads has two `closed_deals` rows | moderate |
| Inner join to `product_category_translation` | R$183,813 lost, 1.36% | moderate |
| `column_confusion` | `product_name_lenght` and `product_description_lenght` are character counts, not physical dimensions, and sit beside `product_length_cm` | moderate |
| `review_fanout` | Joining reviews through `order_items` counts one review once per item line, weighting multi-item orders and shifting rankings | moderate |
| `missing_reviews` | An inner join to reviews drops the 768 orders without one; a review-rate question then returns 1.00 against a true 0.99 | low |
| Inner join `orders` to `order_items` | 8 orders | negligible |

Three of these have a counter-example in the gold set, deliberately, so that
no rule is learnable in the wrong direction: `order_line_vs_order` is an error
in q501 and q514 but correct behaviour in q510; the LEFT join required by
`category_inner_join` in q106 is wrong in q320, where the question names a
category that must be translated; and `missing_reviews` requires a LEFT join
in q508 where q302 correctly uses an inner one.

The last row was originally recorded as a major trap on the strength of 775
item-less orders. Under the exclusion rule in 2.2, 767 of those are already
removed as cancelled or unavailable and only 8 remain. **The decision to
exclude them eliminated the trap.** It is retained here because a question
phrased to include all statuses re-opens it.

The geolocation join is the most dangerous in the schema. The table averages
52.6 rows per zip prefix, but the realised fan-out is nearly three times that,
because customers concentrate in dense city postcodes. `customers` and
`sellers` already carry city and state; geolocation is needed only for
coordinates, and then only via a de-duplicated subquery.

Two figures are worth knowing because they are counter-intuitive rather than
dangerous: average order value is **R$137.42**, and only **3.04% of customers
(2,888 of 94,990) ever place a second order**. A questioner assuming ordinary
e-commerce repeat rates will read the correct answer as an error.

---

## 5. Coverage

Orders run 2016-09-04 to 2018-10-17, but the usable window is narrower:

- **2016** — 4 orders in September, 324 in October, **none in November**,
  1 in December.
- **2017** — complete. November spikes to 7,544, consistent with Black Friday.
- **2018** — complete through August; September has 16 orders and October 4.

**The reliable window is 2017-01 to 2018-08.** Year-over-year comparisons
spanning 2016 or late 2018 compare a full period against a near-empty one and
will look like collapse or explosive growth. Questions touching those edges
belong in the gold set as traps, with the correct answer stating the caveat.

**The marketing funnel tables cover a different period again.**
`marketing_qualified_leads` runs to roughly May 2018 — a question asking for
leads by month across 2018 returns five rows, not eight or twelve. Funnel
activity and marketplace activity therefore cannot be compared month-for-month
without stating the mismatch, and a chart placing them side by side will show
a spurious collapse in lead generation from June 2018.

---

## 6. Data quality register

Small and non-blocking, recorded so they are not rediscovered as surprises.

| Observation | Count |
|---|---:|
| Orders marked delivered with no delivery date | 8 |
| Orders not marked delivered carrying a delivery date | 6 |
| Orders with no payment row | 1 |
| Payment rows typed `not_defined`, value 0.00 | 3 |
| Payment rows recording 0 instalments | 2 |
| Orders where payments disagree with price + freight | 359 of 98,665 (0.39%) |
| Zip prefixes where customer and geolocation state disagree | 7 |

The reconciliation figure is the significant one: **99.61% of orders match
exactly**. The ambiguity in this warehouse is definitional, not a data quality
problem. That is an unusually clean setting in which to measure whether a
system resolves ambiguity correctly.

---

## 7. Resolved and open

**Resolved by `src/02b_reference_values.py`.**

1. Revenue under the exclusion rule is **R$13,494,400.74**, R$97,242.96 below
   the unfiltered figure.
2. The payments-over-items gap is confirmed as the item-less orders:
   R$162,591.95 of R$165,318.88, **98.35%**. The residual R$2,727 falls within
   the 359 orders where payments and order totals disagree.
3. The duplicated `review_id` values do not affect analytics. Average review
   score is 4.09 whether computed over rows or over distinct ids. The
   duplication changes review *counts* only, and is recorded as a counting
   convention rather than a trap.

**Open.**

4. A 3.04% repeat rate is low enough to warrant checking that
   `customer_unique_id` genuinely resolves people across orders rather than
   being reissued in some cases. If it is unreliable, every retention question
   is unanswerable and that fact belongs in the README.
5. Whether `not_defined` payment rows and 0-instalment rows (3 and 2 rows
   respectively) should be filtered from payment-type breakdowns. Immaterial
   to totals; matters only to share-of-payment-method questions.
