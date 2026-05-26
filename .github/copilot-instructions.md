# Copilot Instructions

## Repository Overview

Collection of Python utilities for Azure DevOps repository management:

- **`clone_repos.py`** — bulk-clones all repos from an Azure DevOps org into `<out>/<project>/<repo>`
- **`clone_github_repos.py`** — bulk-clones all repos from a GitHub org into `<out>/<repo>`
- **`create_repo.py`** — creates a new Git repository in an Azure DevOps project

## Running the Scripts

```bash
# clone_repos.py — bulk clone all Azure DevOps repos in an org
python clone_repos.py --org myorg --pat $AZDO_PAT
python clone_repos.py --org myorg --pat $AZDO_PAT --out dc --update --verbose
python clone_repos.py --org myorg --pat $AZDO_PAT --projects-include ProjectA ProjectB
python clone_repos.py --org myorg --pat $AZDO_PAT --bare   # avoids Windows path failures

# clone_github_repos.py — bulk clone all GitHub org repos
python clone_github_repos.py --org myorg --token $GITHUB_TOKEN
python clone_github_repos.py --org myorg --token $GITHUB_TOKEN --out gh --update --verbose
python clone_github_repos.py --org myorg --token $GITHUB_TOKEN --repos-include repoA repoB
python clone_github_repos.py --org myorg --token $GITHUB_TOKEN --skip-archived --skip-forks

# create_repo.py — create a new repo in an Azure DevOps project
python create_repo.py --org myorg --project MyProject --name my-new-repo
python create_repo.py --org myorg --project MyProject --name my-new-repo --default-branch develop
```

- Azure DevOps PAT: `--pat` or `AZDO_PAT` env var
- GitHub token: `--token` or `GITHUB_TOKEN` env var

## Dependencies

Only external dependency is `requests`. Install with:
```bash
pip install requests
```

## Architecture

- **API pagination**: `paged_get()` is a generator that handles Azure DevOps REST API continuation tokens (`x-ms-continuationtoken` header), yielding items page-by-page with a 50ms delay between pages.
- **Auth**: PAT is encoded as HTTP Basic auth for the REST API (`Authorization: Basic base64(:<PAT>)`). For git operations, a temporary `GIT_ASKPASS` helper script is written to a temp file — it returns `"ado"` for username prompts and the PAT for password prompts. The helper is deleted in a `finally` block.
- **Windows path safety**: If a normal clone fails with a checkout/invalid-path error (common on Windows with long or reserved filenames), the script automatically retries as a bare clone.
- **Update mode** (`--update`): runs `git fetch --all --prune`; for non-bare repos also runs `git pull --ff-only`. Bare repos only fetch.

## Key Conventions

- Azure DevOps REST API versions: projects `7.1-preview.4`, repos `7.1-preview.1`.
- Project/repo names are sanitized via `sanitize_name()` which replaces `\/:*?"<>|` with `_`.
- `run_git()` always captures stdout/stderr and raises `RuntimeError` on non-zero exit — never silently ignores git failures.
- The PAT is only passed into child process environments (`os.environ.copy()` + `env["AZDO_PAT"] = pat`), never written to disk or logged.
