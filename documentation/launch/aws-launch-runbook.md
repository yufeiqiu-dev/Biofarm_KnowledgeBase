# AWS launch runbook

Taking Biofarm from "runs on a laptop" to "runs on a fresh AWS account", in the
order the dependencies actually allow.

Read `pre-launch-code-fixes.md` first. Phases 3 onward assume the P0 items there
are done - in particular, the migration chain does not currently work against a
fresh database, and RDS will be a fresh database.

## Target architecture

```
                      Internet
                         |
        +----------------+-----------------+
        |                                  |
   Amplify Hosting                   App Runner (public ingress)
   (static SPA build)                 FastAPI container from ECR
        |                                  |
        |                          VPC connector (egress)
        |                                  |
        |                     +------------+------------+
        |                     |                         |
        |              private subnets            public subnet
        |                     |                         |
        |              RDS PostgreSQL 16          NAT instance (t4g.nano)
        |               (private, TLS)                   |
        |                                          Internet: Stripe API,
        +---- images ----> CloudFront -> S3        Cognito JWKS, AWS APIs
                                                          
   Cognito user pool  <---- browser login (hosted UI)
```

**Decided:** NAT instance rather than NAT Gateway (~$7/month against ~$33), and
App Runner rather than Lambda. Once NAT is being paid for either way, Lambda's
scale-to-zero saves only a few dollars, and App Runner runs the FastAPI app
unmodified - no Mangum, no cold starts on a storefront, and no API Gateway
payload encoding between Stripe and the raw request body its signature check
needs.

**The tradeoff you are accepting with a NAT instance:** it is a single EC2
instance you own. If it dies, the backend loses all outbound internet - Stripe
calls and Cognito token verification both fail - until it comes back. Put it in
an autoscaling group of 1 so it self-heals, and know that this is the thing to
check first when the backend starts failing for no apparent reason.

## Rough monthly cost

Approximate us-east-2 on-demand, excluding free tier. Verify before committing.

| | ~USD/mo |
|---|---|
| RDS `db.t4g.micro`, 20 GB gp3, single-AZ | 14 |
| NAT instance `t4g.nano` + EBS + public IPv4 | 7 |
| App Runner, 0.25 vCPU / 0.5 GB idle | 3-6 |
| Amplify Hosting (build + low traffic) | 1-2 |
| S3 + CloudFront (low traffic) | 1-2 |
| Secrets Manager (2 secrets) | 1 |
| ECR, Cognito (under free tier) | ~0 |
| **Total** | **~30-40** |

A new account's 12-month free tier covers the RDS instance, which takes this
closer to $20 for the first year.

---

## Phase 0 - Decisions before you start

- [ ] **Region.** Everything in one, `us-east-2` unless there is a reason.
- [ ] **Domain.** Amplify and App Runner both hand out default domains, and those
      work end to end. If you want a custom domain, set it up *before* Phase 5 -
      the domain is baked into Cognito callback URLs, `CORS_ORIGINS`, S3 CORS,
      and the frontend build, so changing it later means redoing all four.
- [ ] **Stripe mode.** Launch against test keys, verify the full flow, then swap
      to live keys and re-register the webhook. Test and live have separate
      webhook endpoints and separate signing secrets.
- [ ] **Credentials.** Admin access to the new account via `ada`, and the AWS CLI
      installed.

---

## Phase 1 - Code fixes

- [ ] Work through `pre-launch-code-fixes.md`, at minimum every P0.
- [ ] Confirm on a clean local volume: `alembic upgrade head`, then
      `alembic check` reporting no changes, then the test suite green.

Do not start Phase 3 until this passes. A backend that cannot build its own
schema will fail in App Runner in a way that looks like a networking problem.

---

## Phase 2 - Network, database, registry

Everything here is one-time account setup.

- [ ] **VPC** with two public and two private subnets across two AZs. Two AZs is
      an RDS subnet-group requirement even for a single-AZ instance.
- [ ] **NAT instance**: `t4g.nano` in a public subnet.
      - Disable the source/destination check - without this it silently drops
        forwarded traffic, and nothing tells you why.
      - Route `0.0.0.0/0` in the private subnets' route table to its ENI.
      - Security group: allow inbound from the private subnet CIDRs, allow all
        outbound.
      - Use the `fck-nat` community AMI, which is purpose-built and handles the
        iptables masquerade for you, or configure Amazon Linux 2023 by hand.
      - Put it in an autoscaling group with min=max=1 so it replaces itself.
- [ ] **RDS PostgreSQL 16**, `db.t4g.micro`:
      - Private subnet group, **not** publicly accessible
      - Security group allowing 5432 only from the App Runner VPC connector's SG
      - `rds.force_ssl=1` in the parameter group
      - Storage encrypted, automated backups 7 days, deletion protection on
      - Master password generated straight into Secrets Manager
