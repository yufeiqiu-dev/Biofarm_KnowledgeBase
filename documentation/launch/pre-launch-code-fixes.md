# Pre-launch code fixes

Everything here is in the application code, not the infrastructure, and none of
it is blocked by AWS work. Do it first: several of these are invisible locally
and only fail once the app meets a fresh database, a real Cognito token, or a
public URL.

Ordered by severity. P0 items will break or expose the launch; P1 items will
cost you an incident; P2 items are known defects worth scheduling.

Status legend: `[ ]` not started, `[x]` done and verified.

---

## P0-1. The migration chain does not work on a fresh database

- [x] Squash to a single baseline migration and remove `create_all` — done in `adc2545`

**What is true today.** Alembic is set up correctly - `alembic/env.py` imports
`app.models`, sets `target_metadata = Base.metadata`, and overrides
`sqlalchemy.url` from the app's own settings. The chain is linear with a single
head:

```
55894e29c942 (base) -> c09c8c1b08ed -> a3f1d2e9b047 -> b7e4a1c0d823 -> eea1fc0aee1c (head)
```

**The problem.** Those migrations only ever `create_table` for `orders` and
`order_items`. Five tables have no creating migration at all - `products`,
`product_variants`, `tags`, `product_tags`, and `checkout_sessions` - because
they predate Alembic and are still produced by `Base.metadata.create_all(...)`
in the `lifespan` hook of `app/main.py`. Worse, `c09c8c1b08ed` and
`a3f1d2e9b047` `add_column` to `checkout_sessions`, which nothing creates.

Against a fresh database, both orderings fail:

| You run | What happens |
|---|---|
| `alembic upgrade head` first | Fails in the **first** migration, `55894e29c942`, creating `order_items`: its foreign key targets `product_variants`, which no migration creates. (An earlier draft of this document predicted the failure at `c09c8c1b08ed` on `checkout_sessions`; reproducing it showed the chain breaks a step sooner. Same root cause.) |
| App first, then `alembic upgrade head` | `create_all` has already built `orders`; the base migration fails creating a table that exists |

This is invisible locally because the dev database has been carried forward by
`create_all` since before Alembic existed. RDS will be a fresh database.

**The fix.** Nothing is deployed and no production data exists, so the migration
history is fiction - squash it rather than patching around it:

1. Delete the five files in `alembic/versions/`.
2. Drop the local database so you are generating against nothing:
   `docker compose -f <compose> down -v && docker compose -f <compose> up -d`
3. Generate one baseline from the current models:
   `alembic revision --autogenerate -m "baseline schema"`
4. Read the generated file before trusting it. Autogenerate is reliable for
   tables and columns but routinely misses server defaults, `ondelete` behaviour,
   and non-native enums - all three of which this schema uses
   (`ondelete="CASCADE"`, `ondelete="SET NULL"`, `native_enum=False`,
   `server_default="[]"` on `image_urls`).
5. Delete `Base.metadata.create_all(bind=engine)` from the lifespan hook. Two
   mechanisms owning the same schema is what created this mess.

**Verify.** On a clean volume:

```bash
alembic upgrade head          # succeeds
alembic check                 # reports no new operations - models match schema
python -m pytest app/tests/ -q
```

`alembic check` passing is the real proof: it means the migration and the models
now describe the same database.

**Note for the tests.** `conftest.py` builds its schema with
`Base.metadata.create_all` against SQLite, which stays correct - the tests never
exercise the migrations. Its three `patch.object(Base.metadata, "create_all")`
guards were removed, since the lifespan hook no longer calls it and a patch
against nothing only implies a coupling that has gone.

Worth holding onto: the suite cannot catch a broken migration chain, which is
exactly why this went unnoticed. If that is ever worth closing, it takes a test
that runs `alembic upgrade head` against a real Postgres - SQLite will not do,
because the failure was a foreign key to an absent table.

---

## P0-2. Nothing stops the bypass flags from being true in production

- [x] Add a production guard to `Settings` — done in `adc2545`

`AUTH_BYPASS=true` makes every request without an `Authorization` header a full
admin. `STRIPE_BYPASS=true` creates orders inline without ever charging a card.
Both default to `false`, but a copied `.env`, a stale task definition, or a
mistyped parameter silently re-enables them, and nothing in the app objects.

There is no detectable symptom. The app looks healthy while being completely
open, and with `AUTH_BYPASS` on, the admin API is reachable by anyone who finds
the URL.

**The fix.** A `model_validator` on `Settings` that refuses to start:

```python
@model_validator(mode="after")
def _production_guardrails(self):
    if self.app_env.lower() in {"prod", "production"}:
        if self.auth_bypass:
            raise ValueError("AUTH_BYPASS must be false when APP_ENV=prod")
        if self.stripe_bypass:
            raise ValueError("STRIPE_BYPASS must be false when APP_ENV=prod")
        if not self.stripe_secret_key or not self.stripe_webhook_secret:
            raise ValueError("Stripe keys are required when APP_ENV=prod")
    return self
```

