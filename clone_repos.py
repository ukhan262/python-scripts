#!/usr/bin/env python3
import argparse
import base64
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Dict, Generator, List, Optional
import subprocess
import requests

API_VERSION_PROJECTS = "7.1-preview.4"
API_VERSION_REPOS = "7.1-preview.1"

def sanitize_name(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip()

def make_auth_headers(pat: str) -> Dict[str, str]:
    tok = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {tok}"}

def paged_get(session: requests.Session, url: str, params: Dict[str, str]) -> Generator[Dict, None, None]:
    next_params = dict(params)
    while True:
        resp = session.get(url, params=next_params)
        if resp.status_code == 401:
            raise SystemExit("Unauthorized (401). Check your PAT and org.")
        resp.raise_for_status()
        data = resp.json()
        for item in data.get("value", []):
            yield item
        token = resp.headers.get("x-ms-continuationtoken")
        if not token:
            break
        next_params["continuationToken"] = token
        time.sleep(0.05)

def list_projects(session: requests.Session, org: str) -> List[Dict]:
    url = f"https://dev.azure.com/{org}/_apis/projects"
    params = {"api-version": API_VERSION_PROJECTS, "stateFilter": "WellFormed"}
    return list(paged_get(session, url, params))

def list_repos(session: requests.Session, org: str, project_name: str) -> List[Dict]:
    url = f"https://dev.azure.com/{org}/{project_name}/_apis/git/repositories"
    params = {"api-version": API_VERSION_REPOS}
    return list(paged_get(session, url, params))

def run_git(cmd: List[str], env: Dict[str, str], cwd: Optional[Path] = None) -> None:
    r = subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git failed ({' '.join(cmd)}):\nSTDOUT:\n{r.stdout}\nSTDERR:\n{r.stderr}")

def create_askpass_helper(pat: str) -> (str, Dict[str, str]):
    """
    Creates a temporary askpass script that supplies a dummy username and the PAT as the password.
    Returns the path to the helper and a minimal env overlay for git.
    """
    # Cross-platform: a tiny Python script works well on macOS/Linux. (On Windows, install Python.)
    helper_code = """#!/usr/bin/env python3
import sys
prompt = sys.argv[1] if len(sys.argv) > 1 else ""
if "Username" in prompt or "username" in prompt:
    print("ado")  # any non-empty username works for Azure DevOps
else:
    import os
    print(os.environ.get("AZDO_PAT",""))
"""
    fd, path = tempfile.mkstemp(prefix="askpass_", suffix=".py", text=True)
    with os.fdopen(fd, "w") as f:
        f.write(helper_code)
    os.chmod(path, 0o700)

    env = os.environ.copy()
    env["GIT_ASKPASS"] = path
    env["GIT_ASKPASS_REQUIRE"] = "force"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["AZDO_PAT"] = pat  # only in child env
    return path, env

def main():
    ap = argparse.ArgumentParser(description="Clone all Azure DevOps repos into dc/<project>/<repo> using git.")
    ap.add_argument("--org", required=True, help="Azure DevOps organization (e.g., myorg)")
    ap.add_argument("--pat", help="Azure DevOps Personal Access Token (or set AZDO_PAT env var)")
    ap.add_argument("--out", default="dc", help="Output root folder (default: dc)")
    ap.add_argument("--update", action="store_true", help="If repo folder exists, run 'git fetch --all --prune' and 'git pull --ff-only'")
    ap.add_argument("--projects-include", nargs="*", help="Optional: only include these project names")
    ap.add_argument("--projects-exclude", nargs="*", help="Optional: exclude these project names")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    pat = args.pat or os.getenv("AZDO_PAT")
    if not pat:
        print("ERROR: Provide a PAT via --pat or AZDO_PAT env var.", file=sys.stderr)
        sys.exit(1)

    out_root = Path(args.out)
    out_root.mkdir(parents=True, exist_ok=True)

    askpass_path, git_env = create_askpass_helper(pat)

    try:
        with requests.Session() as session:
            session.headers.update(make_auth_headers(pat))

            if args.verbose:
                print(f"Listing projects in org: {args.org}")

            projects = list_projects(session, args.org)
            if not projects:
                print("No projects found.")
                return

            include = set(args.projects_include or [])
            exclude = set(args.projects_exclude or [])

            for p in projects:
                proj_name_api = p["name"]
                if include and proj_name_api not in include:
                    continue
                if proj_name_api in exclude:
                    continue

                proj_name = sanitize_name(proj_name_api)
                proj_dir = out_root / proj_name
                proj_dir.mkdir(parents=True, exist_ok=True)
                if args.verbose:
                    print(f"\nProject: {proj_name}")

                repos = list_repos(session, args.org, proj_name_api)
                if not repos and args.verbose:
                    print("  (No repos)")

                for r in repos:
                    repo_name = sanitize_name(r["name"])
                    clone_url = r.get("remoteUrl")  # e.g., https://dev.azure.com/{org}/{project}/_git/{repo}
                    target_dir = proj_dir / repo_name

                    if args.verbose:
                        print(f"  Repo: {repo_name}")
                        print(f"    URL: {clone_url}")

                    if target_dir.exists():
                        if (target_dir / ".git").exists():
                            if args.update:
                                if args.verbose:
                                    print("    Exists -> updating (fetch + pull)...")
                                # safer updates
                                run_git(["git", "fetch", "--all", "--prune"], git_env, cwd=target_dir)
                                # Pull default branch only; if unknown, 'git pull' uses current branch.
                                run_git(["git", "pull", "--ff-only"], git_env, cwd=target_dir)
                            else:
                                if args.verbose:
                                    print("    Exists -> skip (use --update to pull).")
                            continue
                        else:
                            # dir exists but isn't a git repo — place inside
                            target_dir = target_dir / repo_name

                    # clone fresh
                    if args.verbose:
                        print("    Cloning...")
                    run_git(["git", "clone", "--origin", "origin", clone_url, str(target_dir)], git_env)
                    if args.verbose:
                        print(f"    -> {target_dir} ✓")

    finally:
        # Clean up the askpass helper
        try:
            os.remove(askpass_path)
        except Exception:
            pass

    print(f"\nAll done. Output at: {out_root.resolve()}")

if __name__ == "__main__":
    main()
