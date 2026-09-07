# The admin dashboard

**Status:** approved; money section deferred by decision (see below)
**Repositories affected:** `Biofarm_Backend` (one endpoint, one setting), `Biofarm_Frontend` (one page, one route). No schema change, no migration.

## Why

There is one admin. They need to know what needs doing, what is running out, and
what the shop is actually taking — and today there is nowhere that says any of it.

`/admin` currently renders the **404 page**. There is no index route at all; the
sidebar links straight to `/admin/products`, `/admin/orders` and `/admin/tags`.
So this is not only a new feature, it fills a hole anyone hits by typing the
obvious URL.

## Revised: Stripe owns the money, we own the work

The first version of this section designed a Collected/Committed split. That was
half wrong, and the question "doesn't Stripe already have a money dashboard?"
is what exposed it.

**Stripe is authoritative and we cannot beat it.** Anything computed from our own
`orders` table is a *belief* about money that drifts from Stripe for real
reasons: `total_amount` is list price while Stripe's figure is net of fees;
partial refunds and disputes happen in Stripe and never touch our rows; a capture
can fail after we have marked an order shipped. A tile reading "Collected: $950"
that disagrees with the Stripe dashboard is worse than no tile - the admin now
distrusts both and reconciles by hand.

**So Collected is dropped.** What survives is the one number Stripe genuinely
cannot produce: Stripe knows PaymentIntents, not that order #7633247686 is
awaiting fulfilment, holds three vials of Anti-Tau, and has been waiting 29
hours. That join lives only in our database.

That number is the **value of goods awaiting fulfilment** - the to-do list,
priced. It answers "is today's queue an afternoon or ten minutes", which is an
operations question, not an accounting one. It therefore belongs inside "Needs
you now" rather than in a money section of its own, and it is stated pre-tax:
sales tax is not the shop's, and a payout figure is Stripe's job.

A link out to the Stripe dashboard covers everything else, and stops anyone
reading our number as authoritative.

Dropping Collected also removes the ambiguity the section below spends its length
explaining. It is kept for the record.

## The complication that made "revenue" ambiguous

Checkout authorises; the money is captured at ship time (`capture_method="manual"`).
So an order's amount means different things depending on where it is:

| Status | The money |
| --- | --- |
| `awaiting_fulfillment`, `confirmed` | Authorised, **not taken** |
| `shipped`, `delivered` | **Captured** — actually yours |
| `cancelled` | Voided or refunded — zero |

A single "revenue" figure would be wrong whichever way it fell. The dashboard
splits it:

- **Collected** — `shipped` + `delivered`. Money that has moved.
- **Committed** — `awaiting_fulfillment` + `confirmed`. Authorised, waiting on
  the admin to ship it.

For a one-person operation that pair is more useful than a total anyway:
committed revenue is the cash value of the to-do list.

**Cancelled orders are excluded from both.**

## Tax is not revenue

`Order.total_amount` is the **pre-tax subtotal** — `create_order` sums
`variant.price * quantity` into it and stores `tax_amount` separately. Verified
against live orders rather than assumed from the column name, which reads like a
grand total and is not one.

Sales tax is collected on the state's behalf and is a liability, not income, so
it is reported on its own line and never folded into Collected or Committed.

## Time windows

`created_at` is UTC. "Today" in UTC rolls over in the early evening in US
timezones, so an evening order would land in tomorrow's figures and the
dashboard would disagree with the admin's own sense of the day.

Windows are therefore computed **server-side in a fixed business timezone**, from
a new `BUSINESS_TIMEZONE` setting. One zone, not the viewer's: the number should
be the same on a laptop, a phone, and from a hotel in another country.

`BUSINESS_TIMEZONE` is **`America/New_York`**.

- **today** — midnight to now, in that zone
- **last 7 days** / **last 30 days** — calendar days including today, so it is
  consistent with `today` rather than mixing calendar and rolling windows

## What it shows

### 1. Needs you now

The reason to open the page.

- **To confirm** — count of `awaiting_fulfillment`, with the age of the oldest.
  The age is the point: a count of 3 is fine, a count of 1 that has been sitting
  four days is not.
