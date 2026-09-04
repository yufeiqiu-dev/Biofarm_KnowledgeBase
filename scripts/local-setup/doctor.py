#!/usr/bin/env python3
"""Report how ready this machine is to run Biofarm locally.

Read-only: it inspects and reports, it never installs or starts anything. The
point is to turn "it doesn't work" into a specific next command, so each failed
check carries the fix rather than just a red X.

    python .claude/skills/local-setup/scripts/doctor.py
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
from pathlib import Path


def find_workspace_root(start: Path) -> Path:
    """Find the directory holding both app repos.

    Located by searching upward rather than by a fixed depth, because this script
    is used both from a checkout of the knowledge-base repo and from a skill
    installed under a project's .claude/skills/ - different depths, same answer.
    Set BIOFARM_ROOT to override when the repos live somewhere unusual.
    """
    override = os.environ.get("BIOFARM_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    here = start.resolve()
    for candidate in (here, *here.parents):
        if (candidate / "Biofarm_Backend").is_dir() and (candidate / "Biofarm_Frontend").is_dir():
            return candidate
    return here


ROOT = find_workspace_root(Path(__file__).parent)
BACKEND = ROOT / "Biofarm_Backend"
FRONTEND = ROOT / "Biofarm_Frontend"


def find_compose() -> Path:
    """Locate docker-compose.yml, which sits beside this script or one level up."""
    here = Path(__file__).resolve().parent
    for candidate in (here / "docker-compose.yml", here.parent / "assets" / "docker-compose.yml"):
        if candidate.exists():
            return candidate
    return here / "docker-compose.yml"


COMPOSE = find_compose()

OK, WARN, FAIL = "PASS", "WARN", "FAIL"


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str, str]] = []

    def add(self, status: str, name: str, detail: str, fix: str = "") -> None:
        self.rows.append((status, name, detail, fix))

    @property
    def failed(self) -> int:
        return sum(1 for s, *_ in self.rows if s == FAIL)

    def render(self) -> str:
        width = max(len(n) for _, n, _, _ in self.rows) + 2
        lines = []
        for status, name, detail, fix in self.rows:
            lines.append(f"  [{status}] {name.ljust(width)} {detail}")
            if fix and status != OK:
                lines.append(f"         -> {fix}")
        return "\n".join(lines)


def run(cmd: list[str], timeout: int = 20) -> tuple[int, str]:
    """Run a command, returning (returncode, combined output). Never raises."""
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, check=False
        )
        return p.returncode, (p.stdout + p.stderr).strip()
    except FileNotFoundError:
        return 127, "not found"
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:  # e.g. WinError 193 on a bad shim
        return 126, str(e)


def which(name: str) -> str | None:
    return shutil.which(name)


def port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex(("127.0.0.1", port)) != 0


def parse_major(text: str) -> int | None:
    m = re.search(r"(\d+)\.(\d+)", text)
    return int(m.group(1)) if m else None


def check_python(r: Report) -> None:
    major, minor = sys.version_info[:2]
    detail = f"{major}.{minor} at {sys.executable}"
    if (major, minor) >= (3, 11):
        r.add(OK, "python", detail)
    else:
        r.add(FAIL, "python", detail, "install Python 3.11+ (winget install Python.Python.3.13)")


NODE_FALLBACK_PATHS = [
    r"C:\Program Files\nodejs\node.exe",
    "/usr/local/bin/node",
    "/opt/homebrew/bin/node",
]


def check_node(r: Report) -> None:
    node = which("node")
    if not node:
        # Installed-but-invisible is the common case right after `winget
        # install`, because PATH is not refreshed in an already-open shell.
        # Reporting a flat "not installed" here sends people off to reinstall
        # something they already have.
        installed = next((p for p in NODE_FALLBACK_PATHS if Path(p).exists()), None)
        if installed:
            r.add(
                WARN,
                "node",
                f"installed at {installed} but not on this shell's PATH",
                "open a new terminal - npm scripts spawn `node` and will fail until you do",
            )
        else:
            r.add(
                FAIL,
                "node",
                "not installed",
                "winget install OpenJS.NodeJS.LTS, then open a NEW terminal (PATH is not refreshed in this one)",
            )
        return

    _, out = run([node, "--version"])
    major = parse_major(out)
    if major is not None and major < 20:
        r.add(WARN, "node", f"{out} (project targets 20+)", "winget upgrade OpenJS.NodeJS.LTS")
    else:
        r.add(OK, "node", out)
    if not which("npm"):
        r.add(FAIL, "npm", "not on PATH", "reinstall Node - npm ships with it")


def check_docker(r: Report) -> None:
    if not which("docker"):
        r.add(FAIL, "docker", "not on PATH", "install Docker Desktop")
        return
    code, out = run(["docker", "info", "--format", "{{.ServerVersion}}"], timeout=30)
    if code != 0:
        # The CLI exists but the engine is down - a very common and confusing state,
        # because `docker --version` still succeeds here.
        r.add(FAIL, "docker engine", "CLI present, daemon unreachable", "start Docker Desktop and wait for it to report Running")
        return
    r.add(OK, "docker engine", f"server {out}")
    check_db_container(r)


def check_db_container(r: Report) -> None:
    code, out = run(
        ["docker", "inspect", "--format", "{{.State.Status}}|{{.State.Health.Status}}", "biofarm-postgres"]
    )
    if code != 0:
        r.add(
            WARN,
            "postgres container",
            "not created",
            f"docker compose -f {COMPOSE} up -d",
        )
        return
    state, _, health = out.partition("|")
    if state == "running" and health in ("healthy", "<no value>", ""):
        r.add(OK, "postgres container", f"running ({health or 'no healthcheck'})")
    elif state == "running":
        r.add(WARN, "postgres container", f"running but health={health}", "give it a few seconds, then re-run")
    else:
        r.add(FAIL, "postgres container", f"state={state}", f"docker compose -f {COMPOSE} up -d")


def check_repos(r: Report) -> None:
    for path, label in ((BACKEND, "backend repo"), (FRONTEND, "frontend repo")):
        if path.is_dir():
            r.add(OK, label, str(path))
        else:
            r.add(FAIL, label, f"missing at {path}", "clone both repos side by side under the workspace root")


def check_deps(r: Report) -> None:
    venv_py = BACKEND / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    if venv_py.exists():
        code, out = run([str(venv_py), "-c", "import fastapi, boto3, stripe; print('ok')"])
        if code == 0:
            r.add(OK, "backend deps", "venv present, imports resolve")
        else:
            r.add(FAIL, "backend deps", "venv present but imports fail", f"{venv_py} -m pip install -r requirements.txt")
    else:
        r.add(
            FAIL,
            "backend venv",
            "missing",
            "cd Biofarm_Backend && python -m venv .venv && .venv/Scripts/python.exe -m pip install -r requirements.txt",
        )

    if (FRONTEND / "node_modules").is_dir():
        r.add(OK, "frontend deps", "node_modules present")
    else:
        r.add(FAIL, "frontend deps", "node_modules missing", "cd Biofarm_Frontend && npm install")


# Required by app/core/config.py - Settings has no defaults for these, so a
# missing one is an import-time crash rather than a degraded feature.
BACKEND_REQUIRED = [
    "DATABASE_URL",
    "COGNITO_REGION",
    "COGNITO_USER_POOL_ID",
    "AWS_REGION",
    "S3_BUCKET_NAME",
    "CLOUDFRONT_URL",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
]
FRONTEND_REQUIRED = [
    "VITE_API_BASE_URL",
    "VITE_COGNITO_USER_POOL_ID",
    "VITE_COGNITO_USER_POOL_CLIENT_ID",
    "VITE_COGNITO_DOMAIN",
    "VITE_COGNITO_REDIRECT_SIGN_IN",
    "VITE_COGNITO_REDIRECT_SIGN_OUT",
]


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        values[k.strip()] = v.strip()
    return values


def check_env_files(r: Report) -> None:
    for path, required, label in (
        (BACKEND / ".env", BACKEND_REQUIRED, "backend .env"),
        (FRONTEND / ".env", FRONTEND_REQUIRED, "frontend .env"),
    ):
        if not path.exists():
            r.add(FAIL, label, "missing", "run scripts/provision_aws.py, or copy .env.example and fill it in")
            continue
        values = read_env(path)
        missing = [k for k in required if not values.get(k)]
        if missing:
            r.add(FAIL, label, "missing/empty: " + ", ".join(missing), "see references/env-vars.md for where each value comes from")
        else:
            r.add(OK, label, f"{len(values)} keys, all required present")

    be, fe = read_env(BACKEND / ".env"), read_env(FRONTEND / ".env")
    if be and fe:
        b = be.get("STRIPE_BYPASS", "").lower()
        f = fe.get("VITE_STRIPE_BYPASS", "").lower()
        if b and f and b != f:
            r.add(
                FAIL,
                "stripe bypass",
                f"backend={b} frontend={f} - mismatched",
                "set both to the same value; they select different checkout code paths",
            )
        elif b:
            r.add(OK, "stripe bypass", f"both {b}")


AWS_CLI_FALLBACK_PATHS = [
    r"C:\Program Files\Amazon\AWSCLIV2\aws.exe",
    "/usr/local/bin/aws",
    "/opt/homebrew/bin/aws",
]

AWS_IDENTITY_SNIPPET = (
    "import json,sys\n"
    "try:\n"
    "    import boto3\n"
    "    i = boto3.Session().client('sts').get_caller_identity()\n"
    "    print(json.dumps({'ok': True, 'account': i['Account'], 'arn': i['Arn']}))\n"
    "except Exception as e:\n"
    "    print(json.dumps({'ok': False, 'error': f'{type(e).__name__}: {e}'}))\n"
)


def check_aws_credentials(r: Report) -> None:
    """Report which AWS account the current credentials resolve to.

    Only needed for provisioning, not for running an already-configured app, so
    a miss is a WARN. It is worth surfacing because credentials from a vending
    tool are short-lived and silently point at whichever account was last
    selected - provisioning into the wrong account is easy and annoying to undo.
    """
    aws_exe = which("aws")
    if aws_exe:
        _, ver = run([aws_exe, "--version"])
        r.add(OK, "aws cli", ver.split()[0] if ver else "present")
    else:
        # A just-installed CLI is on the machine PATH but not in an already-open
        # shell, which reads as "not installed" and sends people round in circles.
        installed = next(
            (p for p in AWS_CLI_FALLBACK_PATHS if Path(p).exists()), None
        )
        if installed:
            r.add(
                WARN,
                "aws cli",
                f"installed at {installed} but not on this shell's PATH",
                "open a new terminal; nothing here requires the CLI either way",
            )

    # boto3 and the AWS CLI share one credential chain, so either interpreter
    # gives the same answer. Prefer the venv, which is guaranteed to have boto3.
    venv_py = BACKEND / (".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python")
    interpreter = str(venv_py) if venv_py.exists() else sys.executable

    code, out = run([interpreter, "-c", AWS_IDENTITY_SNIPPET], timeout=25)
    if code != 0 or not out:
        r.add(WARN, "aws credentials", "could not check (boto3 unavailable)", "only needed for provisioning")
        return
    try:
        data = json.loads(out.splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        r.add(WARN, "aws credentials", "could not parse identity check")
        return

    if data.get("ok"):
        r.add(OK, "aws credentials", f"account {data['account']} as {data['arn'].rsplit('/', 1)[-1]}")
        r.add(WARN, "aws account check", f"provisioning would target {data['account']} - confirm that is the right one")
    else:
        r.add(
            WARN,
            "aws credentials",
            data.get("error", "unresolved"),
            "refresh or set credentials before provisioning; not needed to run an already-configured app",
        )


def check_ports(r: Report) -> None:
    for port, who in ((8000, "backend"), (5174, "frontend")):
        if port_is_free(port):
            r.add(OK, f"port {port}", f"free (for {who})")
        else:
            # Occupied is only a problem if it isn't our own server.
            r.add(WARN, f"port {port}", f"in use - fine if that's your {who}, otherwise free it")


def check_running(r: Report) -> None:
    try:
        import urllib.request

        with urllib.request.urlopen("http://127.0.0.1:8000/api/v1/health", timeout=2) as resp:
            body = json.loads(resp.read().decode())
        r.add(OK, "backend health", f"200 {body}")
    except Exception:
        r.add(WARN, "backend health", "not responding", "start it: cd Biofarm_Backend && .venv/Scripts/python.exe -m uvicorn app.main:app --reload")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--json", action="store_true", help="emit machine-readable results")
    args = ap.parse_args()

    r = Report()
    check_python(r)
    check_node(r)
    check_docker(r)
    check_repos(r)
    check_deps(r)
    check_env_files(r)
    check_aws_credentials(r)
    check_ports(r)
    check_running(r)

    if args.json:
        print(json.dumps([{"status": s, "check": n, "detail": d, "fix": f} for s, n, d, f in r.rows], indent=2))
    else:
        print(f"\nBiofarm local setup doctor - workspace {ROOT}\n")
        print(r.render())
        print()
        if r.failed:
            print(f"{r.failed} blocking issue(s). Fix the FAIL lines above, then re-run.\n")
        else:
            print("No blocking issues. See SKILL.md Phase 5 to start the servers.\n")

    return 1 if r.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
