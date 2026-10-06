"""
Single-file MCP server exposing the full GitHub surface for one repo.
"""

from __future__ import annotations

import base64
import datetime
import itertools
import json
import os
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from github import Auth, Github, GithubException
from github.GithubObject import NotSet
from github.InputFileContent import InputFileContent
from mcp.server.mcpserver import MCPServer

load_dotenv(Path(__file__).parent / ".env")

mcp = MCPServer("github-actions")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _json(obj: Any) -> str:
    return json.dumps(obj, indent=2, default=str)


def _client() -> Github:
    token = os.getenv("ACCESS_TOKEN")
    if not token:
        raise ValueError("ACCESS_TOKEN missing from .env")
    return Github(auth=Auth.Token(token))


def _repo(repo: str | None = None):
    name = repo or os.getenv("GITHUB_REPO")
    if not name:
        raise ValueError("repo arg missing and GITHUB_REPO not set in .env")
    return _client().get_repo(name)


def _limit(items: Iterable[Any], n: int) -> Iterator[Any]:
    """PyGithub's PaginatedList is not sliceable, so cap it with islice."""
    return itertools.islice(iter(items), max(n, 0))


def _opts(**kwargs: Any) -> dict[str, Any]:
    """PyGithub marks unset args with a NotSet sentinel, so drop every None."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _file_block(content: str | None, encoding: str = "utf-8") -> bytes:
    """Build the bytes PyGithub wants for a file create/update."""
    if encoding == "base64":
        return base64.b64decode(content or "")
    return (content or "").encode(encoding)


def _link(obj: Any, attr: str = "html_url") -> str | None:
    return getattr(obj, attr, None)


def _err(exc: Exception) -> str:
    if isinstance(exc, GithubException):
        return _json({"error": str(exc), "status": getattr(exc, "status", None), "data": getattr(exc, "data", None)})
    return f"{type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# repo / metadata
# --------------------------------------------------------------------------- #
@mcp.tool()
def repo_info(repo: str | None = None) -> str:
    """Metadata for the repo: description, visibility, stars, default branch, counts, permissions."""
    r = _repo(repo)
    return _json(
        {
            "full_name": r.full_name,
            "description": r.description,
            "private": r.private,
            "default_branch": r.default_branch,
            "language": r.language,
            "topics": r.get_topics(),
            "stars": r.stargazers_count,
            "forks": r.forks_count,
            "open_issues": r.open_issues_count,
            "size_kb": r.size,
            "license": r.license.spdx_id if r.license else None,
            "permissions": r.permissions,
            "created_at": r.created_at,
            "pushed_at": r.pushed_at,
        }
    )


@mcp.tool()
def repo_branches(repo: str | None = None, protected_only: bool = False) -> str:
    """List branches (name, sha, protected) in the repo."""
    r = _repo(repo)
    out = []
    for b in r.get_branches():
        if protected_only and not b.protected:
            continue
        out.append({"name": b.name, "sha": b.commit.sha, "protected": b.protected})
    return _json(out)


@mcp.tool()
def repo_tags(repo: str | None = None) -> str:
    """List git tags with their commit sha."""
    return _json([{"name": t.name, "sha": t.commit.sha} for t in _repo(repo).get_tags()])


@mcp.tool()
def repo_languages(repo: str | None = None) -> str:
    """Byte counts per language in the repo."""
    return _json(_repo(repo).get_languages())


@mcp.tool()
def repo_topics(repo: str | None = None) -> str:
    """List repo topics/tags."""
    return _json(_repo(repo).get_topics())


# --------------------------------------------------------------------------- #
# files / contents
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_directory(repo: str | None = None, path: str = "", ref: str | None = None) -> str:
    """List files and folders at a path (use path='' for the root)."""
    entries = _repo(repo).get_contents(path, **_opts(ref=ref))
    if not isinstance(entries, list):
        entries = [entries]
    return _json(
        [
            {"path": e.path, "type": e.type, "size": e.size, "sha": e.sha}
            for e in entries
        ]
    )


@mcp.tool()
def read_file(
    repo: str | None = None, path: str = "", ref: str | None = None, max_bytes: int = 200_000
) -> str:
    """Read a file's contents as text (truncated to max_bytes)."""
    f = _repo(repo).get_contents(path, **_opts(ref=ref))
    if isinstance(f, list):
        raise TypeError(f"'{path}' is a directory; use list_directory")
    raw = f.decoded_content
    text = raw[:max_bytes].decode("utf-8", errors="replace")
    truncated = len(raw) > max_bytes
    return text + ("\n... [truncated]" if truncated else "")


