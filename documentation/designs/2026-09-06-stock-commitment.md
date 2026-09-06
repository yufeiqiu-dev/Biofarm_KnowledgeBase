# When stock is committed

**Status:** implemented
**Repository affected:** `Biofarm_Backend` only. No schema change, no migration.

## The problem

Stock is read at checkout and deducted at admin-confirm, and nothing holds it in
between.

`initiate_checkout` (`app/api/v1/endpoints/orders.py:110`) checks that every
variant has enough stock, then creates a manual-capture PaymentIntent and saves a
`CheckoutSession`. The check takes no lock and reserves nothing — it is a
courtesy, not a guarantee. The deduction happens much later, in
`confirm_order_admin` (`app/services/order_service.py:286`), which *is* correctly
row-locked and sorted.

So two customers can both pass the check for the last vial, both authorise their
cards, and both reach `awaiting_fulfillment`. The admin confirms the first; the
second raises `ValueError` and surfaces as a bare 400.

**The window is not milliseconds.** It runs from payment until an admin happens
to click confirm — hours, or over a weekend, days.

### What keeps it from being severe

`capture_method="manual"`, and `admin_orders.py:199` voids the authorisation for
any order before `shipped`. Nobody is wrongly charged: the loser's card carries a
hold that is released. The real costs are a customer told "sold out" after they
believed they had bought it, and an admin given a 400 with no guidance.

### Why fix it anyway

Not because of the race — at this volume two people contending for the same last
vial is vanishingly unlikely. The defect is that **stock moves at an arbitrary
human moment**. An admin clicking confirm on a Tuesday is what currently decides
whether inventory moves, which makes the system hard to reason about and lands
the failure on the person least able to do anything about it.

## What was considered

**Reserve at payment-intent time with a TTL** — the textbook answer, and what
Shopify and Ticketmaster-style checkouts do. The loser is blocked *before* paying.
Rejected: it buys a reservation lifecycle, a short sweeper, and a "your cart
expired" experience, and it holds inventory for everyone who abandons at the card
form. Over-built for this shop.

**Leave the timing; make the failure graceful** — a better admin error and
one-click cancel-and-void. Rejected as insufficient: it dresses up the symptom and
leaves the arbitrary-moment problem in place.

**Accept the order and backorder it** — closest to how reagent suppliers actually
behave, since multi-week lead times are unremarkable in this industry. Rejected
for now only because it needs a new order state and therefore a migration. Worth
revisiting; see "Later".

## The design

The invariant changes from

> stock moves when an admin confirms

to

> **an order that exists holds its stock, from creation until cancellation.**

Everything below follows from that one sentence.

### 1. `create_order` deducts

Both the webhook path (`create_order_from_checkout_session`) and the bypass path
(`initiate_checkout`) funnel through `create_order`, so this is the only place the
deduction needs to go.

It aggregates demand per variant before validating — a cart can legitimately hold
two line items for the same variant, and per-item checks miss combined overstock —
then takes stock row-locked, in sorted variant order. That is the discipline
`confirm_order_admin` already uses, moved rather than reinvented: the lock makes
the read and write one atomic step, and the sort stops two concurrent
transactions taking the same rows in opposite orders and deadlocking.

If any variant is short it raises `ValueError`, and no order is created.

### 2. `confirm_order_admin` stops deducting

It becomes: check the status, set `confirmed`. It can no longer fail on stock,
which is the admin-facing symptom being fixed.

### 3. `cancel_order`'s restore becomes unconditional

Today it restores only from `confirmed`, `shipped` and `delivered`. Since every
order that exists now holds stock, and the function already rejects an
already-cancelled order, the condition disappears entirely.

### 4. The webhook must not 500 on a shortage

The delicate part. If `create_order` raises inside
`create_order_from_checkout_session` and that propagates, Stripe receives a 500
and retries with backoff for days — each retry failing the same way — while the
authorisation stays live until it expires.

