#!/usr/bin/env python3
import argparse
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Generator, List, Optional
import subprocess
import requests

GITHUB_API = "https://api.github.com"


def sanitize_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip()


def make_auth_headers(token: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def paged_get(session: requests.Session, url: str, params: Dict[str, str]) -> Generator[Dict, None, None]:
    next_url: Optional[str] = url
    next_params: Optional[Dict] = dict(params)
    while next_url:
        resp = session.get(next_url, params=next_params)
        if resp.status_code == 401:
            raise SystemExit("Unauthorized (401). Check your token and org.")
        if resp.status_code == 404:
            raise SystemExit(f"Not found (404). Check your org name and token scopes.")
        resp.raise_for_status()
        for item in resp.json():
            yield item
        # GitHub paginates via the Link header
        next_url = None
        next_params = None
        link = resp.headers.get("Link", "")
        for part in link.split(","):
            part = part.strip()
            if 'rel="next"' in part:
                next_url = part.split(";")[0].strip().strip("<>")
        time.sleep(0.05)


def list_repos(session: requests.Session, account: str) -> List[Dict]:
    """Try the org endpoint first; fall back to the user endpoint for personal accounts."""
    org_url = f"{GITHUB_API}/orgs/{account}/repos"
    params = {"per_page": "100", "type": "all"}

    # Probe the org endpoint without consuming the generator
    probe = session.get(org_url, params={**params, "per_page": "1"})
    if probe.status_code == 404:
        # Personal account — use the user repos endpoint
        user_url = f"{GITHUB_API}/users/{account}/repos"
        probe2 = session.get(user_url, params={**params, "per_page": "1"})
        if probe2.status_code == 404:
            raise SystemExit(f"Account '{account}' not found. Check the name and token scopes.")
        return list(paged_get(session, user_url, params))
    if probe.status_code == 401:
        raise SystemExit("Unauthorized (401). Check your token.")
    probe.raise_for_status()
    return list(paged_get(session, org_url, params))


def run_git(cmd: List[str], env: Dict[str, str], cwd: Optional[Path] = None) -> None:
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git failed ({' '.join(cmd)}):\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")


def is_git_repo(path: Path, env: Dict[str, str]) -> bool:
    if not path.exists():
        return False
    r = subprocess.run(["git", "rev-parse", "--is-bare-repository"], cwd=str(path), env=env, capture_output=True, text=True)
    return r.returncode == 0


def create_askpass_helper(token: str) -> tuple:
    """
    Creates a temporary askpass script that supplies 'x-access-token' as the
    username and the GitHub token as the password (standard for HTTPS git auth).
    Returns the script path and a git env overlay.
    """
    helper_code = """#!/usr/bin/env python3
import sys
prompt = sys.argv[1] if len(sys.argv) > 1 else ""
if "Username" in prompt or "username" in prompt:
    print("x-access-token")
else:
    import os
    print(os.environ.get("GITHUB_TOKEN", ""))
"""
    fd, path = tempfile.mkstemp(prefix="askpass_", suffix=".py", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(helper_code)
    os.chmod(path, 0o700)

    env = os.environ.copy()
    env["GIT_ASKPASS"] = path
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GITHUB_TOKEN"] = token  # only in child env
    return path, env


def main() -> None:
    ap = argparse.ArgumentParser(description="Clone all GitHub org repos into <out>/<repo> using git.")
    ap.add_argument("--org", required=True, help="GitHub organization or personal account username")
    ap.add_argument("--token", help="GitHub Personal Access Token (or set GITHUB_TOKEN env var)")
    ap.add_argument("--out", default="gh", help="Output root folder (default: gh)")
    ap.add_argument("--update", action="store_true", help="If repo folder exists, run 'git fetch --all --prune' and 'git pull --ff-only'")
    ap.add_argument("--bare", action="store_true", help="Clone repositories as bare repos to avoid Windows invalid-path checkout failures")
    ap.add_argument("--repos-include", nargs="*", help="Optional: only include these repo names")
    ap.add_argument("--repos-exclude", nargs="*", help="Optional: exclude these repo names")
    ap.add_argument("--skip-archived", action="store_true", help="Skip archived repositories")
    ap.add_argument("--skip-forks", action="store_true", help="Skip forked repositories")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    token = args.token or os.getenv("GITHUB_TOKEN")
    if not token:
        print("ERROR: Provide a token via --token or GITHUB_TOKEN env var.", file=sys.stderr)
        sys.exit(1)

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    askpass_path, git_env = create_askpass_helper(token)

    try:
        with requests.Session() as session:
            session.headers.update(make_auth_headers(token))

            if args.verbose:
                print(f"Listing repos for: {args.org}")

            repos = list_repos(session, args.org)
            if not repos:
                print("No repositories found.")
                return

            include = set(args.repos_include or [])
            exclude = set(args.repos_exclude or [])

            for r in repos:
                repo_name_api = r["name"]

                if include and repo_name_api not in include:
                    continue
                if repo_name_api in exclude:
                    continue
                if args.skip_archived and r.get("archived"):
                    if args.verbose:
                        print(f"  Skipping archived: {repo_name_api}")
                    continue
                if args.skip_forks and r.get("fork"):
                    if args.verbose:
                        print(f"  Skipping fork: {repo_name_api}")
                    continue

                repo_name = sanitize_name(repo_name_api)
                clone_url = r["clone_url"]  # e.g., https://github.com/{org}/{repo}.git
                target_dir = out_root / repo_name

                if args.verbose:
                    print(f"\nRepo: {repo_name}")
                    print(f"  URL: {clone_url}")

                if target_dir.exists():
                    if is_git_repo(target_dir, git_env):
                        if args.update:
                            if args.verbose:
                                print("  Exists -> updating (fetch + prune)...")
                            run_git(["git", "fetch", "--all", "--prune"], git_env, cwd=target_dir)
                            if not args.bare:
                                run_git(["git", "pull", "--ff-only"], git_env, cwd=target_dir)
                            else:
                                if args.verbose:
                                    print("  Bare repo -> fetch only (no working tree to pull).")
                        else:
                            if args.verbose:
                                print("  Exists -> skip (use --update to pull).")
                        continue
                    else:
                        # dir exists but isn't a git repo — nest inside
                        target_dir = target_dir / repo_name

                if args.verbose:
                    print("  Cloning...")
                clone_cmd = ["git", "clone", "--origin", "origin"]
                if args.bare:
                    clone_cmd.append("--bare")
                clone_cmd.extend([clone_url, str(target_dir)])

                try:
                    run_git(clone_cmd, git_env)
                except RuntimeError as exc:
                    message = str(exc).lower()
                    if not args.bare and ("checkout failed" in message or "invalid path" in message):
                        print("  WARNING: Checkout failed due to invalid Windows paths. Retrying as bare clone.")
                        if target_dir.exists():
                            shutil.rmtree(target_dir)
                        run_git(["git", "clone", "--bare", "--origin", "origin", clone_url, str(target_dir)], git_env)
                    else:
                        raise

                if args.verbose:
                    print(f"  -> {target_dir} ✓")

    finally:
        try:
            os.remove(askpass_path)
        except Exception:
            pass

    print(f"\nAll done. Output at: {out_root.resolve()}")


if __name__ == "__main__":
    main()