@mcp.tool()
def create_or_update_file(
    repo: str | None = None,
    path: str = "",
    content: str = "",
    message: str = "",
    branch: str | None = None,
    sha: str | None = None,
    encoding: str = "utf-8",
) -> str:
    """Create or overwrite a file. Pass sha (from read_file/list_directory) to update; omit to create."""
    r = _repo(repo)
    data = _file_block(content, encoding)
    try:
        res = (
            r.create_file(path, message, data, **_opts(branch=branch))
            if sha is None
            else r.update_file(path, message, data, sha, **_opts(branch=branch))
        )
    except GithubException as e:
        return _err(e)
    return _json(
        {"path": _link(res["content"], "path"), "sha": _link(res["content"], "sha"), "commit": _link(res["commit"])}
    )


@mcp.tool()
def delete_file(repo: str | None = None, path: str = "", message: str = "", sha: str = "", branch: str | None = None) -> str:
    """Delete a file. sha must be the file's blob sha."""
    try:
        res = _repo(repo).delete_file(path, message, sha, **_opts(branch=branch))
    except GithubException as e:
        return _err(e)
    return _json({"deleted": path, "commit": _link(res["commit"])})


@mcp.tool()
def file_history(repo: str | None = None, path: str = "") -> str:
    """Commit history touching one file path."""
    commits = _repo(repo).get_commits(path=path)
    return _json(
        [
            {"sha": c.sha[:8], "date": c.commit.author.date, "author": c.commit.author.name, "message": c.commit.message.splitlines()[0]}
            for c in commits
        ]
    )


# --------------------------------------------------------------------------- #
# commits / branches / refs
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_commits(repo: str | None = None, sha: str | None = None, author: str | None = None, per_page: int = 30) -> str:
    """List commits on a branch/sha, newest first, with stats."""
    r = _repo(repo)
    commits = r.get_commits(**_opts(sha=sha, author=author))
    return _json(
        [
            {
                "sha": c.sha[:8],
                "author": c.commit.author.name,
                "date": c.commit.author.date,
                "message": c.commit.message.splitlines()[0],
                "url": c.html_url,
            }
            for c in _limit(commits, per_page)
        ]
    )


@mcp.tool()
def commit_detail(repo: str | None = None, sha: str = "") -> str:
    """Full detail of one commit: message, stats, and per-file changes."""
    c = _repo(repo).get_commit(sha)
    return _json(
        {
            "sha": c.sha,
            "message": c.commit.message,
            "author": c.commit.author.name,
            "date": c.commit.author.date,
            "stats": c.stats,
            "files": [
                {"filename": f.filename, "status": f.status, "additions": f.additions, "deletions": f.deletions, "patch": (f.patch or "")[:4000]}
                for f in c.files
            ],
        }
    )


@mcp.tool()
def compare_commits(repo: str | None = None, base: str = "", head: str = "") -> str:
    """Diff two refs: ahead/behind counts, commits, changed files."""
    c = _repo(repo).compare(base, head)
    return _json(
        {
            "status": c.status,
            "ahead_by": c.ahead_by,
            "behind_by": c.behind_by,
            "total_commits": c.total_commits,
            "commits": [{"sha": x.sha[:8], "message": x.commit.message.splitlines()[0]} for x in c.commits],
            "files": [{"filename": f.filename, "status": f.status, "additions": f.additions, "deletions": f.deletions} for f in c.files],
        }
    )


@mcp.tool()
def create_branch(repo: str | None = None, name: str = "", from_sha: str | None = None) -> str:
    """Create a branch. from_sha defaults to the repo default branch."""
    r = _repo(repo)
    sha = from_sha or r.get_branch(r.default_branch).commit.sha
    ref = r.create_git_ref(ref=f"refs/heads/{name}", sha=sha)
    return _json({"branch": name, "sha": ref.object.sha})