- **To ship** — count of `confirmed`
- **In transit** — count of `shipped`, informational
- **Low stock** — variants at or below a threshold, **listed** rather than
  counted, because the admin's next action is to reorder a specific thing.

  The threshold is 5, which is not a new number: `productSummary.ts` already uses
  `LOW_STOCK_THRESHOLD = 5` to decide when the storefront says "5 left" in its
  low-stock tone. The admin and the shop must not disagree about what "low"
  means, so the backend owns the value and returns it in the payload rather than
  the dashboard keeping a second copy to drift.
- **Out of stock** — variants at zero
- **Invisible products** — products with no variant. `list_public_products`
  filters those out, so they are silently unbuyable and nothing currently says so.

### 2. Money — one figure, inside the queue

The value of goods awaiting confirmation and fulfilment, pre-tax, beside the
counts it prices. Plus a link to the Stripe dashboard for anything that is
actually about money.

Not a section of its own, and no Collected, tax line or average order value -
see "Stripe owns the money" above.

**Known gap:** even this is our belief. Stripe holds an authorisation for about
seven days; if one expires, our order still reads `awaiting_fulfillment` and we
still count it. Arguably useful - an expired authorisation on an unshipped order
is exactly what an admin needs to chase - but nothing detects it today. Its own
piece of work.

### 3. Volume

Order counts over the same windows, top products by units sold over 30 days, and
a 30-day chart carrying two series:

- **daily orders**, as bars — the rhythm, which is what tells one person when to
  block out fulfilment time
- **cumulative orders**, as a line — the growth curve, counted from the shop's
  first order rather than from the window's start, so it shows where the business
  actually is rather than restarting every month

Two scales in one frame, labelled on both sides. Dual axes deserve their bad
reputation when they invite a false comparison between series; here the series
are the same quantity at two aggregations, and the alternative - two charts of
one variable - costs more space than it earns on a page whose job is a glance.

**Inline SVG, no charting library.** Bars and a polyline over a date axis is
about thirty lines. A library earns its weight when axes, tooltips, zoom and
legends are needed; none are here, and the lightest credible option would still
be among the heaviest things in the bundle, on the page most likely to be left
open all day.

Top products reads `OrderItem.product_name` and `quantity`, which are
denormalised on the item — so the figures survive a product being renamed,
repriced or deleted, which is exactly what historical reporting needs.

## How

**`GET /api/v1/admin/stats`**, admin-only, returning the whole payload in one
response.

Aggregated in SQL, not in the browser. The admin order list was moved to
server-side paging precisely so the console stops fetching every order; a
dashboard that pulled them all down to sum them would walk straight back into
that, and would do it on the page most likely to be left open.

Roughly five grouped queries — status counts, per-window sums, oldest pending,
low stock, products without variants, and top products.
`ix_orders_status_created_at` already covers the status-and-date shape.

Deliberately separate queries rather than one clever conditional aggregate: the
suite runs on SQLite and the server on Postgres, and `FILTER (WHERE …)` is not
portable between them. At this size the difference is unmeasurable and the
queries stay readable.

**Frontend:** a new `AdminDashboardPage` at `/admin`, as the index route, plus a
sidebar entry. Numbers are links wherever a number implies an action — "3 to
ship" goes to the order list filtered to `confirmed`, a low-stock variant goes to
its product editor. A dashboard that reports a problem and makes you go and find
it is half a feature.

## Deliberately not in this version

- **A revenue chart.** The order chart above is volume only. Revenue over time is
  Stripe's, for the reasons in "Stripe owns the money".
- **Products with no images.** They render a placeholder rather than breaking,
  so it is cosmetic next to a product nobody can buy at all.
- **Anything per-customer.** There is no cohort to analyse yet.

## Verification

Unit tests for each aggregate against known fixtures, including the cases that
are easy to get wrong: cancelled orders excluded from both money figures, tax
never folded in, an order on the boundary of "today" landing on the correct side
in the business timezone rather than in UTC.

The timezone boundary case is the one to write first — it is the whole reason the
setting exists, and it is invisible in any test that runs at midday.
