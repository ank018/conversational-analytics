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

Joins that produce wrong answers without producing errors.

| Trap | Effect |
|---|---|
| `order_items` joined to `order_payments` | Revenue inflated 4.5% |
| `customers` joined to `geolocation` | 99,441 rows become 15,083,455 — **151.7x** |
| Inner join to `product_category_translation` | 623 products, R$185,050 lost |
| Inner join `orders` to `order_items` | 775 orders vanish from the denominator |
| Counting `customer_id` | Returns order count |
| Averaging item price for AOV | Returns item value, not order value |

The geolocation join is the most dangerous in the schema. The table averages
52.6 rows per zip prefix, but the realised fan-out is three times that,
because customers concentrate in dense city postcodes. `customers` and
`sellers` already carry city and state; geolocation is needed only for
coordinates, and then only via a de-duplicated subquery.

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

## 7. Open items

1. Revenue under the exclusion rule in 2.2 has not yet been computed;
   `sum(price)` above covers all statuses. `src/02b_reference_values.py`
   settles it.
2. The R$165k payments-over-items gap is *attributed* to the 775 item-less
   orders on the arithmetic (≈R$213 each, against a mean order value near
   R$160). Plausible, not verified.
3. Whether the 789 duplicated `review_id` values are genuine duplicate
   submissions or an export artefact is unresolved. It affects only review
   counts, not scores.