@mcp.tool()
def create_tag(repo: str | None = None, name: str = "", target_sha: str | None = None, message: str | None = None) -> str:
    """Create a tag. Supply message for an annotated tag, omit it for a lightweight tag."""
    r = _repo(repo)
    sha = target_sha or r.get_branch(r.default_branch).commit.sha
    try:
        if message:
            t = r.create_git_tag(name, message, sha, "commit")
            return _json({"tag": name, "sha": _link(t.object, "sha"), "annotated": True})
        ref = r.create_git_ref(ref=f"refs/tags/{name}", sha=sha)
    except GithubException as e:
        return _err(e)
    return _json({"tag": name, "sha": ref.object.sha, "annotated": False})


# --------------------------------------------------------------------------- #
# issues
# --------------------------------------------------------------------------- #
@mcp.tool()
def create_issue(
    repo: str | None = None,
    issue_title: str = "",
    issue_text: str = "",
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
    milestone: str | None = None,
) -> str:
    """Create an issue; return its URL and number."""
    try:
        i = _repo(repo).create_issue(
            title=issue_title,
            body=issue_text,
            labels=labels or [],
            assignees=assignees or [],
        )
    except GithubException as e:
        return _err(e)
    return _json({"number": i.number, "url": i.html_url, "state": i.state})


@mcp.tool()
def list_issues(
    repo: str | None = None,
    state: str = "open",
    labels: list[str] | None = None,
    sort: str = "created",
    direction: str = "desc",
    per_page: int = 30,
) -> str:
    """List issues (state: open/closed/all), optionally filtered by label."""
    r = _repo(repo)
    issues = r.get_issues(state=state, labels=labels or [], sort=sort, direction=direction)
    out = []
    for i in _limit(issues, per_page):
        out.append(
            {
                "number": i.number,
                "title": i.title,
                "state": i.state,
                "is_pr": bool(i.pull_request) if hasattr(i, "pull_request") else False,
                "labels": [lb.name for lb in i.labels],
                "assignees": [a.login for a in i.assignees],
                "created_at": i.created_at,
                "url": i.html_url,
            }
        )
    return _json(out)


@mcp.tool()
def get_issue(repo: str | None = None, number: int = 0) -> str:
    """Full detail plus body of one issue or PR (PRs are issues in GitHub's model)."""
    i = _repo(repo).get_issue(number)
    return _json(
        {
            "number": i.number,
            "title": i.title,
            "state": i.state,
            "body": i.body,
            "labels": [lb.name for lb in i.labels],
            "assignees": [a.login for a in i.assignees],
            "comments": i.comments,
            "created_at": i.created_at,
            "closed_at": i.closed_at,
            "url": i.html_url,
        }
    )


@mcp.tool()
def update_issue(
    repo: str | None = None,
    number: int = 0,
    title: str | None = None,
    body: str | None = None,
    state: str | None = None,
    labels: list[str] | None = None,
    assignees: list[str] | None = None,
) -> str:
    """Edit an issue: change title/body, close (state='closed') or reopen, set labels/assignees."""
    kw: dict[str, Any] = {}
    if title is not None:
        kw["title"] = title
    if body is not None:
        kw["body"] = body
    if state is not None:
        kw["state"] = state
    if labels is not None:
        kw["labels"] = labels
    if assignees is not None:
        kw["assignees"] = assignees
    try:
        r = _repo(repo)
        r.get_issue(number).edit(**kw)  # PyGithub's edit() returns None, so re-read the issue.
        i = r.get_issue(number)
    except GithubException as e:
        return _err(e)
    return _json({"number": i.number, "state": i.state, "labels": [lb.name for lb in i.labels], "url": i.html_url})


@mcp.tool()
def comment_on_issue(repo: str | None = None, number: int = 0, body: str = "") -> str:
    """Add a comment to an issue or pull request."""
    try:
        c = _repo(repo).get_issue(number).create_comment(body)
    except GithubException as e:
        return _err(e)
    return _json({"id": c.id, "url": c.html_url})


@mcp.tool()
def list_issue_comments(repo: str | None = None, number: int = 0) -> str:
    """All comments on an issue or pull request."""
    return _json(
        [{"id": c.id, "user": c.user.login, "created_at": c.created_at, "body": c.body} for c in _repo(repo).get_issue(number).get_comments()]
    )


@mcp.tool()
def lock_issue(repo: str | None = None, number: int = 0, reason: str = "resolved", unlock: bool = False) -> str:
    """Lock an issue's conversation, or unlock it when unlock=true."""
    try:
        i = _repo(repo).get_issue(number)
        i.unlock() if unlock else i.lock(reason)
    except GithubException as e:
        return _err(e)
    return _json({"number": number, "locked": not unlock})


