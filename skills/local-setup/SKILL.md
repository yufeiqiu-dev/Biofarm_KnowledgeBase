---
name: local-setup
description: Set up and run the Biofarm app (FastAPI backend + React/Vite frontend) on a local machine, including provisioning the AWS resources it depends on (Cognito user pool, S3 bucket, CloudFront distribution, IAM user) into any AWS account from scratch. Use this skill whenever someone wants to get Biofarm running locally, is onboarding to the project, hits a startup failure like a missing .env / "Settings validation error" / a connection-refused on 8000 or 5174, needs a fresh Cognito pool or S3 bucket, asks where an env value comes from, or wants to point the app at a different AWS account. Also use it when someone just says "run the app", "start the servers", or "why won't the backend boot" in this repo.
---

# Biofarm local setup

Get a machine from nothing to a running catalog, checkout, and admin console.

Two repos run as one app: `Biofarm_Backend/` (FastAPI, port 8000) and
`Biofarm_Frontend/` (Vite, port 5174). Both are needed - the frontend has no mock
backend.

Paths below are relative to this skill's own directory. In a checkout of
Biofarm_KnowledgeBase the same files live at `scripts/local-setup/` and
`documentation/local-setup/`; the scripts locate the app repos by searching
upward, so either location works without arguments.

## How to use this skill

Work through the phases in order. Each is idempotent, so re-running after a fix
is safe and is the normal way to recover. Start by seeing how far along the
machine already is:

```bash
python scripts/doctor.py
```

It prints one line per prerequisite with the exact fix command for anything
missing, and exits non-zero when something blocks. Use its output to decide which
phases to skip - a machine that already has a venv and a running database only
needs Phase 5.

## Phase 1 - Prerequisites

| Need | Check | Install on Windows |
|---|---|---|
| Python 3.11+ | `python --version` | `winget install Python.Python.3.13` |
| Node 20+ | `node --version` | `winget install OpenJS.NodeJS.LTS` |
| Docker | `docker info` | Docker Desktop - **start the app**, not just install it |

Node and Docker are the two that usually bite. `winget install` does not update
the PATH of an already-open shell, so after installing Node, open a new terminal
before continuing - otherwise `npm` still reads as missing and you will chase a
ghost.

Docker Desktop being installed is not the same as running. `docker --version`
answers from the CLI binary and succeeds while the daemon is down; `docker info`
is the check that actually proves the engine is up.

## Phase 2 - Database

Postgres runs as a container so it can be reset without touching the machine, and
so the version matches what RDS will run later.

```bash
docker compose -f scripts/docker-compose.yml up -d
docker inspect --format "{{.State.Health.Status}}" biofarm-postgres   # wait for: healthy
```

To wipe and start clean: `docker compose -f scripts/docker-compose.yml down -v`.
There are no migrations - the backend recreates tables on boot via `create_all` -
so dropping the volume is the supported way to reset schema after a model change.

If 5432 is already taken by another Postgres, change the host side of the port
mapping and match it in `DATABASE_URL`.

## Phase 3 - AWS resources

The backend will not start without AWS values: `Settings` declares
`s3_bucket_name`, `cloudfront_url`, `cognito_*`, and the AWS keys with no
defaults, so a missing one is an import-time crash, not a runtime warning.
Cognito is also the only way to sign in, so the admin console needs a real pool
even when `AUTH_BYPASS=true`.

**Provisioning a fresh account, or unsure what exists.** Run the provisioner. It
creates everything, is safe to re-run, and writes the results into both `.env`
files:

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