Failing to boot is the correct behaviour: a deployment that will not start gets
noticed immediately, where one that starts wide open does not.

**Verify.** A unit test asserting `Settings(app_env="prod", auth_bypass=True,
...)` raises. Then set `APP_ENV=prod` in App Runner and confirm the service
comes up.

---

## P0-3. There is no Dockerfile

- [x] Add `Biofarm_Backend/Dockerfile` and a `.dockerignore` — done in `adc2545`

App Runner deploys a container image; none exists yet. Requirements:

- `python:3.13-slim` base, non-root user
- `pip install --no-cache-dir -r requirements.txt` as its own layer, before the
  app copy, so code changes do not reinstall dependencies
- Entrypoint runs `alembic upgrade head` and then `uvicorn app.main:app --host
  0.0.0.0 --port 8000`. Migrating in the entrypoint is the pragmatic choice
  here: RDS is private, so a CI runner cannot reach it. Alembic takes a lock, so
  a brief two-instance overlap during a rolling deploy is safe at this scale.
- `.dockerignore` must exclude `.venv/`, `.env`, `__pycache__/`, `.git/` - `.env`
  in particular must never enter an image layer.

**Verify.** `docker build` then `docker run` against the local Postgres, and
confirm `/api/v1/health` answers.

---

## P1-1. `echo=True` logs every statement to CloudWatch

- [x] Make SQL echo conditional — done in `adc2545`

`app/db/session.py` has `create_engine(settings.database_url, echo=True)`. In
production that writes every SQL statement *and its bound parameters* to
CloudWatch Logs - customer emails, names, phone numbers, and shipping addresses
in plain text, plus an ingestion bill for the volume.

```python
engine = create_engine(settings.database_url, echo=settings.app_env == "dev")
```

---

## P1-2. The connection pool is not configured for a remote database

- [x] Add `pool_pre_ping=True` and a `pool_recycle` — done in `adc2545`

Against localhost, a connection lives as long as the process. Against RDS
through a NAT instance, idle connections get dropped by the database's own
timeout and by NAT idle timeouts, and SQLAlchemy hands the application a dead
one - surfacing as an intermittent `OperationalError` on the first request after
a quiet period, typically overnight and never reproducible on demand.

```python
engine = create_engine(
    settings.database_url,
    echo=settings.app_env == "dev",
    pool_pre_ping=True,     # cheap liveness check on checkout
    pool_recycle=1800,      # recycle before RDS or NAT drops it
    pool_size=5,
    max_overflow=5,
)
```

Keep the pool small. `db.t4g.micro` allows on the order of 80 connections, and
they are shared with anything else that connects.

---

## P1-3. `cleanup_stale_checkout_sessions` runs in the lifespan hook

- [x] Move it to `app.jobs.cleanup` — done in `adc2545`; still needs an EventBridge schedule at deploy time

It runs on every process start, so it fires on each deploy, each scale-out, and
each restart - doing a table scan and a delete at exactly the moment the service
is trying to become healthy. It is a daily housekeeping task wearing a startup
hook's clothing.

Expose it as a CLI entrypoint (`python -m app.jobs.cleanup`) and run it on an
EventBridge schedule. Remove it from `lifespan` at the same time you remove
`create_all`.

---

## P1-4. The Stripe webhook reports success when it silently does nothing

- [x] Fail loudly when the checkout session is missing — done in `adc2545`

`stripe_webhook.py` calls `create_order_from_checkout_session(db, pi.id)` and
ignores the result. That function returns `None` when no `CheckoutSession` matches
the payment intent - and the handler still returns `{"status": "ok"}`.

The failure mode: a customer is charged, no order row is created, Stripe records
a successful delivery, and nothing anywhere indicates a problem. You find out
when the customer emails you.

Log an error with the payment intent id, and return a 500 so Stripe retries -
its retry schedule is the free recovery mechanism you are currently declining.
Guard against the double-processing case first: the handler already runs for both
`amount_capturable_updated` and `succeeded`, so the second one legitimately finds
the session gone. Distinguish "already converted to an order" from "never existed"
before deciding to fail.

---

## P1-5. `stripe` is the only unpinned dependency

- [x] Pinned to `stripe==15.6.1` — done in `adc2545`

`requirements.txt` pins all 44 other packages exactly and then has
`stripe>=7.0.0`. A rebuild months from now can pull a different major version.
This mattered more than usual because `stripe_webhook.py` catches
`stripe.error.SignatureVerificationError` and that import path has moved between
majors. **Checked:** `>=7.0.0` resolved to 15.6.1, where `stripe.error` survives
as an alias of `stripe._error`, so the handler was not broken - the risk was a
future rebuild, not the present. Pinned to the version actually tested.