# --------------------------------------------------------------------------- #
# labels / milestones
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_labels(repo: str | None = None) -> str:
    """List repo labels."""
    return _json([{"name": lb.name, "color": lb.color, "description": lb.description} for lb in _repo(repo).get_labels()])


@mcp.tool()
def create_label(repo: str | None = None, name: str = "", color: str = "ededed", description: str = "") -> str:
    """Create a label."""
    try:
        lb = _repo(repo).create_label(name=name, color=color, description=description)
    except GithubException as e:
        return _err(e)
    return _json({"name": lb.name, "color": lb.color, "url": lb.url})


@mcp.tool()
def list_milestones(repo: str | None = None, state: str = "open") -> str:
    """List milestones."""
    return _json([{"number": m.number, "title": m.title, "state": m.state, "open_issues": m.open_issues, "due_on": m.due_on} for m in _repo(repo).get_milestones(state=state)])


@mcp.tool()
def create_milestone(repo: str | None = None, title: str = "", description: str = "", due_on: str | None = None) -> str:
    """Create a milestone. due_on is an ISO date, e.g. 2026-01-31."""
    try:
        m = _repo(repo).create_milestone(
            title=title, description=description, due_on=datetime.date.fromisoformat(due_on) if due_on else NotSet
        )
    except (GithubException, ValueError) as e:
        return _err(e)
    return _json({"number": m.number, "title": m.title, "url": m.html_url})


# --------------------------------------------------------------------------- #
# pull requests
# --------------------------------------------------------------------------- #
@mcp.tool()
def create_pull_request(repo: str | None = None, title: str = "", body: str = "", head: str = "", base: str | None = None, draft: bool = False) -> str:
    """Open a pull request from head branch to base branch."""
    r = _repo(repo)
    try:
        p = r.create_pull(base=base or r.default_branch, head=head, title=title, body=body, draft=draft)
    except GithubException as e:
        return _err(e)
    return _json({"number": p.number, "url": p.html_url, "state": p.state})


@mcp.tool()
def list_pull_requests(repo: str | None = None, state: str = "open", base: str | None = None, head: str | None = None) -> str:
    """List pull requests, optionally filtered by base/head branch."""
    kw: dict[str, Any] = {"state": state}
    if base:
        kw["base"] = base
    if head:
        kw["head"] = head
    return _json(
        [
            {"number": p.number, "title": p.title, "state": p.state, "head": p.head.ref, "base": p.base.ref, "draft": p.draft, "url": p.html_url}
            for p in _repo(repo).get_pulls(**kw)
        ]
    )


@mcp.tool()
def get_pull_request(repo: str | None = None, number: int = 0) -> str:
    """Pull request detail: state, mergeability, head/base sha, changed files, checks summary."""
    p = _repo(repo).get_pull(number)
    return _json(
        {
            "number": p.number,
            "title": p.title,
            "body": p.body,
            "state": p.state,
            "draft": p.draft,
            "merged": p.merged,
            "mergeable": p.mergeable,
            "mergeable_state": p.mergeable_state,
            "head": {"ref": p.head.ref, "sha": p.head.sha},
            "base": {"ref": p.base.ref, "sha": p.base.sha},
            "additions": p.additions,
            "deletions": p.deletions,
            "changed_files": p.changed_files,
            "labels": [lb.name for lb in p.labels],
            "url": p.html_url,
        }
    )


@mcp.tool()
def update_pull_request(repo: str | None = None, number: int = 0, title: str | None = None, body: str | None = None, state: str | None = None, base: str | None = None) -> str:
    """Edit a PR, or close it with state='closed'."""
    kw: dict[str, Any] = {}
    if title is not None:
        kw["title"] = title
    if body is not None:
        kw["body"] = body
    if state is not None:
        kw["state"] = state
    if base is not None:
        kw["base"] = base
    try:
        r = _repo(repo)
        r.get_pull(number).edit(**kw)  # edit() returns None, so re-read the PR.
        p = r.get_pull(number)
    except GithubException as e:
        return _err(e)
    return _json({"number": p.number, "state": p.state, "url": p.html_url})