python scripts/provision_aws.py --dry-run --region us-east-2      # plan first
python scripts/provision_aws.py --region us-east-2 --admin-email you@example.com
```

`--admin-email` also creates a confirmed user in the `Admin` group, which is what
makes a fresh account immediately usable rather than merely provisioned. Pass
`--profile <name>` instead of exported keys if you use AWS profiles.

It needs boto3, which ships with the backend's requirements - run it with
`Biofarm_Backend/.venv/Scripts/python.exe` if your system python lacks it.

`references/aws-resources.md` covers what each resource is for, how to find the
values in the console, cost, and teardown. Read it when a step fails or when
adopting an account someone else set up.

**You already know your values.** Skip the script and fill the two `.env` files
by hand; `references/env-vars.md` lists every variable, which repo it belongs to,
and where in the console to find it.

Two things worth knowing before running it. The CloudFront distribution takes
5-15 minutes to reach `Deployed`; the script returns as soon as the domain name
exists, and image *uploads* work immediately while image *display* stays broken
until deployment finishes - that lag is expected, not a bug. And the IAM secret
access key is retrievable exactly once, at creation, so the script writes it
straight to `Biofarm_Backend/.env` and never to its state file. If it is lost,
delete the access key and re-run to mint a new one.

## Phase 4 - Install and configure

```bash
cd Biofarm_Backend && python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt
cd ../Biofarm_Frontend && npm install
```

On macOS or Linux the venv binary is `.venv/bin/python`.

The provisioner writes most `.env` values. Confirm the rest for local work:

- `AUTH_BYPASS=true` - the backend accepts unauthenticated requests as an admin,
  so you can exercise admin endpoints from `/docs` without a token. A request
  that *does* carry a bearer token is still fully verified.
- `STRIPE_BYPASS=true` (backend) and `VITE_STRIPE_BYPASS=true` (frontend) -
  **these two must agree.** They select genuinely different code paths: in
  bypass, checkout creates the order inline; with real Stripe, the order is only
  created when the webhook arrives. A mismatch produces a checkout that appears
  to succeed and never yields an order.
- `DATABASE_URL=postgresql+psycopg://biofarm:biofarm@localhost:5432/oasis` for
  the container above.

Both `.env` files are gitignored in their repos. Verify that still holds before
writing real credentials - `git -C Biofarm_Backend check-ignore -v .env` should
print a match.

## Phase 5 - Run

Two long-running processes, in separate shells or as background tasks:

```bash
cd Biofarm_Backend && .venv/Scripts/python.exe -m uvicorn app.main:app --reload
cd Biofarm_Frontend && npm run dev
```

Then verify rather than assume:

```bash
curl http://127.0.0.1:8000/api/v1/health                        # {"status":"ok"}
curl -s -o /dev/null -w "%{http_code}" http://localhost:5174    # 200
```

The frontend uses `strictPort`, so if 5174 is occupied Vite exits instead of
sliding to 5175 - free the port rather than changing it, since the port is baked
into the Cognito callback URLs and the backend's CORS allowlist.

A useful end-to-end check that touches every layer: open http://localhost:5174,
sign in through the hosted UI, add a product to the cart, and complete checkout
in bypass mode. That exercises Cognito, the API, Postgres, and the order flow in
one pass. If you provisioned with `--admin-email`, that account is already in the
`Admin` group and `/admin/products` should load.

## When something breaks

`references/troubleshooting.md` maps the failures this stack actually produces -
validation errors naming a missing setting, CORS rejections, the Cognito callback
mismatch, blank images from a still-deploying CloudFront, an empty product list -
to their causes. Read it before guessing, because several present with a
misleading symptom: a Cognito callback differing by a trailing slash fails as a
generic login error, and an unset `VITE_API_BASE_URL` shows up as requests to the
Vite dev server rather than as a missing-config message.

## Maintaining this skill

`scripts/tests/test_provision.py` exercises the provisioner against a fake AWS
(`tests/fakeaws/`) so the whole flow - env merging, resource ordering, policy
scoping - can be checked without an AWS account:

```bash
python scripts/tests/test_provision.py
```

Run it after any change to `provision_aws.py`. It is the only guard on that
script, since testing it for real means creating live infrastructure.

The backend's required-settings list, the port numbers, and the bypass flags all
live in code that changes: `app/core/config.py`, `vite.config.ts`, and the
`.env.example` files. When those change, update `references/env-vars.md` and the
provisioner's `.env` writer together - they encode the same contract from
opposite ends, and a mismatch surfaces as a confusing boot failure for the next
person onboarding.