- [ ] **Secrets**: `STRIPE_SECRET_KEY` and the DB password in Secrets Manager;
      the non-sensitive settings in SSM Parameter Store, which is free.
      `STRIPE_WEBHOOK_SECRET` gets created in Phase 6 - leave a placeholder.
- [ ] **ECR** repository for the backend image.
- [ ] **S3, CloudFront, Cognito, IAM**: run the existing provisioner against the
      new account rather than doing this by hand.

```bash
python scripts/local-setup/provision_aws.py --dry-run --region us-east-2
python scripts/local-setup/provision_aws.py --region us-east-2 --admin-email you@example.com
```

It is idempotent and account-derived, so it creates a distinct bucket, pool, and
distribution in the new account. **Do not point it at the frontend origin yet** -
that is Phase 5, once the Amplify domain exists.

---

## Phase 3 - Backend

- [ ] Build and push the image:

```bash
aws ecr get-login-password --region us-east-2 | docker login --username AWS --password-stdin <acct>.dkr.ecr.us-east-2.amazonaws.com
docker build -t biofarm-backend Biofarm_Backend/
docker tag biofarm-backend:latest <acct>.dkr.ecr.us-east-2.amazonaws.com/biofarm-backend:latest
docker push <acct>.dkr.ecr.us-east-2.amazonaws.com/biofarm-backend:latest
```

- [ ] Create the App Runner service: image from ECR, port 8000, VPC connector on
      the private subnets, health check on `/api/v1/health/ready`.
- [ ] Environment: `APP_ENV=prod`, `AUTH_BYPASS=false`, `STRIPE_BYPASS=false`,
      `DATABASE_URL` assembled from the RDS endpoint and the Secrets Manager
      password, plus the Cognito, S3, and CloudFront values from Phase 2.
      With `APP_ENV=prod` the guard from P0-2 refuses to start if the bypass
      flags are wrong - that is the point.
- [ ] Deployment runs `alembic upgrade head` from the entrypoint. Watch the logs
      and confirm it applied rather than skipped.
- [ ] **Record the App Runner default domain.** It is the API base URL the
      frontend needs next.

Sanity check before moving on:

```bash
curl https://<app-runner-domain>/api/v1/health       # {"status":"ok"}
curl https://<app-runner-domain>/api/v1/products     # [] - empty, but 200 and JSON
```

An empty array here is success: it proves the container is up, reached RDS
through the VPC connector, and the migration built the tables.

---

## Phase 4 - Frontend

- [ ] Connect `Biofarm_Frontend` to Amplify Hosting, branch `main`.
- [ ] Set build environment variables. These are **build-time** - Vite inlines
      `VITE_*` into the bundle, so changing one requires a rebuild, not a
      restart:
      - `VITE_API_BASE_URL=https://<app-runner-domain>/api/v1` - the `/api/v1`
        suffix is required; without it every request resolves against the
        Amplify origin and returns the SPA shell instead of JSON
      - the four `VITE_COGNITO_*` values from the new pool
      - `VITE_STRIPE_BYPASS=false` and the publishable key
- [ ] Deploy, and **record the Amplify domain.**

---

## Phase 5 - Wire the two together

This is where the ordering bites. The frontend build needs the backend's URL,
and the backend's CORS needs the frontend's URL - so each is deployed twice.
Both URLs now exist, so close the loop:

- [ ] **Cognito**: add `https://<amplify-domain>/auth/callback` to both the
      allowed callback URLs and the sign-out URLs. It must match the frontend's
      `VITE_COGNITO_REDIRECT_SIGN_IN` character for character - a trailing slash
      is enough to break login, and the error Cognito returns does not say so.
- [ ] **Backend CORS**: set `CORS_ORIGINS=["https://<amplify-domain>"]` and
      redeploy App Runner.
- [ ] **S3 CORS**: re-run the provisioner with the real origin so browser uploads
      work from the deployed admin console:

```bash
python scripts/local-setup/provision_aws.py --region us-east-2 \
  --frontend-origin https://<amplify-domain>
```

- [ ] **Frontend**: set the `VITE_COGNITO_REDIRECT_*` values to the Amplify
      domain and rebuild.
- [ ] Add an admin user to the `Admin` group in the new pool - the group name is
      case-sensitive on both sides.

---

## Phase 6 - Stripe

- [ ] Register the webhook endpoint:
      `https://<app-runner-domain>/api/v1/stripe/webhook`
- [ ] Subscribe exactly the three events the handler acts on:
      `payment_intent.amount_capturable_updated`, `payment_intent.succeeded`,
      `payment_intent.canceled`.
- [ ] Put the signing secret into Secrets Manager as `STRIPE_WEBHOOK_SECRET` and
      redeploy. It differs between test and live mode, and again from whatever
      `stripe listen` printed locally.
- [ ] **Configure the Stripe Tax origin address** in the Stripe Dashboard.
      Without it, `calculate_tax` throws and checkout returns a 502 - the code
      says as much in the error, but only after a customer has hit it.