@mcp.tool()
def merge_pull_request(repo: str | None = None, number: int = 0, commit_title: str | None = None, commit_message: str | None = None, merge_method: str = "merge") -> str:
    """Merge a PR. merge_method: merge | squash | rebase."""
    try:
        p = _repo(repo).get_pull(number)
        merged = p.merge(
            **_opts(commit_title=commit_title, commit_message=commit_message), merge_method=merge_method
        )
    except GithubException as e:
        return _err(e)
    return _json({"number": number, "merged": bool(merged), "sha": merged.sha, "message": merged.message})


@mcp.tool()
def create_pull_review(repo: str | None = None, number: int = 0, body: str = "", event: str = "COMMENT") -> str:
    """Review a PR. event: APPROVE | REQUEST_CHANGES | COMMENT."""
    try:
        r = _repo(repo).get_pull(number).create_review(body=body, event=event)
    except GithubException as e:
        return _err(e)
    return _json({"review_id": r.id, "state": r.state, "url": r.html_url})


@mcp.tool()
def request_reviewers(repo: str | None = None, number: int = 0, reviewers: list[str] | None = None, team_reviewers: list[str] | None = None) -> str:
    """Ask users or teams to review a PR."""
    try:
        p = _repo(repo).get_pull(number)
        p.create_review_request(reviewers=reviewers or [], team_reviewers=team_reviewers or [])
    except GithubException as e:
        return _err(e)
    return _json({"number": number, "reviewers": reviewers or [], "team_reviewers": team_reviewers or []})


# --------------------------------------------------------------------------- #
# releases
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_releases(repo: str | None = None) -> str:
    """List releases (tag, name, draft/pre-release flags)."""
    return _json([{"tag": r.tag_name, "name": r.name, "draft": r.draft, "prerelease": r.prerelease, "published_at": r.published_at, "url": r.html_url} for r in _repo(repo).get_releases()])


@mcp.tool()
def create_release(repo: str | None = None, tag: str = "", name: str = "", message: str = "", draft: bool = False, prerelease: bool = False, target_commitish: str | None = None) -> str:
    """Publish a release for a tag."""
    try:
        r = _repo(repo).create_git_release(
            tag=tag, name=name or tag, message=message, draft=draft, prerelease=prerelease, **_opts(target_commitish=target_commitish)
        )
    except GithubException as e:
        return _err(e)
    return _json({"tag": r.tag_name, "url": r.html_url, "draft": r.draft})


@mcp.tool()
def upload_release_asset(repo: str | None = None, tag: str = "", path: str = "", label: str | None = None) -> str:
    """Attach a local file to an existing release."""
    try:
        a = _repo(repo).get_release(tag).upload_asset(path, label=label or Path(path).name)
    except GithubException as e:
        return _err(e)
    return _json({"name": a.name, "size": a.size, "url": a.browser_download_url})


# --------------------------------------------------------------------------- #
# Actions: workflows and runs
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_workflows(repo: str | None = None) -> str:
    """List GitHub Actions workflows with id, name, state, and path."""
    return _json([{"id": w.id, "name": w.name, "path": w.path, "state": w.state} for w in _repo(repo).get_workflows()])


@mcp.tool()
def get_workflow(repo: str | None = None, workflow_id: str = "", recent_runs: int = 10) -> str:
    """One workflow plus its most recent runs."""
    r = _repo(repo)
    w = r.get_workflow(workflow_id)
    runs = w.get_runs()
    return _json(
        {
            "id": w.id,
            "name": w.name,
            "state": w.state,
            "path": w.path,
            "recent_runs": [
                {"id": r.id, "status": r.status, "conclusion": r.conclusion, "event": r.event, "branch": r.head_branch, "created_at": r.created_at, "url": r.html_url}
                for r in _limit(runs, recent_runs)
            ],
        }
    )


@mcp.tool()
def dispatch_workflow(repo: str | None = None, workflow_id: str = "", ref: str = "main", inputs: dict[str, str] | None = None) -> str:
    """Trigger a workflow_dispatch workflow on a ref, passing inputs."""
    try:
        ok = _repo(repo).get_workflow(workflow_id).create_dispatch(ref=ref, inputs=inputs or {})
    except GithubException as e:
        return _err(e)
    return _json({"dispatched": bool(ok), "workflow_id": workflow_id, "ref": ref, "inputs": inputs or {}})


