#!/usr/bin/env python3
import argparse
import base64
import os
import sys
from typing import Dict, Optional
import requests

API_VERSION_REPOS = "7.1-preview.1"
API_VERSION_PROJECTS = "7.1-preview.4"


def make_auth_headers(pat: str) -> Dict[str, str]:
    tok = base64.b64encode(f":{pat}".encode()).decode()
    return {"Authorization": f"Basic {tok}", "Content-Type": "application/json"}


def get_project_id(session: requests.Session, org: str, project: str) -> str:
    url = f"https://dev.azure.com/{org}/_apis/projects/{project}"
    resp = session.get(url, params={"api-version": API_VERSION_PROJECTS})
    if resp.status_code == 401:
        raise SystemExit("Unauthorized (401). Check your PAT and org.")
    if resp.status_code == 404:
        raise SystemExit(f"Project '{project}' not found in org '{org}'.")
    resp.raise_for_status()
    return resp.json()["id"]


def create_repo(
    session: requests.Session,
    org: str,
    project_id: str,
    name: str,
    default_branch: Optional[str],
) -> Dict:
    url = f"https://dev.azure.com/{org}/_apis/git/repositories"
    body: Dict = {"name": name, "project": {"id": project_id}}
    if default_branch:
        body["defaultBranch"] = (
            default_branch
            if default_branch.startswith("refs/")
            else f"refs/heads/{default_branch}"
        )
    resp = session.post(url, json=body, params={"api-version": API_VERSION_REPOS})
    if resp.status_code == 401:
        raise SystemExit("Unauthorized (401). Check your PAT and org.")
    if resp.status_code == 409:
        raise SystemExit(f"A repository named '{name}' already exists in this project.")
    resp.raise_for_status()
    return resp.json()


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Create a new Git repository in an Azure DevOps project."
    )
    ap.add_argument("--org", required=True, help="Azure DevOps organization (e.g., myorg)")
    ap.add_argument("--project", required=True, help="Project name or ID")
    ap.add_argument("--name", required=True, help="Name for the new repository")
    ap.add_argument(
        "--default-branch",
        default="main",
        help="Default branch name (default: main). Can omit 'refs/heads/' prefix.",
    )
    ap.add_argument("--pat", help="Azure DevOps PAT (or set AZDO_PAT env var)")
    args = ap.parse_args()

    pat = args.pat or os.getenv("AZDO_PAT")
    if not pat:
        print("ERROR: Provide a PAT via --pat or AZDO_PAT env var.", file=sys.stderr)
        sys.exit(1)

    with requests.Session() as session:
        session.headers.update(make_auth_headers(pat))

        print(f"Looking up project '{args.project}'...")
        project_id = get_project_id(session, args.org, args.project)

        print(f"Creating repository '{args.name}'...")
        repo = create_repo(session, args.org, project_id, args.name, args.default_branch)

    print(f"\nRepository created successfully!")
    print(f"  Name:          {repo['name']}")
    print(f"  ID:            {repo['id']}")
    print(f"  Default branch: {repo.get('defaultBranch', 'N/A')}")
    print(f"  Clone URL:     {repo['remoteUrl']}")
    print(f"  Web URL:       {repo['webUrl']}")


if __name__ == "__main__":
    main()
