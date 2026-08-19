# Business rules

Distilled from `docs/data_semantics.md`, which was written at stage 2 before
any model was run against the gold set. **No rule here was added in response
to an observed failure.** That matters: rules written by reading the failure
list would be fitted to the test set, and the resulting gain would measure
nothing but hindsight.

This file is injected verbatim into the prompt at stage 9. It is deliberately
short. The source document runs to several pages of reasoning and measured
figures; a model needs the conclusions, not the workings.

**One post-hoc correction, disclosed.** The scope of the status exclusion
originally read "revenue, order counts, item counts, and customer counts".
That contradicted the gold set, which — consistently across q605, q606 and
q607 — applies no status filter to questions about how long a process took.
Two documents disagreed and one had to be wrong; the wording below resolves
it. Headline results are reported against the ORIGINAL wording, with the
corrected figure alongside, so no number depends on a change made after
seeing the outcome.

Nothing else was changed. In particular the payment-row exception, which the
model violated on q111 and q313, is stated exactly as it was. Strengthening
it would be tuning the prompt against the test set, and the violation is a
result worth keeping.

---

## Revenue

Revenue is `sum(order_items.price)`. It excludes freight.

`order_items.freight_value` is shipping, charged per item line, and is not
revenue. `order_payments.payment_value` is what the customer paid — it
includes freight and covers cancelled orders, so it is not revenue either.
Total paid exceeds revenue by roughly 18%, and that is expected rather than
an error.

## Which orders count

Exclude orders whose `order_status` is `canceled` or `unavailable`.

This applies to questions about commercial activity: revenue, sales, order
counts, item counts, and customer counts reached through orders.

It does **not** apply to:

- counts of payment rows or review rows aggregated from their own tables;
- questions about how long a process took — approval, dispatch, delivery.
  An order that was later cancelled still took two days to be approved, and
  the exclusion does not erase that.

Other statuses — `shipped`, `invoiced`, `processing`, `created`, `approved` —
are included. They are live commerce that has not yet completed.

## Customers

A customer is `customers.customer_unique_id`.

`customers.customer_id` is issued once per order, so counting it returns the
order count. It is the join key to `orders`; it is not a person.

## Averages over orders

To average anything per order, aggregate to order level first, then average.
Averaging `order_items` rows directly gives the average per item, which is a
different and smaller number.

## Dates

"When an order happened" means `order_purchase_timestamp` unless the question
says otherwise. A question about deliveries in a period filters on
`order_delivered_customer_date`.

Delivery time is `order_purchase_timestamp` to
`order_delivered_customer_date`, over orders with status `delivered` and a
non-null delivery date. An order is on time when
`order_delivered_customer_date <= order_estimated_delivery_date`.

## Reviews

Count review rows. `order_reviews.review_id` is not unique — some values
appear against more than one order.

## Product categories

`products.product_category_name` is Portuguese. Join
`product_category_translation` with a **LEFT** join and report products with
no category as `unknown`; an inner join silently drops them. When a question
names a specific category, an inner join is correct because the category must
be translated to be matched.

## Joins that produce wrong numbers

Never join `order_items` and `order_payments` in the same query. An order has
many item rows and many payment rows, so joining both multiplies the result.
If both are needed, aggregate each to order level in separate CTEs first.

Do not join `geolocation` unless coordinates are required. Many rows share a
zip prefix, so the join multiplies rows roughly 150-fold. `customers` and
`sellers` already carry city and state.

When a question asks about orders but the filter applies to item rows, count
`DISTINCT order_id`. When it asks about units sold, count rows.

## Coverage

Orders run from September 2016 to October 2018, but only January 2017 to
August 2018 is complete. November 2016 is missing entirely, and September and
October 2018 hold 20 orders between them. Comparisons spanning those edges
are not like for like.

The marketing tables cover a shorter period still, ending around May 2018,
and only a minority of closed deals match a seller in `sellers`. Funnel
figures do not describe the marketplace as a whole.
