"""Exercise provision_aws.py against the fake AWS stub."""
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE.parent / "provision_aws.py"

# The fake AWS stub lives beside this file; putting it first on the path makes
# provision_aws.py import it instead of the real boto3, so the whole flow can be
# exercised without an AWS account.
sys.path.insert(0, str(HERE / "fakeaws"))

spec = importlib.util.spec_from_file_location("provision_aws", SCRIPT)
pa = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pa)

failures = []


def check(label, cond, extra=""):
    print(("  PASS  " if cond else "  FAIL  ") + label + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(label)


print("\n--- merge_env: creates a new file ---")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / ".env"
    pa.ROOT = Path(d)
    log = pa.Log(False)
    pa.merge_env(p, {"A": "1", "B": "2"}, log)
    text = p.read_text()
    check("writes both keys", "A=1" in text and "B=2" in text, text)

print("\n--- merge_env: updates in place, preserves comments and unrelated keys ---")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / ".env"
    pa.ROOT = Path(d)
    p.write_text("# my header\nKEEP=untouched\nA=old\n\n# trailing note\n")
    pa.merge_env(p, {"A": "new", "C": "added"}, pa.Log(False))
    text = p.read_text()
    check("updates existing key", "A=new" in text and "A=old" not in text, text)
    check("preserves unrelated key", "KEEP=untouched" in text, text)
    check("preserves comments", "# my header" in text and "# trailing note" in text, text)
    check("appends new key", "C=added" in text, text)
    check("does not duplicate A", text.count("A=") == 1, text)

print("\n--- merge_env: None values are skipped, not written as 'None' ---")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / ".env"
    pa.ROOT = Path(d)
    pa.merge_env(p, {"A": "1", "B": None}, pa.Log(False))
    text = p.read_text()
    check("skips None", "B=" not in text, text)
    check("no literal None", "None" not in text, text)

print("\n--- merge_env: a value containing '=' survives a round trip ---")
with tempfile.TemporaryDirectory() as d:
    p = Path(d) / ".env"
    pa.ROOT = Path(d)
    url = "postgresql+psycopg://u:p@localhost:5432/oasis?opt=1"
    pa.merge_env(p, {"DATABASE_URL": url}, pa.Log(False))
    check("reads back intact", pa.read_env_value(p, "DATABASE_URL") == url, pa.read_env_value(p, "DATABASE_URL"))

print("\n--- slug() ---")
check("lowercases and strips", pa.slug("My Project!") == "my-project", pa.slug("My Project!"))

print("\n--- full dry run against the stub ---")
import boto3  # the stub
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "Biofarm_Backend").mkdir()
    (root / "Biofarm_Frontend").mkdir()
    pa.ROOT = root
    pa.BACKEND_ENV = root / "Biofarm_Backend" / ".env"
    pa.FRONTEND_ENV = root / "Biofarm_Frontend" / ".env"
    pa.STATE_FILE = root / ".aws-provision-state.json"
    boto3.CALLS.clear()
    sys.argv = ["provision_aws.py", "--region", "us-east-2", "--dry-run", "--admin-email", "a@b.com"]
    rc = pa.main()
    check("dry run exits 0", rc == 0, str(rc))
    check("dry run writes no .env", not pa.BACKEND_ENV.exists(), "backend .env was created")
    check("dry run writes no state file", not pa.STATE_FILE.exists())
    mutating = [c for c in boto3.CALLS if c[1].startswith(("create_", "put_", "delete_", "admin_"))]
    check("dry run makes no mutating calls", not mutating, str(mutating[:3]))