@mcp.tool()
def list_workflow_runs(
    repo: str | None = None,
    workflow_id: str | None = None,
    status: str | None = None,
    branch: str | None = None,
    event: str | None = None,
    per_page: int = 20,
) -> str:
    """List workflow runs, optionally filtered by workflow, status, branch, or event."""
    r = _repo(repo)
    filters = _opts(status=status, branch=branch, event=event)
    runs = r.get_workflow(workflow_id).get_runs(**filters) if workflow_id else r.get_workflow_runs(**filters)
    return _json(
        [
            {
                "run_id": r.id,
                "run_number": r.run_number,
                "workflow": r.name,
                "status": r.status,
                "conclusion": r.conclusion,
                "event": r.event,
                "branch": r.head_branch,
                "commit": r.head_sha[:8] if r.head_sha else None,
                "actor": r.actor.login if r.actor else None,
                "created_at": r.created_at,
                "url": r.html_url,
            }
            for r in _limit(runs, per_page)
        ]
    )


@mcp.tool()
def get_workflow_run(repo: str | None = None, run_id: int = 0) -> str:
    """One run with timings, conclusion, and its jobs."""
    run = _repo(repo).get_workflow_run(run_id)
    return _json(
        {
            "run_id": run.id,
            "run_number": run.run_number,
            "workflow": run.name,
            "status": run.status,
            "conclusion": run.conclusion,
            "event": run.event,
            "branch": run.head_branch,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "url": run.html_url,
            "jobs": [
                {"id": j.id, "name": j.name, "status": j.status, "conclusion": j.conclusion, "started_at": j.started_at, "completed_at": j.completed_at}
                for j in run.jobs()
            ],
        }
    )


@mcp.tool()
def rerun_workflow(repo: str | None = None, run_id: int = 0, failed_only: bool = False) -> str:
    """Re-run a workflow run (all jobs, or only failed ones)."""
    try:
        run = _repo(repo).get_workflow_run(run_id)
        run.rerun_failed_jobs() if failed_only else run.rerun()
    except GithubException as e:
        return _err(e)
    return _json({"run_id": run_id, "rerun": True, "failed_only": failed_only})


@mcp.tool()
def cancel_workflow_run(repo: str | None = None, run_id: int = 0) -> str:
    """Cancel an in-progress workflow run."""
    try:
        _repo(repo).get_workflow_run(run_id).cancel()
    except GithubException as e:
        return _err(e)
    return _json({"run_id": run_id, "cancelled": True})


@mcp.tool()
def run_logs(repo: str | None = None, run_id: int = 0) -> str:
    """Log URLs (and failed-step names) for a workflow run."""
    run = _repo(repo).get_workflow_run(run_id)
    return _json({"logs_url": run.logs_url, "html_url": run.html_url, "conclusion": run.conclusion})


# --------------------------------------------------------------------------- #
# checks / statuses / deployments
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_check_runs(repo: str | None = None, sha: str = "", per_page: int = 30) -> str:
    """Check runs attached to a commit (Actions jobs, external apps). Needs checks:read on the token."""
    try:
        return _json(
            [{"name": c.name, "status": c.status, "conclusion": c.conclusion, "app": c.app.name if c.app else None, "url": c.html_url} for c in _limit(_repo(repo).get_commit(sha).get_check_runs(), per_page)]
        )
    except GithubException as e:
        return _err(e)


@mcp.tool()
def combined_status(repo: str | None = None, sha: str = "") -> str:
    """Aggregate commit status (pending/failure/success counts, latest statuses)."""
    try:
        s = _repo(repo).get_commit(sha).get_combined_status()
        return _json({"state": s.state, "total_count": s.total_count, "statuses": [{"context": x.context, "state": x.state, "description": x.description} for x in s.statuses]})
    except GithubException as e:
        return _err(e)


@mcp.tool()
def create_check_run(repo: str | None = None, name: str = "", head_sha: str = "", status: str = "completed", conclusion: str = "success", title: str = "", summary: str = "", details_url: str | None = None) -> str:
    """Create a check run on a commit (CI reporting)."""
    try:
        c = _repo(repo).create_check_run(
            name=name,
            head_sha=head_sha,
            status=status,
            conclusion=conclusion,
            output={"title": title, "summary": summary},
            **_opts(details_url=details_url),
        )
    except GithubException as e:
        return _err(e)
    return _json({"id": c.id, "name": c.name, "conclusion": c.conclusion, "url": c.html_url})


