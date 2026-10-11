"""
Single-file MCP server exposing the full GitHub surface for one repo.

Conventions every tool follows:
  * returns a plain dict (the SDK renders it as JSON text for the model)
  * every dict carries ``success: True`` on success
  * anticipated failures raise ``ToolError`` so the model reads the real reason
  * mutating tools declare ``confirm`` and refuse to run without it
  * never returns secret or variable values, only their names
"""

from __future__ import annotations

import base64
import datetime
import functools
import inspect
import itertools
import os
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from github import Auth, Github, GithubException
from github.GithubObject import NotSet
from github.InputFileContent import InputFileContent
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from pydantic.fields import FieldInfo

load_dotenv(Path(__file__).parent / ".env")

mcp = MCPServer(
    "github-actions",
    instructions=(
        "Full GitHub surface for one repository: repo metadata, files, commits, issues, "
        "pull requests, releases, Actions workflows, checks, access control and search. "
        "Every tool takes an optional 'repo' argument; omit it to use GITHUB_REPO. "
        "Mutating tools require confirm=true. Call whoami first when a call fails."
    ),
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _client() -> Github:
    """Authenticated GitHub client."""
    token = os.getenv("ACCESS_TOKEN")
    if not token:
        raise ToolError("ACCESS_TOKEN is not set: put it in the .env file next to main.py")
    return Github(auth=Auth.Token(token))


def _repo(repo: str | None = None) -> Any:
    """Resolve a repository from the argument, else from GITHUB_REPO.

    The isinstance check keeps direct Python calls (``repo_info()``) working:
    without it an unpassed ``repo`` arrives as its pydantic ``FieldInfo``
    default instead of None.
    """
    name = repo if isinstance(repo, str) and repo else os.getenv("GITHUB_REPO")
    if not name:
        raise ToolError("No repository: pass repo='owner/name' or set GITHUB_REPO in .env")
    return _client().get_repo(name)


def _limit(items: Iterable[Any], n: int) -> Iterator[Any]:
    """PyGithub's PaginatedList is not sliceable, so cap it with islice."""
    return itertools.islice(iter(items), max(n, 0))


def _opts(**kwargs: Any) -> dict[str, Any]:
    """PyGithub marks unset arguments with a NotSet sentinel, so drop every None."""
    return {k: v for k, v in kwargs.items() if v is not None}


def _file_block(content: str | None, encoding: str = "utf-8") -> bytes:
    """Build the bytes PyGithub wants for a file create or update."""
    if encoding == "base64":
        return base64.b64decode(content or "")
    return (content or "").encode(encoding)


def _link(obj: Any, attr: str = "html_url") -> Any:
    """Read an attribute, returning None when the object does not carry it."""
    return getattr(obj, attr, None)


def _gh_err(exc: GithubException, tool: str) -> ToolError:
    """Turn an API failure into a ToolError the model can read and act on."""
    status = getattr(exc, "status", None) or 500
    detail = getattr(exc, "data", None) or str(exc)
    return ToolError(f"{tool}: GitHub API returned {status} | {detail}")


def _confirmed(ok: bool) -> None:
    """Gate every mutating tool behind an explicit confirm=true."""
    if not ok:
        raise ToolError("Refusing to run a mutating tool: pass confirm=true once you are sure")


def _guard(fn: Callable[..., Any]) -> Callable[..., Any]:
    """Make a tool body safe under both call paths.

    Two jobs:

    * an uncaught ``GithubException`` becomes a ``ToolError`` naming the tool.
      Without this it is treated as a crash and the model only sees
      "Error executing tool <name>" with the reason withheld.
    * an unpassed parameter keeps its pydantic ``FieldInfo`` default. The SDK
      always passes validated values, but a direct Python call such as
      ``repo_info()`` does not, so resolve those defaults to real values first.
    """
    parameters = list(inspect.signature(fn).parameters)
    defaults = {
        name: param.default
        for name, param in inspect.signature(fn).parameters.items()
        if isinstance(param.default, FieldInfo)
    }

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        supplied = set(parameters[: len(args)])
        for name, field in defaults.items():
            if name not in kwargs and name not in supplied:
                kwargs[name] = field.get_default(call_default_factory=False)
        try:
            return fn(*args, **kwargs)
        except GithubException as exc:
            raise _gh_err(exc, fn.__name__) from exc

    return wrapper


def _read(title: str) -> Callable[..., Any]:
    """Register a read-only, idempotent tool."""
    return mcp.tool(
        annotations=ToolAnnotations(
            title=title,
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        )
    )


def _write(title: str, idempotent: bool = False) -> Callable[..., Any]:
    """Register a mutating tool; set idempotent when a repeat lands in the same state."""
    return mcp.tool(
        annotations=ToolAnnotations(
            title=title,
            readOnlyHint=False,
            destructiveHint=True,
            idempotentHint=idempotent,
            openWorldHint=True,
        )
    )


# --------------------------------------------------------------------------- #
# repo / metadata (5)
# --------------------------------------------------------------------------- #
@_read("Repo Info")
@_guard
def repo_info(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """Metadata for the repo: description, visibility, stars, default branch, counts, permissions."""
    r = _repo(repo)
    return {
        "success": True,
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
        "created_at": str(r.created_at),
        "pushed_at": str(r.pushed_at),
    }


@_read("Repo Branches")
@_guard
def repo_branches(
    repo: str | None = Field(None, description="Repository as owner/name"),
    protected_only: bool = Field(False, description="Only return protected branches"),
) -> dict[str, Any]:
    """List branches (name, sha, protected) in the repo."""
    branches = [
        {"name": b.name, "sha": b.commit.sha, "protected": b.protected}
        for b in _repo(repo).get_branches()
        if not (protected_only and not b.protected)
    ]
    return {"success": True, "branches": branches, "count": len(branches)}


@_read("Repo Tags")
@_guard
def repo_tags(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """List git tags with their commit sha."""
    tags = [{"name": t.name, "sha": t.commit.sha} for t in _repo(repo).get_tags()]
    return {"success": True, "tags": tags, "count": len(tags)}


@_read("Repo Languages")
@_guard
def repo_languages(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """Byte counts per language in the repo."""
    languages = _repo(repo).get_languages()
    return {"success": True, "languages": languages, "count": len(languages)}


@_read("Repo Topics")
@_guard
def repo_topics(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """List repo topics/tags."""
    topics = _repo(repo).get_topics()
    return {"success": True, "topics": topics, "count": len(topics)}


# --------------------------------------------------------------------------- #
# files / contents (5)
# --------------------------------------------------------------------------- #
@_read("List Directory")
@_guard
def list_directory(
    repo: str | None = Field(None, description="Repository as owner/name"),
    path: str = Field("", description="Path to list; empty means the repo root"),
    ref: str | None = Field(None, description="Branch, tag or sha to read from"),
) -> dict[str, Any]:
    """List files and folders at a path (use path='' for the root)."""
    entries = _repo(repo).get_contents(path, **_opts(ref=ref))
    if not isinstance(entries, list):
        entries = [entries]
    items = [{"path": e.path, "type": e.type, "size": e.size, "sha": e.sha} for e in entries]
    return {"success": True, "entries": items, "count": len(items)}


@_read("Read File")
@_guard
def read_file(
    repo: str | None = Field(None, description="Repository as owner/name"),
    path: str = Field(..., min_length=1, description="File path to read"),
    ref: str | None = Field(None, description="Branch, tag or sha to read from"),
    max_bytes: int = Field(200_000, ge=1, le=1_000_000, description="Maximum bytes to return"),
) -> dict[str, Any]:
    """Read a file's contents as text (truncated to max_bytes)."""
    f = _repo(repo).get_contents(path, **_opts(ref=ref))
    if isinstance(f, list):
        raise ToolError(f"'{path}' is a directory; use list_directory instead")
    raw = f.decoded_content
    truncated = len(raw) > max_bytes
    return {
        "success": True,
        "path": path,
        "content": raw[:max_bytes].decode("utf-8", errors="replace"),
        "size": len(raw),
        "truncated": truncated,
        "sha": f.sha,
    }


@_write("Create or Update File")
@_guard
def create_or_update_file(
    repo: str | None = Field(None, description="Repository as owner/name"),
    path: str = Field(..., min_length=1, description="File path"),
    content: str = Field(..., description="File content"),
    message: str = Field(..., min_length=1, description="Commit message"),
    branch: str | None = Field(None, description="Branch to commit to"),
    sha: str | None = Field(None, description="Blob sha to update; omit to create"),
    encoding: Literal["utf-8", "base64"] = Field("utf-8", description="How content is encoded"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create or overwrite a file. Pass sha (from read_file/list_directory) to update; omit to create."""
    _confirmed(confirm)
    r = _repo(repo)
    data = _file_block(content, encoding)
    res = (
        r.create_file(path, message, data, **_opts(branch=branch))
        if sha is None
        else r.update_file(path, message, data, sha, **_opts(branch=branch))
    )
    return {
        "success": True,
        "operation": "update" if sha is not None else "create",
        "path": _link(res["content"], "path"),
        "sha": _link(res["content"], "sha"),
        "commit": _link(res["commit"]),
    }


@_write("Delete File")
@_guard
def delete_file(
    repo: str | None = Field(None, description="Repository as owner/name"),
    path: str = Field(..., min_length=1, description="File path to delete"),
    message: str = Field(..., min_length=1, description="Commit message"),
    sha: str = Field(..., min_length=1, description="File's blob sha, from read_file"),
    branch: str | None = Field(None, description="Branch to commit to"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Delete a file. sha must be the file's blob sha."""
    _confirmed(confirm)
    res = _repo(repo).delete_file(path, message, sha, **_opts(branch=branch))
    return {"success": True, "deleted": path, "commit": _link(res["commit"])}


@_read("File History")
@_guard
def file_history(
    repo: str | None = Field(None, description="Repository as owner/name"),
    path: str = Field(..., min_length=1, description="File path"),
    per_page: int = Field(30, ge=1, le=100, description="Maximum commits to return"),
) -> dict[str, Any]:
    """Commit history touching one file path."""
    commits = [
        {
            "sha": c.sha[:8],
            "date": str(c.commit.author.date),
            "author": c.commit.author.name,
            "message": c.commit.message.splitlines()[0],
        }
        for c in _limit(_repo(repo).get_commits(path=path), per_page)
    ]
    return {"success": True, "commits": commits, "count": len(commits)}


# --------------------------------------------------------------------------- #
# commits / branches / refs (5)
# --------------------------------------------------------------------------- #
@_read("List Commits")
@_guard
def list_commits(
    repo: str | None = Field(None, description="Repository as owner/name"),
    sha: str | None = Field(None, description="Branch or sha to start from"),
    author: str | None = Field(None, description="Filter by author"),
    per_page: int = Field(30, ge=1, le=100, description="Maximum commits to return"),
) -> dict[str, Any]:
    """List commits on a branch/sha, newest first."""
    commits = _repo(repo).get_commits(**_opts(sha=sha, author=author))
    items = [
        {
            "sha": c.sha[:8],
            "author": c.commit.author.name,
            "date": str(c.commit.author.date),
            "message": c.commit.message.splitlines()[0],
            "url": c.html_url,
        }
        for c in _limit(commits, per_page)
    ]
    return {"success": True, "commits": items, "count": len(items)}


@_read("Commit Detail")
@_guard
def commit_detail(
    repo: str | None = Field(None, description="Repository as owner/name"),
    sha: str = Field(..., min_length=1, description="Commit sha"),
) -> dict[str, Any]:
    """Full detail of one commit: message, stats, and per-file changes."""
    c = _repo(repo).get_commit(sha)
    return {
        "success": True,
        "sha": c.sha,
        "message": c.commit.message,
        "author": c.commit.author.name,
        "date": str(c.commit.author.date),
        "stats": c.stats,
        "files": [
            {
                "filename": f.filename,
                "status": f.status,
                "additions": f.additions,
                "deletions": f.deletions,
                "patch": (f.patch or "")[:4000],
            }
            for f in c.files
        ],
    }


@_read("Compare Commits")
@_guard
def compare_commits(
    repo: str | None = Field(None, description="Repository as owner/name"),
    base: str = Field(..., min_length=1, description="Base ref: branch, tag or sha"),
    head: str = Field(..., min_length=1, description="Head ref: branch, tag or sha"),
) -> dict[str, Any]:
    """Diff two refs: ahead/behind counts, commits, changed files."""
    c = _repo(repo).compare(base, head)
    return {
        "success": True,
        "status": c.status,
        "ahead_by": c.ahead_by,
        "behind_by": c.behind_by,
        "total_commits": c.total_commits,
        "commits": [{"sha": x.sha[:8], "message": x.commit.message.splitlines()[0]} for x in c.commits],
        "files": [
            {"filename": f.filename, "status": f.status, "additions": f.additions, "deletions": f.deletions}
            for f in c.files
        ],
    }


@_write("Create Branch")
@_guard
def create_branch(
    repo: str | None = Field(None, description="Repository as owner/name"),
    name: str = Field(..., min_length=1, max_length=255, description="Branch name"),
    from_sha: str | None = Field(None, description="Sha to branch from; defaults to the default branch"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create a branch. from_sha defaults to the repo default branch."""
    _confirmed(confirm)
    r = _repo(repo)
    sha = from_sha or r.get_branch(r.default_branch).commit.sha
    ref = r.create_git_ref(ref=f"refs/heads/{name}", sha=sha)
    return {"success": True, "branch": name, "sha": ref.object.sha}


@_write("Create Tag")
@_guard
def create_tag(
    repo: str | None = Field(None, description="Repository as owner/name"),
    name: str = Field(..., min_length=1, max_length=255, description="Tag name"),
    target_sha: str | None = Field(None, description="Sha to tag; defaults to the default branch"),
    message: str | None = Field(None, description="Tag message; makes an annotated tag"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create a tag. Supply message for an annotated tag, omit it for a lightweight tag."""
    _confirmed(confirm)
    r = _repo(repo)
    sha = target_sha or r.get_branch(r.default_branch).commit.sha
    if message:
        t = r.create_git_tag(name, message, sha, "commit")
        return {"success": True, "tag": name, "sha": _link(t.object, "sha"), "annotated": True}
    ref = r.create_git_ref(ref=f"refs/tags/{name}", sha=sha)
    return {"success": True, "tag": name, "sha": ref.object.sha, "annotated": False}


# --------------------------------------------------------------------------- #
# issues (7)
# --------------------------------------------------------------------------- #
@_write("Create Issue")
@_guard
def create_issue(
    repo: str | None = Field(None, description="Repository as owner/name"),
    issue_title: str = Field(..., min_length=1, max_length=256, description="Issue title"),
    issue_text: str = Field(..., min_length=1, max_length=65536, description="Issue body, markdown"),
    labels: list[str] | None = Field(None, description="Labels to apply"),
    assignees: list[str] | None = Field(None, description="GitHub usernames to assign"),
    milestone: int | None = Field(None, gt=0, description="Milestone number"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create an issue; return its URL and number."""
    _confirmed(confirm)
    i = _repo(repo).create_issue(
        title=issue_title,
        body=issue_text,
        labels=labels or [],
        assignees=assignees or [],
        **_opts(milestone=milestone),
    )
    return {
        "success": True,
        "number": i.number,
        "url": i.html_url,
        "state": i.state,
        "created_at": str(i.created_at),
    }


@_read("List Issues")
@_guard
def list_issues(
    repo: str | None = Field(None, description="Repository as owner/name"),
    state: Literal["open", "closed", "all"] = Field("open", description="Issue state filter"),
    labels: list[str] | None = Field(None, description="Filter by labels"),
    sort: Literal["created", "updated", "comments"] = Field("created", description="Sort field"),
    direction: Literal["asc", "desc"] = Field("desc", description="Sort direction"),
    per_page: int = Field(30, ge=1, le=100, description="Maximum issues to return"),
) -> dict[str, Any]:
    """List issues (state: open/closed/all), optionally filtered by label."""
    issues = _repo(repo).get_issues(state=state, labels=labels or [], sort=sort, direction=direction)
    items = [
        {
            "number": i.number,
            "title": i.title,
            "state": i.state,
            "is_pr": bool(i.pull_request) if hasattr(i, "pull_request") else False,
            "labels": [lb.name for lb in i.labels],
            "assignees": [a.login for a in i.assignees],
            "created_at": str(i.created_at),
            "url": i.html_url,
        }
        for i in _limit(issues, per_page)
    ]
    return {"success": True, "issues": items, "count": len(items)}


@_read("Get Issue")
@_guard
def get_issue(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Issue number"),
) -> dict[str, Any]:
    """Full detail plus body of one issue or PR (PRs are issues in GitHub's model)."""
    i = _repo(repo).get_issue(number)
    return {
        "success": True,
        "number": i.number,
        "title": i.title,
        "state": i.state,
        "body": i.body or "",
        "labels": [lb.name for lb in i.labels],
        "assignees": [a.login for a in i.assignees],
        "comments": i.comments,
        "created_at": str(i.created_at),
        "closed_at": str(i.closed_at) if i.closed_at else None,
        "url": i.html_url,
    }


@_write("Update Issue", idempotent=True)
@_guard
def update_issue(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Issue number"),
    title: str | None = Field(None, description="New title"),
    body: str | None = Field(None, description="New body"),
    state: Literal["open", "closed"] | None = Field(None, description="New state"),
    labels: list[str] | None = Field(None, description="New labels, replacing the existing set"),
    assignees: list[str] | None = Field(None, description="New assignees, replacing the existing set"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Edit an issue: change title/body, close (state='closed') or reopen, set labels/assignees."""
    _confirmed(confirm)
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
    r = _repo(repo)
    r.get_issue(number).edit(**kw)  # edit() returns None, so re-read the issue.
    i = r.get_issue(number)
    return {
        "success": True,
        "number": i.number,
        "state": i.state,
        "labels": [lb.name for lb in i.labels],
        "url": i.html_url,
    }


@_write("Comment on Issue")
@_guard
def comment_on_issue(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Issue number"),
    body: str = Field(..., min_length=1, max_length=65536, description="Comment body"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Add a comment to an issue or pull request."""
    _confirmed(confirm)
    c = _repo(repo).get_issue(number).create_comment(body)
    return {"success": True, "id": c.id, "url": c.html_url, "created_at": str(c.created_at)}


@_read("List Issue Comments")
@_guard
def list_issue_comments(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Issue number"),
) -> dict[str, Any]:
    """All comments on an issue or pull request."""
    comments = [
        {"id": c.id, "user": c.user.login, "created_at": str(c.created_at), "body": c.body}
        for c in _repo(repo).get_issue(number).get_comments()
    ]
    return {"success": True, "comments": comments, "count": len(comments)}


@_write("Lock Issue", idempotent=True)
@_guard
def lock_issue(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Issue number"),
    reason: Literal["off-topic", "too heated", "resolved", "spam"] = Field("resolved", description="Lock reason"),
    unlock: bool = Field(False, description="Unlock instead of locking"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Lock an issue's conversation, or unlock it when unlock=true."""
    _confirmed(confirm)
    i = _repo(repo).get_issue(number)
    i.unlock() if unlock else i.lock(reason)
    return {"success": True, "number": number, "locked": not unlock}


# --------------------------------------------------------------------------- #
# labels / milestones (4)
# --------------------------------------------------------------------------- #
@_read("List Labels")
@_guard
def list_labels(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """List repo labels."""
    labels = [{"name": lb.name, "color": lb.color, "description": lb.description} for lb in _repo(repo).get_labels()]
    return {"success": True, "labels": labels, "count": len(labels)}


@_write("Create Label", idempotent=True)
@_guard
def create_label(
    repo: str | None = Field(None, description="Repository as owner/name"),
    name: str = Field(..., min_length=1, max_length=100, description="Label name"),
    color: str = Field("ededed", pattern=r"^[0-9a-fA-F]{6}$", description="Hex color, 6 digits, no #"),
    description: str = Field("", max_length=200, description="Label description"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create a label."""
    _confirmed(confirm)
    lb = _repo(repo).create_label(name=name, color=color, description=description)
    return {"success": True, "name": lb.name, "color": lb.color, "url": lb.url}


@_read("List Milestones")
@_guard
def list_milestones(
    repo: str | None = Field(None, description="Repository as owner/name"),
    state: Literal["open", "closed", "all"] = Field("open", description="Milestone state"),
) -> dict[str, Any]:
    """List milestones."""
    milestones = [
        {
            "number": m.number,
            "title": m.title,
            "state": m.state,
            "open_issues": m.open_issues,
            "due_on": str(m.due_on) if m.due_on else None,
        }
        for m in _repo(repo).get_milestones(state=state)
    ]
    return {"success": True, "milestones": milestones, "count": len(milestones)}


@_write("Create Milestone")
@_guard
def create_milestone(
    repo: str | None = Field(None, description="Repository as owner/name"),
    title: str = Field(..., min_length=1, max_length=256, description="Milestone title"),
    description: str = Field("", max_length=1000, description="Milestone description"),
    due_on: str | None = Field(None, description="Due date as YYYY-MM-DD"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create a milestone. due_on is an ISO date, for example 2026-01-31."""
    _confirmed(confirm)
    try:
        due = datetime.date.fromisoformat(due_on) if due_on else NotSet
    except ValueError:
        raise ToolError(f"create_milestone: due_on must be YYYY-MM-DD, got {due_on!r}")
    m = _repo(repo).create_milestone(title=title, description=description, due_on=due)
    return {"success": True, "number": m.number, "title": m.title, "url": m.html_url}


# --------------------------------------------------------------------------- #
# pull requests (7)
# --------------------------------------------------------------------------- #
@_write("Create Pull Request")
@_guard
def create_pull_request(
    repo: str | None = Field(None, description="Repository as owner/name"),
    title: str = Field(..., min_length=1, max_length=256, description="PR title"),
    head: str = Field(..., min_length=1, description="Source branch holding the commits"),
    body: str = Field("", max_length=65536, description="PR body"),
    base: str | None = Field(None, description="Target branch; defaults to the default branch"),
    draft: bool = Field(False, description="Open as a draft"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Open a pull request from the head branch to the base branch."""
    _confirmed(confirm)
    r = _repo(repo)
    p = r.create_pull(base=base or r.default_branch, head=head, title=title, body=body, draft=draft)
    return {"success": True, "number": p.number, "url": p.html_url, "state": p.state}


@_read("List Pull Requests")
@_guard
def list_pull_requests(
    repo: str | None = Field(None, description="Repository as owner/name"),
    state: Literal["open", "closed", "all"] = Field("open", description="PR state filter"),
    base: str | None = Field(None, description="Filter by base branch"),
    head: str | None = Field(None, description="Filter by head branch"),
) -> dict[str, Any]:
    """List pull requests, optionally filtered by base or head branch."""
    kw: dict[str, Any] = {"state": state, **_opts(base=base, head=head)}
    prs = [
        {
            "number": p.number,
            "title": p.title,
            "state": p.state,
            "head": p.head.ref,
            "base": p.base.ref,
            "draft": p.draft,
            "url": p.html_url,
        }
        for p in _repo(repo).get_pulls(**kw)
    ]
    return {"success": True, "pull_requests": prs, "count": len(prs)}


@_read("Get Pull Request")
@_guard
def get_pull_request(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Pull request number"),
) -> dict[str, Any]:
    """Pull request detail: state, mergeability, head/base sha, changed files."""
    p = _repo(repo).get_pull(number)
    return {
        "success": True,
        "number": p.number,
        "title": p.title,
        "body": p.body or "",
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


@_write("Update Pull Request", idempotent=True)
@_guard
def update_pull_request(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Pull request number"),
    title: str | None = Field(None, description="New title"),
    body: str | None = Field(None, description="New body"),
    state: Literal["open", "closed"] | None = Field(None, description="New state"),
    base: str | None = Field(None, description="New base branch"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Edit a PR, or close it with state='closed'."""
    _confirmed(confirm)
    kw: dict[str, Any] = {}
    if title is not None:
        kw["title"] = title
    if body is not None:
        kw["body"] = body
    if state is not None:
        kw["state"] = state
    if base is not None:
        kw["base"] = base
    r = _repo(repo)
    r.get_pull(number).edit(**kw)  # edit() returns None, so re-read the PR.
    p = r.get_pull(number)
    return {"success": True, "number": p.number, "state": p.state, "url": p.html_url}


@_write("Merge Pull Request")
@_guard
def merge_pull_request(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Pull request number"),
    merge_method: Literal["merge", "squash", "rebase"] = Field("merge", description="How to merge"),
    commit_title: str | None = Field(None, description="Title for the merge commit"),
    commit_message: str | None = Field(None, description="Body for the merge commit"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Merge a PR. merge_method: merge | squash | rebase."""
    _confirmed(confirm)
    merged = _repo(repo).get_pull(number).merge(
        **_opts(commit_title=commit_title, commit_message=commit_message), merge_method=merge_method
    )
    return {
        "success": True,
        "number": number,
        "merged": bool(merged),
        "sha": _link(merged, "sha"),
        "message": _link(merged, "message"),
    }


@_write("Create Pull Review")
@_guard
def create_pull_review(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Pull request number"),
    body: str = Field(..., min_length=1, max_length=65536, description="Review body"),
    event: Literal["APPROVE", "REQUEST_CHANGES", "COMMENT"] = Field("COMMENT", description="Review verdict"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Review a PR. event: APPROVE | REQUEST_CHANGES | COMMENT."""
    _confirmed(confirm)
    r = _repo(repo).get_pull(number).create_review(body=body, event=event)
    return {"success": True, "review_id": r.id, "state": r.state, "url": r.html_url}


@_write("Request Reviewers", idempotent=True)
@_guard
def request_reviewers(
    repo: str | None = Field(None, description="Repository as owner/name"),
    number: int = Field(..., gt=0, description="Pull request number"),
    reviewers: list[str] | None = Field(None, description="Usernames to ask"),
    team_reviewers: list[str] | None = Field(None, description="Team slugs to ask"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Ask users or teams to review a PR."""
    _confirmed(confirm)
    _repo(repo).get_pull(number).create_review_request(
        reviewers=reviewers or [], team_reviewers=team_reviewers or []
    )
    return {
        "success": True,
        "number": number,
        "reviewers": reviewers or [],
        "team_reviewers": team_reviewers or [],
    }


# --------------------------------------------------------------------------- #
# releases (3)
# --------------------------------------------------------------------------- #
@_read("List Releases")
@_guard
def list_releases(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """List releases (tag, name, draft/pre-release flags)."""
    releases = [
        {
            "tag": r.tag_name,
            "name": r.name,
            "draft": r.draft,
            "prerelease": r.prerelease,
            "published_at": str(r.published_at) if r.published_at else None,
            "url": r.html_url,
        }
        for r in _repo(repo).get_releases()
    ]
    return {"success": True, "releases": releases, "count": len(releases)}


@_write("Create Release")
@_guard
def create_release(
    repo: str | None = Field(None, description="Repository as owner/name"),
    tag: str = Field(..., min_length=1, max_length=255, description="Existing tag name"),
    name: str = Field("", max_length=255, description="Release name; defaults to the tag"),
    message: str = Field("", max_length=65536, description="Release notes"),
    draft: bool = Field(False, description="Create as a draft"),
    prerelease: bool = Field(False, description="Mark as a pre-release"),
    target_commitish: str | None = Field(None, description="Branch or sha to target"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Publish a release for a tag."""
    _confirmed(confirm)
    r = _repo(repo).create_git_release(
        tag=tag,
        name=name or tag,
        message=message,
        draft=draft,
        prerelease=prerelease,
        **_opts(target_commitish=target_commitish),
    )
    return {"success": True, "tag": r.tag_name, "url": r.html_url, "draft": r.draft}


@_write("Upload Release Asset")
@_guard
def upload_release_asset(
    repo: str | None = Field(None, description="Repository as owner/name"),
    tag: str = Field(..., min_length=1, description="Release tag"),
    path: str = Field(..., min_length=1, description="Local file path to attach"),
    label: str | None = Field(None, description="Display label; defaults to the file name"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Attach a local file to an existing release."""
    _confirmed(confirm)
    a = _repo(repo).get_release(tag).upload_asset(path, label=label or Path(path).name)
    return {"success": True, "name": a.name, "size": a.size, "url": a.browser_download_url}


# --------------------------------------------------------------------------- #
# Actions: workflows and runs (8)
# --------------------------------------------------------------------------- #
@_read("List Workflows")
@_guard
def list_workflows(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """List GitHub Actions workflows with id, name, state and path."""
    workflows = [{"id": w.id, "name": w.name, "path": w.path, "state": w.state} for w in _repo(repo).get_workflows()]
    return {"success": True, "workflows": workflows, "count": len(workflows)}


@_read("Get Workflow")
@_guard
def get_workflow(
    repo: str | None = Field(None, description="Repository as owner/name"),
    workflow_id: str = Field(..., min_length=1, description="Workflow id, name or file name"),
    recent_runs: int = Field(10, ge=1, le=100, description="How many recent runs to include"),
) -> dict[str, Any]:
    """One workflow plus its most recent runs."""
    w = _repo(repo).get_workflow(workflow_id)
    runs = [
        {
            "id": run.id,
            "status": run.status,
            "conclusion": run.conclusion,
            "event": run.event,
            "branch": run.head_branch,
            "created_at": str(run.created_at),
            "url": run.html_url,
        }
        for run in _limit(w.get_runs(), recent_runs)
    ]
    return {
        "success": True,
        "id": w.id,
        "name": w.name,
        "state": w.state,
        "path": w.path,
        "recent_runs": runs,
        "count": len(runs),
    }


@_write("Dispatch Workflow")
@_guard
def dispatch_workflow(
    repo: str | None = Field(None, description="Repository as owner/name"),
    workflow_id: str = Field(..., min_length=1, description="Workflow id, name or file name"),
    ref: str = Field("main", min_length=1, description="Branch or tag to run on"),
    inputs: dict[str, str] | None = Field(None, description="workflow_dispatch inputs"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Trigger a workflow_dispatch workflow on a ref, passing inputs."""
    _confirmed(confirm)
    ok = _repo(repo).get_workflow(workflow_id).create_dispatch(ref=ref, inputs=inputs or {})
    return {
        "success": True,
        "dispatched": bool(ok),
        "workflow_id": workflow_id,
        "ref": ref,
        "inputs": inputs or {},
    }


@_read("List Workflow Runs")
@_guard
def list_workflow_runs(
    repo: str | None = Field(None, description="Repository as owner/name"),
    workflow_id: str | None = Field(None, description="Restrict to one workflow"),
    status: Literal[
        "queued", "in_progress", "completed", "requested", "waiting", "pending", "action_required"
    ]
    | None = Field(None, description="Filter by run status"),
    branch: str | None = Field(None, description="Filter by head branch"),
    event: str | None = Field(None, description="Filter by triggering event"),
    per_page: int = Field(20, ge=1, le=100, description="Maximum runs to return"),
) -> dict[str, Any]:
    """List workflow runs, optionally filtered by workflow, status, branch or event."""
    r = _repo(repo)
    filters = _opts(status=status, branch=branch, event=event)
    runs = r.get_workflow(workflow_id).get_runs(**filters) if workflow_id else r.get_workflow_runs(**filters)
    items = [
        {
            "run_id": run.id,
            "run_number": run.run_number,
            "workflow": run.name,
            "status": run.status,
            "conclusion": run.conclusion,
            "event": run.event,
            "branch": run.head_branch,
            "commit": run.head_sha[:8] if run.head_sha else None,
            "actor": run.actor.login if run.actor else None,
            "created_at": str(run.created_at),
            "url": run.html_url,
        }
        for run in _limit(runs, per_page)
    ]
    return {"success": True, "runs": items, "count": len(items)}


@_read("Get Workflow Run")
@_guard
def get_workflow_run(
    repo: str | None = Field(None, description="Repository as owner/name"),
    run_id: int = Field(..., gt=0, description="Workflow run id"),
) -> dict[str, Any]:
    """One run with timings, conclusion and its jobs."""
    run = _repo(repo).get_workflow_run(run_id)
    jobs = [
        {
            "id": j.id,
            "name": j.name,
            "status": j.status,
            "conclusion": j.conclusion,
            "started_at": str(j.started_at) if j.started_at else None,
            "completed_at": str(j.completed_at) if j.completed_at else None,
        }
        for j in run.jobs()
    ]
    return {
        "success": True,
        "run_id": run.id,
        "run_number": run.run_number,
        "workflow": run.name,
        "status": run.status,
        "conclusion": run.conclusion,
        "event": run.event,
        "branch": run.head_branch,
        "created_at": str(run.created_at),
        "updated_at": str(run.updated_at),
        "url": run.html_url,
        "jobs": jobs,
        "job_count": len(jobs),
    }


@_write("Re-run Workflow")
@_guard
def rerun_workflow(
    repo: str | None = Field(None, description="Repository as owner/name"),
    run_id: int = Field(..., gt=0, description="Workflow run id"),
    failed_only: bool = Field(False, description="Re-run only the failed jobs"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Re-run a workflow run (all jobs, or only the failed ones)."""
    _confirmed(confirm)
    run = _repo(repo).get_workflow_run(run_id)
    run.rerun_failed_jobs() if failed_only else run.rerun()
    return {"success": True, "run_id": run_id, "rerun": True, "failed_only": failed_only}


@_write("Cancel Workflow Run", idempotent=True)
@_guard
def cancel_workflow_run(
    repo: str | None = Field(None, description="Repository as owner/name"),
    run_id: int = Field(..., gt=0, description="Workflow run id"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Cancel an in-progress workflow run."""
    _confirmed(confirm)
    _repo(repo).get_workflow_run(run_id).cancel()
    return {"success": True, "run_id": run_id, "cancelled": True}


@_read("Run Logs")
@_guard
def run_logs(
    repo: str | None = Field(None, description="Repository as owner/name"),
    run_id: int = Field(..., gt=0, description="Workflow run id"),
) -> dict[str, Any]:
    """Log URL and conclusion for a workflow run."""
    run = _repo(repo).get_workflow_run(run_id)
    return {"success": True, "logs_url": run.logs_url, "html_url": run.html_url, "conclusion": run.conclusion}


# --------------------------------------------------------------------------- #
# checks / statuses / deployments (4)
# --------------------------------------------------------------------------- #
@_read("List Check Runs")
@_guard
def list_check_runs(
    repo: str | None = Field(None, description="Repository as owner/name"),
    sha: str = Field(..., min_length=1, description="Commit sha to inspect"),
    per_page: int = Field(30, ge=1, le=100, description="Maximum check runs to return"),
) -> dict[str, Any]:
    """Check runs attached to a commit (Actions jobs, external apps). Needs checks:read."""
    checks = [
        {
            "name": c.name,
            "status": c.status,
            "conclusion": c.conclusion,
            "app": c.app.name if c.app else None,
            "url": c.html_url,
        }
        for c in _limit(_repo(repo).get_commit(sha).get_check_runs(), per_page)
    ]
    return {"success": True, "checks": checks, "count": len(checks)}


@_read("Combined Status")
@_guard
def combined_status(
    repo: str | None = Field(None, description="Repository as owner/name"),
    sha: str = Field(..., min_length=1, description="Commit sha to inspect"),
) -> dict[str, Any]:
    """Aggregate commit status (state, counts and the individual statuses)."""
    s = _repo(repo).get_commit(sha).get_combined_status()
    return {
        "success": True,
        "state": s.state,
        "total_count": s.total_count,
        "statuses": [{"context": x.context, "state": x.state, "description": x.description} for x in s.statuses],
    }


@_write("Create Check Run")
@_guard
def create_check_run(
    repo: str | None = Field(None, description="Repository as owner/name"),
    name: str = Field(..., min_length=1, description="Check run name"),
    head_sha: str = Field(..., min_length=1, description="Commit sha to attach the check to"),
    status: Literal["queued", "in_progress", "completed"] = Field("completed", description="Check status"),
    conclusion: Literal[
        "success", "failure", "neutral", "cancelled", "timed_out", "action_required", "skipped", "stale"
    ]
    | None = Field("success", description="Conclusion; required when status is completed"),
    title: str = Field("", description="Output title"),
    summary: str = Field("", description="Output summary"),
    details_url: str | None = Field(None, description="Link shown on the check"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create a check run on a commit (CI reporting)."""
    _confirmed(confirm)
    c = _repo(repo).create_check_run(
        name=name,
        head_sha=head_sha,
        status=status,
        conclusion=conclusion,
        output={"title": title, "summary": summary},
        **_opts(details_url=details_url),
    )
    return {"success": True, "id": c.id, "name": c.name, "conclusion": c.conclusion, "url": c.html_url}


@_read("List Deployments")
@_guard
def list_deployments(
    repo: str | None = Field(None, description="Repository as owner/name"),
    environment: str | None = Field(None, description="Filter by environment"),
    per_page: int = Field(20, ge=1, le=100, description="Maximum deployments to return"),
) -> dict[str, Any]:
    """List deployments with ref, environment and status."""
    deployments = [
        {
            "id": d.id,
            "ref": d.ref,
            "environment": d.environment,
            "sha": d.sha[:8],
            "created_at": str(d.created_at),
            "url": d.url,
        }
        for d in _limit(_repo(repo).get_deployments(**_opts(environment=environment)), per_page)
    ]
    return {"success": True, "deployments": deployments, "count": len(deployments)}


# --------------------------------------------------------------------------- #
# repo settings / collaborators / webhooks / secrets (9)
# --------------------------------------------------------------------------- #
@_read("List Collaborators")
@_guard
def list_collaborators(
    repo: str | None = Field(None, description="Repository as owner/name"),
    permission: Literal["pull", "triage", "push", "maintain", "admin"] | None = Field(
        None, description="Filter by permission level"
    ),
) -> dict[str, Any]:
    """List collaborators and their permissions."""
    collaborators = [
        {"login": c.login, "permissions": c.permissions}
        for c in _repo(repo).get_collaborators(**_opts(permission=permission))
    ]
    return {"success": True, "collaborators": collaborators, "count": len(collaborators)}


@_write("Add Collaborator", idempotent=True)
@_guard
def add_collaborator(
    repo: str | None = Field(None, description="Repository as owner/name"),
    username: str = Field(..., min_length=1, description="GitHub username to invite"),
    permission: Literal["pull", "triage", "push", "maintain", "admin"] = Field("push", description="Access level"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Invite a collaborator. permission: pull | triage | push | maintain | admin."""
    _confirmed(confirm)
    _repo(repo).add_to_collaborators(username, permission=permission)
    return {"success": True, "added": username, "permission": permission}


@_write("Remove Collaborator", idempotent=True)
@_guard
def remove_collaborator(
    repo: str | None = Field(None, description="Repository as owner/name"),
    username: str = Field(..., min_length=1, description="GitHub username to remove"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Remove a collaborator."""
    _confirmed(confirm)
    _repo(repo).remove_from_collaborators(username)
    return {"success": True, "removed": username}


@_read("Repo Settings")
@_guard
def repo_settings(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """Config knobs worth knowing: feature flags, merge options, visibility."""
    r = _repo(repo)
    return {
        "success": True,
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


@_write("Edit Repo", idempotent=True)
@_guard
def edit_repo(
    repo: str | None = Field(None, description="Repository as owner/name"),
    description: str | None = Field(None, description="New description; empty string clears it"),
    homepage: str | None = Field(None, description="New homepage"),
    private: bool | None = Field(None, description="Change visibility"),
    default_branch: str | None = Field(None, description="New default branch"),
    topics: list[str] | None = Field(None, description="Replacement topic list"),
    archive: bool | None = Field(None, description="Archive or unarchive the repo"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Edit repo settings or topics. Omit a field to leave it unchanged."""
    _confirmed(confirm)
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
    _repo(repo).edit(**kw)
    return {"success": True, "edited": repo or os.getenv("GITHUB_REPO"), "fields": sorted(kw)}


@_read("List Webhooks")
@_guard
def list_webhooks(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """List webhooks (url, events, active). Secret values are never returned by GitHub."""
    hooks = [
        {"id": h.id, "url": h.config.get("url"), "events": h.events, "active": h.active}
        for h in _repo(repo).get_hooks()
    ]
    return {"success": True, "webhooks": hooks, "count": len(hooks)}


@_write("Create Webhook")
@_guard
def create_webhook(
    repo: str | None = Field(None, description="Repository as owner/name"),
    url: str = Field(..., min_length=1, description="Delivery URL"),
    name: str = Field("web", description="Display label"),
    events: list[str] | None = Field(None, description="Events to subscribe to; defaults to ['push']"),
    secret: str | None = Field(None, description="Signing secret; never returned by GitHub"),
    active: bool = Field(True, description="Create the hook active"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create a webhook. name is a display label; events defaults to ['push']."""
    _confirmed(confirm)
    cfg: dict[str, str] = {"url": url, "content_type": "json", **({"secret": secret} if secret else {})}
    h = _repo(repo).create_hook(name=name, config=cfg, events=events or ["push"], active=active)
    return {"success": True, "id": h.id, "name": h.name, "url": h.config.get("url"), "events": h.events, "active": h.active}


@_write("Delete Webhook", idempotent=True)
@_guard
def delete_webhook(
    repo: str | None = Field(None, description="Repository as owner/name"),
    hook_id: int = Field(..., gt=0, description="Webhook id from list_webhooks"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Delete a webhook by id."""
    _confirmed(confirm)
    _repo(repo).get_hook(hook_id).delete()
    return {"success": True, "deleted_hook": hook_id}


@_read("List Secrets")
@_guard
def list_secrets(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """Names of Actions secrets and repo variables.

    Secret values are never readable through the API. Variable values *are*
    readable, so this returns names only and never the value itself.
    """
    r = _repo(repo)
    secrets = [{"name": s.name, "created_at": str(s.created_at), "updated_at": str(s.updated_at)} for s in r.get_secrets()]
    variables = [{"name": v.name} for v in r.get_variables()]
    return {"success": True, "secrets": secrets, "variables": variables, "note": "values are never returned"}


# --------------------------------------------------------------------------- #
# search / user / gists / misc (8)
# --------------------------------------------------------------------------- #
@_read("Search Code")
@_guard
def search_code(
    query: str = Field(..., min_length=1, description="GitHub code search query"),
    repo: str | None = Field(None, description="Restrict to this repo; empty string searches all of GitHub"),
    per_page: int = Field(20, ge=1, le=100, description="Maximum results to return"),
) -> dict[str, Any]:
    """Search code. Defaults to GITHUB_REPO; pass repo='' to search all of GitHub."""
    target = repo if repo is not None else os.getenv("GITHUB_REPO", "")
    q = f"{query} repo:{target}" if target and "repo:" not in query else query
    items = [
        {
            "repo": i.repository.full_name if i.repository else None,
            "path": i.path,
            "url": i.html_url,
        }
        for i in _limit(_client().search_code(q), per_page)
    ]
    return {"success": True, "query": q, "results": items, "count": len(items)}


@_read("Search Issues")
@_guard
def search_issues(
    query: str = Field(..., min_length=1, description="GitHub issue search query"),
    repo: str | None = Field(None, description="Restrict to this repo"),
    state: Literal["open", "closed", "all"] = Field("open", description="State filter"),
    kind: Literal["issue", "pull_request"] = Field("issue", description="Search issues or pull requests"),
    per_page: int = Field(20, ge=1, le=100, description="Maximum results to return"),
) -> dict[str, Any]:
    """Search issues (kind='issue') or PRs (kind='pull_request'), scoped to GITHUB_REPO."""
    target = repo if repo is not None else os.getenv("GITHUB_REPO", "")
    terms = [query]
    if target and "repo:" not in query:
        terms.append(f"repo:{target}")
    if state != "all" and "is:" not in query:
        terms.append(f"is:{'open' if state == 'open' else 'closed'}")
    if "is:" not in query:
        terms.append("is:pull-request" if kind == "pull_request" else "is:issue")
    q = " ".join(terms)
    items = [
        {"number": i.number, "title": i.title, "state": i.state, "url": i.html_url}
        for i in _limit(_client().search_issues(q), per_page)
    ]
    return {"success": True, "query": q, "results": items, "count": len(items)}


@_read("Search Users")
@_guard
def search_users(
    query: str = Field(..., min_length=1, description="GitHub user search query"),
    per_page: int = Field(20, ge=1, le=100, description="Maximum results to return"),
) -> dict[str, Any]:
    """Search users by login, name, email or location."""
    users = [{"login": u.login, "name": u.name, "type": u.type} for u in _limit(_client().search_users(query), per_page)]
    return {"success": True, "users": users, "count": len(users)}


@_read("Current User")
@_guard
def current_user() -> dict[str, Any]:
    """Who the token belongs to, plus rate-limit headroom."""
    g = _client()
    u = g.get_user()
    core = g.get_rate_limit().resources.core
    return {
        "success": True,
        "login": u.login,
        "name": u.name,
        "rate_remaining": core.remaining,
        "rate_limit": core.limit,
        "reset": core.reset,
    }


@_read("List Gists")
@_guard
def list_gists(username: str | None = Field(None, description="Owner; defaults to the token owner")) -> dict[str, Any]:
    """List a user's gists (id, description, files)."""
    g = _client()
    u = g.get_user(login=username) if username else g.get_user()
    gists = [{"id": x.id, "description": x.description, "url": x.html_url, "files": list(x.files)} for x in u.get_gists()]
    return {"success": True, "gists": gists, "count": len(gists)}


@_write("Create Gist")
@_guard
def create_gist(
    filename: str = Field(..., min_length=1, description="File name inside the gist"),
    content: str = Field(..., description="File content"),
    description: str = Field("", description="Gist description"),
    public: bool = Field(False, description="Create a public gist"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Create a gist from a file name and its content."""
    _confirmed(confirm)
    g = _client().get_user().create_gist(
        public=public, files={filename: InputFileContent(content)}, description=description
    )
    return {"success": True, "id": g.id, "url": g.html_url, "public": public}


@_read("Repo Events")
@_guard
def repo_events(
    repo: str | None = Field(None, description="Repository as owner/name"),
    per_page: int = Field(30, ge=1, le=100, description="Maximum events to return"),
) -> dict[str, Any]:
    """Recent repository events (pushes, issues, PRs, reviews)."""
    events = [
        {"type": e.type, "actor": e.actor.login if e.actor else None, "created_at": str(e.created_at)}
        for e in _limit(_repo(repo).get_events(), per_page)
    ]
    return {"success": True, "events": events, "count": len(events)}


@_write("Archive Repo", idempotent=True)
@_guard
def archive_repo(
    repo: str | None = Field(None, description="Repository as owner/name"),
    unarchive: bool = Field(False, description="Unarchive instead of archive"),
    confirm: bool = Field(False, description="Must be true to proceed"),
) -> dict[str, Any]:
    """Archive or unarchive the repo (an archived repo cannot be unarchived through the API)."""
    _confirmed(confirm)
    _repo(repo).edit(archived=not unarchive)
    return {"success": True, "repo": repo or os.getenv("GITHUB_REPO"), "archived": not unarchive}


# --------------------------------------------------------------------------- #
# health (1)
# --------------------------------------------------------------------------- #
@_read("Who Am I")
@_guard
def whoami(repo: str | None = Field(None, description="Repository as owner/name")) -> dict[str, Any]:
    """Health check: config present, token valid, repo reachable. Call this first when a call fails."""
    token, repo_name = os.getenv("ACCESS_TOKEN"), repo or os.getenv("GITHUB_REPO")
    if not token or not repo_name:
        return {
            "success": False,
            "error": "ACCESS_TOKEN and GITHUB_REPO must both be set in the .env file next to main.py",
        }
    try:
        r = _client().get_repo(repo_name)
    except GithubException as exc:
        status = getattr(exc, "status", None) or 500
        return {
            "success": False,
            "repo": repo_name,
            "status": status,
            "error": getattr(exc, "data", None) or str(exc),
        }
    return {"success": True, "repo": r.full_name, "default_branch": r.default_branch, "private": r.private}


if __name__ == "__main__":
    mcp.run(transport="stdio")