print("\n--- full real run against the stub ---")
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "Biofarm_Backend").mkdir()
    (root / "Biofarm_Frontend").mkdir()
    pa.ROOT = root
    pa.BACKEND_ENV = root / "Biofarm_Backend" / ".env"
    pa.FRONTEND_ENV = root / "Biofarm_Frontend" / ".env"
    pa.STATE_FILE = root / ".aws-provision-state.json"
    boto3.CALLS.clear()
    sys.argv = ["provision_aws.py", "--region", "us-east-2", "--admin-email", "a@b.com"]
    rc = pa.main()
    check("exits 0", rc == 0, str(rc))

    be = pa.BACKEND_ENV.read_text()
    fe = pa.FRONTEND_ENV.read_text()
    print("\n  backend .env:\n" + "\n".join("    " + l for l in be.splitlines()))
    print("\n  frontend .env:\n" + "\n".join("    " + l for l in fe.splitlines()))

    for key in pa.__dict__.get("_", []) or [
        "DATABASE_URL", "COGNITO_REGION", "COGNITO_USER_POOL_ID", "AWS_REGION",
        "S3_BUCKET_NAME", "CLOUDFRONT_URL", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    ]:
        check(f"backend .env has {key}", pa.read_env_value(pa.BACKEND_ENV, key) not in (None, ""))
    for key in [
        "VITE_API_BASE_URL", "VITE_COGNITO_USER_POOL_ID", "VITE_COGNITO_USER_POOL_CLIENT_ID",
        "VITE_COGNITO_DOMAIN", "VITE_COGNITO_REDIRECT_SIGN_IN", "VITE_COGNITO_REDIRECT_SIGN_OUT",
    ]:
        check(f"frontend .env has {key}", pa.read_env_value(pa.FRONTEND_ENV, key) not in (None, ""))

    check("bucket name derived from account id",
          pa.read_env_value(pa.BACKEND_ENV, "S3_BUCKET_NAME") == "biofarm-images-123456789012",
          pa.read_env_value(pa.BACKEND_ENV, "S3_BUCKET_NAME"))
    check("cloudfront url has scheme",
          pa.read_env_value(pa.BACKEND_ENV, "CLOUDFRONT_URL") == "https://d123fake.cloudfront.net",
          pa.read_env_value(pa.BACKEND_ENV, "CLOUDFRONT_URL"))
    check("cognito domain has no scheme",
          not (pa.read_env_value(pa.FRONTEND_ENV, "VITE_COGNITO_DOMAIN") or "").startswith("http"),
          pa.read_env_value(pa.FRONTEND_ENV, "VITE_COGNITO_DOMAIN"))
    check("secret key written", pa.read_env_value(pa.BACKEND_ENV, "AWS_SECRET_ACCESS_KEY") == "s3cr3t-fake")
    check("state file written", pa.STATE_FILE.exists())

    import json
    state = json.loads(pa.STATE_FILE.read_text())
    check("state does NOT contain the secret", "s3cr3t-fake" not in pa.STATE_FILE.read_text(), str(state))
    check("state records distribution", state.get("distribution_id") == "E1FAKE")

    names = [f"{c[0]}.{c[1]}" for c in boto3.CALLS]
    for expected in ["s3.create_bucket", "s3.put_bucket_cors", "s3.put_bucket_policy",
                     "cloudfront.create_origin_access_control", "cloudfront.create_distribution",
                     "iam.create_user", "iam.put_user_policy", "iam.create_access_key",
                     "cognito-idp.create_user_pool", "cognito-idp.create_user_pool_client",
                     "cognito-idp.create_user_pool_domain", "cognito-idp.create_group",
                     "cognito-idp.admin_create_user", "cognito-idp.admin_add_user_to_group"]:
        check(f"called {expected}", expected in names)

    pol = next(c[2] for c in boto3.CALLS if c[1] == "put_user_policy")
    check("IAM policy scoped to products/ prefix", "products/*" in pol["PolicyDocument"], pol["PolicyDocument"])
    bp = next(c[2] for c in boto3.CALLS if c[1] == "put_bucket_policy")
    check("bucket policy scoped to distribution ARN", "distribution/E1FAKE" in bp["Policy"], bp["Policy"])
    cl = next(c[2] for c in boto3.CALLS if c[1] == "create_user_pool_client")
    check("app client has no secret", cl["GenerateSecret"] is False)
    check("app client enables OAuth flows", cl["AllowedOAuthFlowsUserPoolClient"] is True)
    check("callback url is the vite dev origin",
          cl["CallbackURLs"] == ["http://localhost:5174/auth/callback"], str(cl["CallbackURLs"]))

print("\n--- re-run is idempotent for .env (no duplicate keys) ---")
with tempfile.TemporaryDirectory() as d:
    root = Path(d)
    (root / "Biofarm_Backend").mkdir()
    (root / "Biofarm_Frontend").mkdir()
    pa.ROOT = root
    pa.BACKEND_ENV = root / "Biofarm_Backend" / ".env"
    pa.FRONTEND_ENV = root / "Biofarm_Frontend" / ".env"
    pa.STATE_FILE = root / ".aws-provision-state.json"
    sys.argv = ["provision_aws.py", "--region", "us-east-2"]
    pa.main()
    pa.main()
    be = pa.BACKEND_ENV.read_text()
    check("S3_BUCKET_NAME appears once", be.count("S3_BUCKET_NAME=") == 1, be)
    check("DATABASE_URL appears once", be.count("DATABASE_URL=") == 1, be)

print()
if failures:
    print(f"{len(failures)} FAILURES: " + "; ".join(failures))
    raise SystemExit(1)
print("All checks passed.")