@mcp.tool()
def list_deployments(repo: str | None = None, environment: str | None = None, per_page: int = 20) -> str:
    """List deployments with ref, environment, and status."""
    r = _repo(repo)
    kw = _opts(environment=environment)
    deps = r.get_deployments(**kw)
    return _json([{"id": d.id, "ref": d.ref, "environment": d.environment, "sha": d.sha[:8], "created_at": d.created_at, "url": d.url} for d in _limit(deps, per_page)])


# --------------------------------------------------------------------------- #
# repo settings / collaborators / webhooks / secrets
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_collaborators(repo: str | None = None, permission: str | None = None) -> str:
    """List collaborators and their permissions."""
    kw: dict[str, Any] = {}
    if permission:
        kw["permission"] = permission
    return _json([{"login": c.login, "permissions": c.permissions} for c in _repo(repo).get_collaborators(**kw)])


@mcp.tool()
def add_collaborator(repo: str | None = None, username: str = "", permission: str = "push") -> str:
    """Invite a collaborator. permission: pull | triage | push | maintain | admin."""
    try:
        _repo(repo).add_to_collaborators(username, permission=permission)
    except GithubException as e:
        return _err(e)
    return _json({"added": username, "permission": permission})


@mcp.tool()
def remove_collaborator(repo: str | None = None, username: str = "") -> str:
    """Remove a collaborator."""
    try:
        _repo(repo).remove_from_collaborators(username)
    except GithubException as e:
        return _err(e)
    return _json({"removed": username})


@mcp.tool()
def repo_settings(repo: str | None = None) -> str:
    """Config knobs worth knowing: has_issues, has_projects, has_wiki, has_discussions, merge options, visibility."""
    r = _repo(repo)
    return _json(
        {
            "has_issues": r.has_issues,
            "has_projects": r.has_projects,
            "has_wiki": r.has_wiki,
            "has_discussions": r.has_discussions,
            "allow_merge_commit": r.allow_merge_commit,
            "allow_squash_merge": r.allow_squash_merge,
            "allow_rebase_merge": r.allow_rebase_merge,
            "delete_branch_on_merge": r.delete_branch_on_merge,
            "archived": r.archived,
        }
    )


@mcp.tool()
def edit_repo(repo: str | None = None, description: str | None = None, homepage: str | None = None, private: bool | None = None, default_branch: str | None = None, topics: list[str] | None = None, archive: bool | None = None) -> str:
    """Edit repo settings/topics. Empty-string clears a field; omit to leave it unchanged."""
    kw: dict[str, Any] = {}
    if description is not None:
        kw["description"] = description
    if homepage is not None:
        kw["homepage"] = homepage
    if private is not None:
        kw["private"] = private
    if default_branch is not None:
        kw["default_branch"] = default_branch
    if topics is not None:
        kw["topics"] = topics
    if archive is not None:
        kw["archived"] = archive
    try:
        _repo(repo).edit(**kw)
    except GithubException as e:
        return _err(e)
    return _json({"edited": repo or os.getenv("GITHUB_REPO"), "fields": list(kw)})


@mcp.tool()
def list_webhooks(repo: str | None = None) -> str:
    """List webhooks (url, events, active). Secret values are never returned by GitHub."""
    return _json([{"id": h.id, "url": h.config.get("url"), "events": h.events, "active": h.active} for h in _repo(repo).get_hooks()])


@mcp.tool()
def create_webhook(repo: str | None = None, name: str = "web", url: str = "", events: list[str] | None = None, secret: str | None = None, active: bool = True) -> str:
    """Create a webhook. name is a display label; events defaults to ['push']."""
    cfg: dict[str, str] = {"url": url, "content_type": "json"}
    if secret:
        cfg["secret"] = secret
    try:
        h = _repo(repo).create_hook(name=name, config=cfg, events=events or ["push"], active=active)
    except GithubException as e:
        return _err(e)
    return _json({"id": h.id, "name": h.name, "url": h.config.get("url"), "events": h.events, "active": h.active})


@mcp.tool()
def delete_webhook(repo: str | None = None, hook_id: int = 0) -> str:
    """Delete a webhook by id."""
    try:
        _repo(repo).get_hook(hook_id).delete()
    except GithubException as e:
        return _err(e)
    return _json({"deleted_hook": hook_id})


@mcp.tool()
def list_secrets(repo: str | None = None) -> str:
    """Names of Actions secrets and repo variables (values are never readable)."""
    r = _repo(repo)
    return _json(
        {
            "secrets": [{"name": s.name, "created_at": s.created_at} for s in r.get_secrets()],
            "variables": [{"name": v.name, "value": v.value} for v in r.get_variables()],
        }
    )