- [ ] Confirm `STRIPE_BYPASS=false` on the backend and `VITE_STRIPE_BYPASS=false`
      on the frontend. These select different code paths, not just a mock: in
      bypass the order is created inline, in live mode only the webhook creates
      it. Mismatched, checkout appears to succeed and no order ever appears.

---

## Phase 7 - Data

**Schema** comes from `alembic upgrade head` in Phase 3. Nothing else to do.

**Catalog data** is the awkward part, because RDS is private - your laptop
cannot reach it. Options, cheapest effort first:

1. **Re-enter the catalog through the admin console.** For a small product list
   this is genuinely the fastest path and avoids everything below.
2. **Load through a bastion.** Start a small EC2 instance in a private subnet,
   connect with SSM Session Manager (no SSH key, no public IP), and
   `pg_dump --data-only` from local into `psql` there. Terminate it afterwards.

> **Image URLs do not survive an account move.** `product.image_urls` stores
> absolute CloudFront URLs. A new AWS account means a new bucket and a new
> distribution, so every row you copy will point at a domain the new account does
> not own, and every product image will be broken. Nothing rewrites them. Either
> re-upload the images through the admin UI after loading the rows, or rewrite
> the URLs during the load. This is the strongest argument for option 1.

---

## Phase 8 - CI/CD

Your tests need no infrastructure - backend pytest runs on in-memory SQLite with
S3 and Stripe mocked, frontend is vitest and jsdom - so CI is cheap and fast.

- [ ] **GitHub OIDC role** in the AWS account. No stored AWS keys in GitHub.
- [ ] **Backend** (`Biofarm_Backend`, push to `main`):
      `pytest` -> `docker build` -> push to ECR -> App Runner auto-deploys on
      push. Migrations run from the container entrypoint.
- [ ] **Frontend**: Amplify builds on push to `main` with no extra pipeline. Turn
      on PR previews - per-branch, and cheap.
- [ ] **Knowledge base**: run `scripts/local-setup/tests/test_provision.py` on
      push. It needs nothing but Python.
- [ ] Make the backend pipeline fail on a red test rather than deploying anyway.

---

## Phase 9 - Go-live checklist

Verify by doing, not by reading config:

- [ ] `GET /api/v1/health/ready` returns 200 (proves the database is reachable)
- [ ] `GET /api/v1/products` returns the real catalog over HTTPS
- [ ] Sign up a fresh user through the hosted UI, confirm the email, sign in
- [ ] That user can reach `/orders` but is redirected away from `/admin/products`
- [ ] An `Admin` group user can reach `/admin/products`
- [ ] Upload a product image and confirm it renders - if it 403s, the CloudFront
      distribution may still be deploying, which takes 5-15 minutes
- [ ] Complete a real checkout with Stripe test card `4242 4242 4242 4242`
- [ ] The order appears in `/admin/orders` - this is the webhook working
- [ ] Confirm, ship, and deliver that order; check the payment captures in Stripe
      at the ship step, not before
- [ ] Cancel a second order and confirm the authorization is voided
- [ ] Backend logs contain no SQL statements (P1-1 done) and no secrets
- [ ] `AUTH_BYPASS` and `STRIPE_BYPASS` are false - confirm by calling an admin
      endpoint with no token and getting a 401
- [ ] RDS is not publicly accessible, backups on, deletion protection on
- [ ] Kill the NAT instance and confirm the ASG replaces it

---

## Phase 10 - Rollback

- **Backend**: App Runner keeps previous deployments; roll back by deploying the
  prior ECR image tag. Tag images with the commit SHA, not just `latest`, or you
  will have nothing to roll back to.
- **Frontend**: Amplify keeps build history and redeploys a previous build in
  place.
- **Database**: migrations are the one-way door. Alembic `downgrade` exists but
  is only as good as the `down_revision` functions, which are rarely tested.
  Take an RDS snapshot before any deploy carrying a migration - it is the real
  rollback path.

---

## Known gotchas, collected

| Symptom | Cause |
|---|---|
| Backend won't start, validation error naming a field | A required setting is missing; `Settings` has no defaults for the AWS and Cognito values |
| Backend starts, every API call blocked in the browser | `CORS_ORIGINS` still localhost (Phase 5) |
| API calls return the SPA HTML | `VITE_API_BASE_URL` missing the `/api/v1` suffix |
| Login bounces with a generic error | Cognito callback URL differs from the frontend's, often by a trailing slash |
| Login does nothing at all | App client was created with a secret; Amplify PKCE cannot use one |
| Images 403 for the first quarter hour | CloudFront distribution still deploying |
| All product images broken after a data copy | `image_urls` point at the old account's CloudFront domain |
| Checkout succeeds, no order appears | Bypass flags disagree, or the webhook is not registered or has the wrong signing secret |
| Checkout returns 502 on tax | Stripe Tax origin address not configured |
| Intermittent database errors after idle | `pool_pre_ping` not set (P1-2) |
| Backend suddenly cannot reach Stripe or Cognito | NAT instance is down |
