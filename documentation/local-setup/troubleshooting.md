# Troubleshooting

Failures this stack actually produces, matched to their causes. Several present
with a misleading symptom, which is why guessing tends to cost more time than
reading.

Start with `python scripts/doctor.py` — it catches most of the environment-level
causes below before you go hunting.

## Backend won't start

**`ValidationError: N validation errors for Settings`, naming fields.**
Those fields have no default in `app/core/config.py`, so pydantic-settings raises
at import. The error names exactly what's missing. Fill them in
`Biofarm_Backend/.env`; `references/env-vars.md` says where each comes from.
Note the `.env` must sit in `Biofarm_Backend/`, not the workspace root — the
model resolves `env_file=".env"` relative to the process's working directory, so
launching uvicorn from the wrong directory produces this same error with a
perfectly good file one level away.

**`ModuleNotFoundError: No module named 'psycopg'`.**
Dependencies aren't installed, or you're running the system python rather than
the venv's. Use `.venv/Scripts/python.exe -m uvicorn ...` explicitly.

**`connection to server at "localhost", port 5432 failed`.**
Postgres isn't up. `docker compose -f assets/docker-compose.yml up -d`, then wait
for `docker inspect --format "{{.State.Health.Status}}" biofarm-postgres` to say
`healthy`. If Docker itself is unreachable, Docker Desktop is installed but not
running — `docker --version` succeeds in that state, which is what makes it
confusing; `docker info` is the check that tells the truth.

**`sqlalchemy.exc.NoSuchModuleError: Can't load plugin: sqlalchemy.dialects.postgresql.psycopg`.**
`DATABASE_URL` uses `postgresql://` instead of `postgresql+psycopg://`. Only the
v3 driver is installed.

## Frontend won't start or can't reach the API

**Vite exits with "Port 5174 is in use".**
`strictPort` is on, so it refuses to slide to another port. Free 5174 rather than
changing it — the port is embedded in the Cognito callback URLs and the backend's
CORS allowlist, so moving it means updating both.

**Requests return HTML instead of JSON, or 404 on `/products`.**
`VITE_API_BASE_URL` is unset or missing the `/api/v1` suffix, so relative paths
resolve against the Vite dev server, which answers every unknown path with the
SPA shell. Set it to `http://127.0.0.1:8000/api/v1` and restart the dev server —
Vite inlines env values at startup, so editing `.env` mid-session changes nothing.

**CORS error in the console.**
The backend's `cors_origins` defaults to `http://localhost:5174` exactly. Reaching
the app on `127.0.0.1:5174` is a *different* origin to the browser and is blocked.
Use `localhost`, or add the other origin to `CORS_ORIGINS`.

## Login problems

**Hosted UI shows "redirect_mismatch" or bounces back to an error.**
The callback URL must match character for character between
`VITE_COGNITO_REDIRECT_SIGN_IN` and the pool's *Allowed callback URLs* — a
trailing slash or `http` vs `https` is enough to break it, and Cognito reports it
generically.

**Login appears to do nothing, no error.**
Usually the app client was created with a client secret. Amplify's browser PKCE
flow can't use one and fails quietly. Recreate the client with the secret off.

**`VITE_COGNITO_DOMAIN` with a scheme.**
Store the host only (`biofarm-123.auth.us-east-2.amazoncognito.com`). Amplify
prepends `https://`, so including it yields a malformed URL.

**Signed in, but `/admin/*` redirects away.**
The user isn't in the `Admin` group, or the group is spelled differently.
`AdminRoute` checks `user.roles.includes("Admin")` and the backend checks the
same string in `cognito:groups` — both case-sensitive. Add the user under
Cognito → Users → the user → Groups, then sign out and back in: group membership
is a token claim, so an existing session won't pick it up.

**New signup can't sign in.**
Self-registration is off on the pool, or the email was never verified. The
provisioner sets `AllowAdminCreateUserOnly: false` and auto-verifies email; a
hand-made pool often doesn't.

## Images

**Uploads succeed, images render broken.**
Most likely the distribution hasn't finished deploying — it takes 5–15 minutes
after creation and this is expected, not a fault. After that, check the bucket
policy actually references the distribution's ARN, and that `CLOUDFRONT_URL` in
`.env` matches the distribution's domain.

**403 on the presigned PUT.**
The bucket's CORS rule doesn't list the origin you're browsing from, or the IAM
user's policy doesn't cover the key. The policy is scoped to
`<bucket>/products/*`, which is where the backend writes.

**An image disappeared after deleting a different one.**
Keys are `products/{product_id}/{index}.{ext}` with `index = len(image_urls) + 1`,
so indices are reused after a middle image is deleted and a later upload can land
on a key another image still occupies. Known behavior of the current scheme, not
a setup problem.

## Checkout

**Payment "succeeds" but no order exists; the success page spins then times out.**
The two bypass flags disagree, or the backend never got the webhook. In bypass
mode the order is created inline by the checkout request; with real Stripe it is
only created when `stripe listen` forwards the event. Confirm `STRIPE_BYPASS` and
`VITE_STRIPE_BYPASS` match, and that the CLI is running and its `whsec_...` is in
`.env`.

**Tax calculation returns 502.**
With real Stripe, Stripe Tax needs an origin address configured in the Dashboard.
The error text says so. In bypass mode tax is a flat 8.75% and never calls out.

## Product list is empty

The public listing only returns products that have at least one variant — a
product created without variants is invisible on `/products` but present in
`/admin/products`. That's the filter working, not a data problem.

## Schema changes don't take effect

There are no migrations. Tables are created by `Base.metadata.create_all` at
startup, which only creates *missing* tables and never alters existing ones. To
pick up a changed column locally, drop the volume and let it rebuild:

```bash
docker compose -f assets/docker-compose.yml down -v
docker compose -f assets/docker-compose.yml up -d
```

This deletes all local data, which is fine locally and is the reason a real
migration tool becomes necessary before there's a database anyone cares about.