Instead the webhook catches it, voids the PaymentIntent, deletes the checkout
session, logs at error, and returns 200. This mirrors the reasoning already
recorded next to `send_order_confirmation`: a non-200 to Stripe is a retried,
duplicated order, so anything recoverable must be swallowed deliberately.

### 5. Shared helpers

Creation and cancellation get one implementation of the lock-and-sort discipline
rather than copies drifting apart.

## What implementing it turned up

**The lock did not work, and the suite could not tell.** `_take_stock` locks each
row with `Session.get(..., with_for_update=True)` — correct on the face of it, and
the whole suite was green. Against real Postgres, eight concurrent buyers racing
for one unit produced **six orders**.

The cause is that `create_order` loads the variants before it takes stock, so they
are already in the session's identity map. `Session.get` will take the lock and
leave those already-loaded attributes untouched: every buyer waited properly for
the lock and then deducted from the value it had read *before* waiting. The row
was locked and the read was stale, which is precisely the lost update the lock
exists to prevent.

The fix is `populate_existing=True` alongside `with_for_update`, forcing a re-read
under the lock. Its absence looks like nothing — there is no missing call to spot,
and the code reads as correct.

SQLite ignores `FOR UPDATE` entirely, so no amount of running `app/tests/` would
ever have shown this. The suite now asserts that both arguments are passed, which
is the most it can honestly do; `scripts/check_stock_race.py` is the check that
actually proves it, and it needs Postgres. It is deliberately not part of the
suite.

Worth stating plainly: this bug was introduced *by this change* and caught only
because the fix was exercised against the database it will actually run on.

## Consequences

**The customer who loses.** Their hold is voided and no order exists, so
`OrderSuccessPage` polls `/orders/by-payment-intent/{pi}` and finds nothing.

This was first recorded here as falling back to a message that was "honest, but
vague about why". That was wrong, and reading the page rather than remembering it
would have shown so. On timeout it rendered a green tick, **"Order Placed!"** and
"Your payment was successful", told the customer to look in My Orders for
something that would never arrive, and emptied their cart on the way — four
false statements and the loss of the only state that would have let them try
again. Not vague; the opposite of true.

Fixed in the frontend as a follow-up: the timed-out case is now its own state
that says the order could not be confirmed, covers both possible causes -
because the browser genuinely cannot tell a slow webhook from a sold-out item -
and says plainly that no charge has been taken if it was the latter. The cart is
cleared when an order is confirmed rather than on arrival, so it survives.

**Existing orders straddle the change.** Two orders currently sit at
`awaiting_fulfillment`, created under the old rule with their stock never
deducted. After the change, confirming one deducts nothing and cancelling one
*returns* stock that was never taken — inventory is overstated either way.

Decided: **left as-is.** They are development test data, and staging is expected
to be wiped before launch. This is a known-wrong state, recorded here rather than
discovered later. Any environment carrying real orders would need a one-off
reconciliation before this ships.

**Safety stock remains the cheapest mitigation** and needs no code: holding a
buffer back — the site reads 0 while two remain — absorbs races, miscounts and
breakage together, and at this volume does more real work than any of the above.

## Tests

Several existing tests encode the old contract and are rewritten, not deleted:
`test_admin_confirm_order_deducts_stock`, `test_admin_ship_order_captures_no_stock_change`,
and `test_stock_locking.py` in full — its scenarios move from confirm to creation.

New coverage:

- deduction happens on both creation paths (webhook and bypass)
- insufficient stock at creation leaves no order behind
- the webhook returns 200, voids the intent, and deletes the session on a shortage
- confirm never changes stock, and cannot fail on it
- cancelling from `awaiting_fulfillment` restores
- duplicate line items for one variant aggregate at creation
- two buyers, one vial: the second fails at creation rather than at confirm

## Later

Not part of this change, recorded so they are decisions rather than oversights:

- **Backorder instead of void.** Give the losing order a state that carries a lead
  time, matching how the industry actually behaves. Needs a migration.
- ~~**Tell the customer what happened.**~~ Done, as a follow-up: see "The
  customer who loses" above.
