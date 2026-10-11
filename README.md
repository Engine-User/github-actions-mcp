# github-actions — MCP server for one/many GitHub repo(s)

Exposes the GitHub surface (repo, files, issues, PRs, Actions runs, releases,
webhooks, search, …) as 66 annotated MCP tools for a single configured repository.

![MCP server architecture flow](architecture-flow.svg)

## 1. Configuration

Copy the template and fill it in:

```bash
cp .env.example .env
```

```text
ACCESS_TOKEN=github_pat_xxx
GITHUB_REPO=owner/repo
```

Install and verify:

```bash
uv sync --dev
.venv\Scripts\python.exe verify_mcp.py     # real stdio handshake: tool list, annotations, whoami
.venv\Scripts\python.exe -m pytest          # unit tests, no network
```

## 2. Register with pi (or any MCP host)

Add to `C:\Users\archi\.config\mcp\mcp.json` (top-level key is `mcpServers`):

```json
{
  "mcpServers": {
    "github-actions": {
      "command": "C:\\Users\\archi\\Desktop\\Testing\\Code\\MCP\\Github_Actions\\.venv\\Scripts\\python.exe",
      "args": [
        "C:\\Users\\archi\\Desktop\\Testing\\Code\\MCP\\Github_Actions\\main.py"
      ], }
    }
}
```

Then run **`/reload`** in pi (config is read at startup). Confirm with `mcp({})`:
`github-actions (66 tools)`.

## 3. Use

In pi, the tools appear as `github-actions_<tool>` — or call them through the gateway:

```js
mcp({ server: "github-actions" })                          // list every tool
mcp({ tool: "github-actions_list_workflow_runs", args: { per_page: 5 } })
mcp({ search: "open a pull request" })                      // semantic/lexical tool search
```

Every tool takes an optional `repo` argument; omit it to use `GITHUB_REPO`.
Start with `github-actions_whoami` when a call fails — it reports config, token, and reachability.

```bash
# direct, without pi
.venv\Scripts\python.exe -c "import main; print(main.repo_info())"
```

## Tools by area (66)

Read-only tools are safe to call at will. Some tools need explicit `confirm=true`.

| Area | Tools |
| --- | --- |
| Repo | `repo_info` `repo_settings` `edit_repo` `repo_branches` `repo_tags` `repo_languages` `repo_topics` `create_branch` `create_tag` `repo_events` `archive_repo` |
| Files | `list_directory` `read_file` `create_or_update_file` `delete_file` `file_history` |
| Commits | `list_commits` `commit_detail` `compare_commits` |
| Issues | `create_issue` `list_issues` `get_issue` `update_issue` `comment_on_issue` `list_issue_comments` `lock_issue` |
| Labels/milestones | `list_labels` `create_label` `list_milestones` `create_milestone` |
| Pull requests | `create_pull_request` `list_pull_requests` `get_pull_request` `update_pull_request` `merge_pull_request` `create_pull_review` `request_reviewers` |
| Releases | `list_releases` `create_release` `upload_release_asset` |
| Actions | `list_workflows` `get_workflow` `dispatch_workflow` `list_workflow_runs` `get_workflow_run` `rerun_workflow` `cancel_workflow_run` `run_logs` |
| CI/CD | `list_check_runs` `combined_status` `create_check_run` `list_deployments` |
| Access/infra | `list_collaborators` `add_collaborator` `remove_collaborator` `list_webhooks` `create_webhook` `delete_webhook` `list_secrets` |
| Search/user | `search_code` `search_issues` `search_users` `current_user` `list_gists` `create_gist` |
| Health | `whoami` |

## Behaviour contract

**Annotations.** Every tool declares `readOnlyHint`, `destructiveHint`,
`idempotentHint` and `openWorldHint`. A host can therefore pick a read-only tool
without reading the doc. `idempotentHint` is true only where a repeat call lands
in the same state — closing an issue, editing a PR, cancelling a run.

**Output.** Tools return a JSON object. Success always carries `"success": true`
alongside the payload, plus a `count` wherever a list is returned.

**Errors.** Anticipated failures raise `ToolError`, which the SDK returns as
`isError: true` with the reason in the content, e.g.

```text
get_issue: GitHub API returned 404 | {'message': 'Not Found', ...}
```

Every tool body is wrapped so no `GithubException` escapes as an unhandled crash
(which would hide the reason behind a generic "Error executing tool X"). Bad
arguments are rejected by the schema before the body runs.

**Confirmation.** The 28 mutating tools raise `ToolError` immediately unless
`confirm=true` is passed, and never reach GitHub before that. `whoami` is the one
mutating-shaped tool that reads state, so it needs no confirmation.

**Secrets.** Secret values are never readable through the API. Repository
*variable* values **are** readable, so `list_secrets` returns names only and
never the value.

## Token permissions

Fine-grained PAT: **Contents** (read/write), **Issues**, **Pull requests**, **Actions**,
**Metadata** (always required), plus **Checks** for `list_check_runs`/`combined_status`
and **Administration** for webhooks/settings. A missing scope comes back as a
`ToolError` naming the tool and the HTTP status (for example `403`), never as a
silent empty result.

## Development

```bash
.venv\Scripts\python.exe -m pytest          # unit tests, mocked, no network
.venv\Scripts\python.exe verify_mcp.py      # live stdio handshake against the real token
```

`tests/` covers the registered tool surface (schema bounds, required arguments,
annotation completeness, confirm gates), the helpers, and the error contract.
`verify_mcp.py` is the end-to-end check: it spawns the server over stdio, lists
the tools, verifies annotations, probes `whoami`, and asserts the confirm gate
fires. Both must pass before a change ships.

## License

MIT — see [LICENSE](LICENSE).
