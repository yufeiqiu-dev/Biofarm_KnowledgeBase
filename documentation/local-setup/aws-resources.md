# AWS resources

What `scripts/provision_aws.py` builds, why each piece is shaped the way it is,
how to find the same values in the console, and how to take it all down.

## The stack

```
Browser ──presigned PUT──▶ S3 (private)
                             ▲
                             │ OAC-signed GET (bucket policy allows only this distribution)
Browser ──image GET────▶ CloudFront

Browser ──hosted UI login──▶ Cognito user pool ──JWT──▶ FastAPI (verifies against the pool's JWKS)
FastAPI ──presign / delete──▶ S3, as the IAM user
```

Product images never pass through the backend. It only signs URLs, so the IAM
user needs `PutObject`/`GetObject`/`DeleteObject` on the image prefix and nothing
else.

| Resource | Name the script derives | Why |
|---|---|---|
| S3 bucket | `biofarm-images-<account-id>` | Bucket names are globally unique; the account id makes the name deterministic, so a re-run finds it again without a state file. |
| Public access block | on the bucket | Images are served through CloudFront only. `BlockPublicPolicy` stays **off** because the OAC grant is itself a bucket policy. |
| CORS rule | `GET`/`PUT` from the frontend origin, exposing `ETag` | The browser PUTs directly to S3 with a presigned URL; without this the upload fails at the preflight. |
| Origin access control | `biofarm-images-oac` | Lets CloudFront sign requests to a private bucket. Replaces the legacy OAI. |
| CloudFront distribution | comment `Biofarm product images` | Serves images over HTTPS with caching. `PriceClass_100` (NA + EU) is the cheap tier. |
| Bucket policy | allows `cloudfront.amazonaws.com` | Scoped by `AWS:SourceArn` to this one distribution, so another account's distribution can't read the bucket. |
| IAM user | `biofarm-backend` | The backend's credentials. Inline policy `BiofarmS3ImageAccess`, scoped to `<bucket>/products/*`. |
| Cognito user pool | `biofarm-users` | Email sign-in, email auto-verification, self-registration on. |
| App client | `biofarm-web` | **No client secret** — Amplify's browser PKCE flow cannot use one. Authorization-code grant, scopes `openid email profile`. |
| Hosted UI domain | `biofarm-<account-id>` | Prefix is globally unique across all AWS accounts; override with `--domain-prefix` if taken. |
| Group | `Admin` | `AdminRoute` and `require_admin` both check for this exact string. Case-sensitive. |

## Running it

```bash
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...

python scripts/provision_aws.py --dry-run --region us-east-2        # plan only
python scripts/provision_aws.py --region us-east-2 --admin-email you@example.com
```

The calling principal needs create/read permissions on S3, CloudFront, IAM, and
Cognito. An account admin is simplest for a test account; for anything shared,
scope a role to those four services rather than reusing an admin key.

Useful flags:

- `--profile <name>` — use an AWS profile instead of exported keys.
- `--bucket-name` / `--domain-prefix` — override the derived names when one collides.
- `--frontend-origin` / `--callback-url` — for a deployed origin rather than localhost.
- `--admin-email` (+ `--admin-password`) — also create a confirmed user in the `Admin` group, which is what makes a fresh account immediately usable.
- `--teardown` — delete what the state file records, after typing the account id to confirm.

Results land in `.aws-provision-state.json` at the workspace root and in the two
`.env` files. The state file records ids, never the IAM secret.

## Things that trip people up

**The IAM secret appears once.** AWS returns it only from `CreateAccessKey`, so
the script writes it straight to `Biofarm_Backend/.env`. If it's lost, delete the
key and re-run to mint a replacement. The script reuses the key already in `.env`
when it's still live, and deletes the oldest key when the 2-per-user limit is hit
— so re-running is safe but not free of side effects.

**CloudFront takes 5–15 minutes.** The domain name exists immediately and the
script returns as soon as it does. Uploads work right away; image *display*
returns errors until the distribution reaches `Deployed`. Check with
`aws cloudfront get-distribution --id <id> --query 'Distribution.Status'`, or the
console. This is the single most common "the script is broken" report, and it
isn't.

**Cognito domain prefixes are globally unique.** Not per-account — across all of
AWS. A collision surfaces as `InvalidParameterException`; the script catches it
and tells you to pass `--domain-prefix`.

**Changing `CLOUDFRONT_URL` orphans existing images.** `product.image_urls`
stores absolute URLs built from this value at upload time. Pointing at a new
distribution leaves old rows referencing the old domain — they don't rewrite
themselves.

**A client secret breaks login silently.** If you create the app client by hand,
leave "Generate a client secret" off. Amplify's PKCE flow fails without a
meaningful error when a secret is present.

## Finding these values in the console

- **User pool ID** — Cognito → User pools → your pool, top of the overview.
- **App client ID** — that pool → App integration → App clients.
- **Hosted UI domain** — that pool → App integration → Domain. Copy the host only.
- **Bucket name** — S3 → Buckets.
- **CloudFront domain** — CloudFront → Distributions → *Distribution domain name*; prefix `https://`.
- **IAM keys** — IAM → Users → `biofarm-backend` → Security credentials.

## Cost

At development traffic this is roughly free: Cognito's free tier covers far more
monthly active users than a test account will have, S3 and CloudFront bill on
storage and transfer measured in cents, and IAM is free. A CloudFront
distribution left running costs nothing when idle — but tear down the stack when
you abandon an account, so nothing lingers with live credentials.

## Teardown

```bash
python scripts/provision_aws.py --teardown
```

Disables and deletes the distribution (the disable-then-wait cycle is why this
takes several minutes), empties and deletes the bucket, deletes the IAM user's
keys and inline policies then the user, and deletes the Cognito domain and pool.
Deleting the pool destroys every user account in it.

The `.env` files keep their old values afterwards — re-run provisioning to
replace them.