---

## P1-6. `CORS_ORIGINS` still points at localhost

- [ ] Set it to the deployed frontend origin

Defaults to `["http://localhost:5174"]`. The browser blocks every API call from
the Amplify domain until this is set. See the runbook - it cannot be set until
the Amplify domain exists, which is why the backend is deployed twice.

---

## P2-1. The health endpoint does not check the database

- [x] Split liveness from readiness — done in `adc2545`

`/api/v1/health` returns `{"status": "ok"}` unconditionally. As an App Runner
health check that means a service with a dead database stays "healthy" and keeps
receiving traffic.

Keep `/health` shallow for liveness, add `/health/ready` that runs `SELECT 1`,
and point App Runner at the readiness path. Do not make the readiness check
expensive - it runs continuously.

---

## P2-2. Image keys collide after a middle image is deleted

- [ ] Switch to non-reusable image keys

S3 keys are `products/{product_id}/{index}.{ext}` where
`index = len(image_urls) + 1`. Delete the second of three images and the next
upload computes index 3 - a key the third image still occupies, overwriting it.

Not launch-blocking, but it silently destroys data, and it gets harder to change
once real image URLs are stored in `product.image_urls`. A UUID or timestamp
suffix removes the class of bug entirely. Do it before there is a catalog worth
keeping.

---

## Suggested order

1. P0-1 migrations - everything about RDS depends on it
2. P0-2 production guard - a few lines, removes the worst outcome
3. P1-1, P1-2 engine config - same file, one change
4. P0-3 Dockerfile - needed before anything deploys
5. P1-4, P1-5 Stripe correctness
6. P1-3 scheduled job, P2-1 health check - alongside the infrastructure work
7. P1-6 CORS - during the runbook, not before
8. P2-2 image keys - before the catalog is populated for real

---

## Found while implementing

Three defects that were not in the original survey, each caught by running the
work rather than reading it.

### P0-4. `pip install -r requirements.txt` fails outright on Windows

- [x] Add a platform marker to `uvloop` — done in `adc2545`

`uvloop==0.22.1` was pinned unconditionally, and uvloop does not support Windows:
its build refuses with `RuntimeError: uvloop does not support Windows at the
moment`, and because pip resolves the whole file as a unit, **nothing** installs.
The documented setup path could not work on a Windows machine.

`uvloop==0.22.1; sys_platform != "win32"` keeps the speedup on Linux, where the
container runs, and lets Windows install everything else. Environment markers are
the intended mechanism for exactly this.

### P0-5. The test suite inherited the developer's `.env`

- [x] Pin the configuration in `conftest.py` — done in `adc2545`

`Settings` reads `Biofarm_Backend/.env`, and the tests construct it like anything
else. So the suite's behaviour depended on an untracked local file:
`AUTH_BYPASS=true` turned every "unauthenticated request is rejected" test from
pass to fail, and `STRIPE_BYPASS=true` changed which code path created an order.
Tests whose result depends on a file that is not in the repository are not tests,
and CI would have disagreed with every developer's machine.

`conftest.py` now sets the whole configuration through `os.environ` above the app
imports - environment variables outrank the `.env` file in pydantic-settings, and
`get_settings()` is `lru_cache`d so the first import wins.

This surfaced three tests that could not pass under either configuration. Two
were only failing on the inherited settings. The third,
`test_create_payment_intent_success`, patched `create_payment_intent` but not
`calculate_tax`: with bypass off it attempted a live Stripe Tax call and got a
502, and with bypass on its own final assertion (`order_id is None`) contradicted
inline order creation. It had no configuration in which it could pass.

### P1-7. A fresh Windows clone would produce a broken container

- [x] Add `.gitattributes` forcing LF — done in `f0fbde7`

The entrypoint is stored with LF, but Windows defaults `core.autocrlf` to true,
so a fresh clone checks it out as `#!/bin/sh`. Docker bakes that in, and the
container dies with `no such file or directory` naming an entrypoint that plainly
exists - because the kernel is looking for an interpreter called `/bin/sh`.

Nothing was broken yet; the local working copy had LF, which is why the build
succeeded. It would have broken on the next clone, with an error that points
nowhere near the cause.

---

## Remaining

- **P1-6** `CORS_ORIGINS` - deliberately deferred; it cannot be set until the
  Amplify domain exists. Phase 5 of the runbook.
- **P2-2** image key collisions - still open. Worth doing before the catalog is
  populated for real.
- **P1-3** the cleanup job exists as `python -m app.jobs.cleanup`, but still needs
  an EventBridge schedule pointing at it. Phase 2 of the runbook.