# --------------------------------------------------------------------------- #
# search / user / gists / misc
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_code(query: str = "", repo: str | None = None, per_page: int = 20) -> str:
    """Search code inside one repo. Defaults to GITHUB_REPO; pass repo='' to search all of GitHub."""
    try:
        target = repo if repo is not None else os.getenv("GITHUB_REPO", "")
        q = f"{query} repo:{target}" if target and target not in query else query
        return _json([{"repo": i.repository.full_name, "path": i.path, "url": i.html_url} for i in _limit(_client().search_code(q), per_page)])
    except GithubException as e:
        return _err(e)


@mcp.tool()
def search_issues(query: str = "", repo: str | None = None, state: str = "open", kind: str = "issue", per_page: int = 20) -> str:
    """Search issues (kind='issue') or PRs (kind='pull_request'), scoped to GITHUB_REPO by default."""
    target = repo if repo is not None else os.getenv("GITHUB_REPO", "")
    terms = [query] if query else []
    if target:
        terms.append(f"repo:{target}")
    if state != "all":
        terms.append(f"is:{'open' if state == 'open' else 'closed'}")
    scoped = "is:" in query
    if not scoped:
        terms.append("is:pull-request" if kind == "pull_request" else "is:issue")
    try:
        return _json([{"number": i.number, "title": i.title, "state": i.state, "url": i.html_url} for i in _limit(_client().search_issues(" ".join(terms)), per_page)])
    except GithubException as e:
        return _err(e)


@mcp.tool()
def search_users(query: str = "", per_page: int = 20) -> str:
    """Search users by login, name, email, or location."""
    try:
        return _json([{"login": u.login, "name": u.name, "type": u.type} for u in _limit(_client().search_users(query), per_page)])
    except GithubException as e:
        return _err(e)


@mcp.tool()
def current_user() -> str:
    """Who the token belongs to: login, name, rate-limit headroom."""
    g = _client()
    u = g.get_user()
    core = g.get_rate_limit().resources.core
    return _json({"login": u.login, "name": u.name, "rate_remaining": core.remaining, "rate_limit": core.limit, "reset": core.reset})


@mcp.tool()
def list_gists(username: str | None = None) -> str:
    """List a user's gists (id, description, files)."""
    g = _client()
    u = g.get_user(login=username) if username else g.get_user()
    return _json([{"id": x.id, "description": x.description, "url": x.html_url, "files": list(x.files)} for x in u.get_gists()])


@mcp.tool()
def create_gist(description: str = "", filename: str = "file.txt", content: str = "", public: bool = False) -> str:
    """Create a gist from a filename and its content."""
    g = _client().get_user().create_gist(
        public=public, files={filename: InputFileContent(content)}, description=description
    )
    return _json({"id": g.id, "url": g.html_url})


@mcp.tool()
def repo_events(repo: str | None = None, per_page: int = 30) -> str:
    """Recent repository events (pushes, issues, PRs, reviews)."""
    return _json([{"type": e.type, "actor": e.actor.login if e.actor else None, "created_at": e.created_at} for e in _limit(_repo(repo).get_events(), per_page)])


@mcp.tool()
def archive_repo(repo: str | None = None, unarchive: bool = False) -> str:
    """Archive or unarchive the repo (read-only repos cannot be unarchived via API)."""
    try:
        _repo(repo).edit(archived=not unarchive)
    except GithubException as e:
        return _err(e)
    return _json({"repo": repo or os.getenv("GITHUB_REPO"), "archived": not unarchive})


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
@mcp.tool()
def whoami(repo: str | None = None) -> str:
    """Health check: config present, token valid, repo reachable. Use this first when a call errors."""
    token, repo_name = os.getenv("ACCESS_TOKEN"), repo or os.getenv("GITHUB_REPO")
    if not token or not repo_name:
        return _json({"ok": False, "error": "ACCESS_TOKEN and GITHUB_REPO must both be set in .env"})
    try:
        r = _client().get_repo(repo_name)
    except GithubException as e:
        return _json({"ok": False, "repo": repo_name, **json.loads(_err(e))})
    return _json({"ok": True, "repo": r.full_name, "default_branch": r.default_branch, "private": r.private})


if __name__ == "__main__":
    mcp.run(transport="stdio")