# Environment variables

Every value the app reads, which repo it belongs to, and where it comes from. The
authoritative definitions are `Biofarm_Backend/app/core/config.py` and the
`import.meta.env` reads in `Biofarm_Frontend/src/` — if this table and the code
disagree, the code wins and this file needs updating.

## Backend — `Biofarm_Backend/.env`

`Settings` is a pydantic-settings model. Fields without a default are **required
at import time**: a missing one crashes uvicorn on startup with a validation
error naming the field, before any request is served.

| Variable | Required | Where it comes from |
|---|---|---|
| `DATABASE_URL` | yes | Local: `postgresql+psycopg://biofarm:biofarm@localhost:5432/oasis` for the compose file. Must use the `psycopg` (v3) driver — plain `postgresql://` picks psycopg2, which is not installed. |
| `COGNITO_REGION` | yes | Region of the user pool, e.g. `us-east-2`. |
| `COGNITO_USER_POOL_ID` | yes | Cognito → User pools → your pool → *User pool ID* (`us-east-2_XXXXXXXXX`). |
| `COGNITO_USER_POOL_CLIENT_ID` | dev: optional<br>prod: **yes** | The same value as the frontend's `VITE_COGNITO_USER_POOL_CLIENT_ID`. The backend rejects any access token that was not issued to this app client. Leave it unset and that check is skipped, which is why `Settings` refuses to boot without it under `APP_ENV=prod`. If the two repos disagree, every authenticated request answers 401 with *"Token was not issued for this application"*. |
| `AWS_REGION` | yes | Region of the S3 bucket. Normally the same as `COGNITO_REGION`. |
| `S3_BUCKET_NAME` | yes | S3 → your bucket name. |
| `CLOUDFRONT_URL` | yes | CloudFront → Distributions → *Distribution domain name*, prefixed with `https://`. No trailing slash — the code strips one, but the stored image URLs are built from this so changing it later orphans existing images. |
| `AWS_ACCESS_KEY_ID` | yes | IAM → Users → your backend user → Security credentials → Access keys. |
| `AWS_SECRET_ACCESS_KEY` | yes | Shown **once**, at key creation. Lost secrets can't be recovered — delete the key and make a new one. |
| `AUTH_BYPASS` | no (`false`) | `true` locally. Requests without an `Authorization` header get a synthetic admin identity; requests *with* a bearer token are still fully verified. |
| `STRIPE_SECRET_KEY` | no (`""`) | Stripe Dashboard → Developers → API keys (`sk_test_...`). Only needed when `STRIPE_BYPASS=false`. |
| `STRIPE_WEBHOOK_SECRET` | no (`""`) | Printed by `stripe listen --forward-to localhost:8000/api/v1/stripe/webhook` (`whsec_...`). Changes each time you start a new listen session. |
| `STRIPE_BYPASS` | no (`false`) | `true` locally. Must match `VITE_STRIPE_BYPASS`. |
| `APP_ENV` | no (`dev`) | Informational. |
| `CORS_ORIGINS` | no (`["http://localhost:5174"]`) | JSON array. Add the deployed origin when the frontend is hosted elsewhere. |

## Frontend — `Biofarm_Frontend/.env`

Vite only exposes variables prefixed `VITE_`, and it inlines them at **build
time** — changing one requires restarting `npm run dev`, and a production build
bakes in whatever was set when it ran.

| Variable | Where it comes from |
|---|---|
| `VITE_API_BASE_URL` | `http://127.0.0.1:8000/api/v1` locally. Must include `/api/v1`: the API modules use paths relative to it, so omitting the prefix silently sends requests to the Vite dev server, which returns the HTML shell instead of JSON. |
| `VITE_COGNITO_USER_POOL_ID` | Same value as the backend's `COGNITO_USER_POOL_ID`. |
| `VITE_COGNITO_USER_POOL_CLIENT_ID` | Cognito → your pool → App integration → App clients → *Client ID*. |
| `VITE_COGNITO_DOMAIN` | Cognito → your pool → App integration → Domain. Host only, **no** `https://` — Amplify prepends the scheme, and including it produces a malformed redirect. |
| `VITE_COGNITO_REDIRECT_SIGN_IN` | `http://localhost:5174/auth/callback`. Must match a Cognito *Allowed callback URL* character for character. |
| `VITE_COGNITO_REDIRECT_SIGN_OUT` | Same URL as sign-in in this app; must be in *Allowed sign-out URLs*. |
| `VITE_STRIPE_PUBLISHABLE_KEY` | Stripe Dashboard → API keys (`pk_test_...`). Only read when bypass is off. |
| `VITE_STRIPE_BYPASS` | `true` locally. Must match the backend's `STRIPE_BYPASS`. |

## Why the two bypass flags have to agree

They don't just toggle a mock — they select different code paths on each side.

With `STRIPE_BYPASS=true`, `POST /orders/payment-intent` creates the order inline
and returns its `order_id`; there is no webhook. With it `false`, that endpoint
only stores a `CheckoutSession`, and the order is created later when Stripe calls
the webhook.

So a frontend in bypass talking to a real-Stripe backend gets a redirect to the
success page for an order that will never exist, and the reverse leaves the
frontend waiting for a Payment Element that the backend already settled. Neither
produces a useful error — set both, together.
