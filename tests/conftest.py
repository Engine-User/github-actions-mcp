"""Pytest fixtures for the github-actions MCP server tests."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# The server is a single module at the project root, not an installed package.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def env(monkeypatch):
    """Give every test a token and a repo so helpers never short-circuit on config."""
    monkeypatch.setenv("ACCESS_TOKEN", "test_token_123")
    monkeypatch.setenv("GITHUB_REPO", "testowner/testrepo")


@pytest.fixture
def gh():
    """Patch ``main.Github`` so no test ever touches the network.

    Yields the repo mock; ``client`` and ``Github`` are also exposed for the
    few tools that build a client without resolving a repo first.
    """
    with patch("main.Github") as gh_class:
        client = MagicMock()
        gh_class.return_value = client
        repo = MagicMock()
        client.get_repo.return_value = repo
        yield {"Github": gh_class, "client": client, "repo": repo}


@pytest.fixture
def branch():
    b = MagicMock()
    b.name = "main"
    b.protected = True
    b.commit.sha = "abc123def456"
    return b


@pytest.fixture
def issue():
    i = MagicMock()
    i.number = 42
    i.html_url = "https://github.com/testowner/testrepo/issues/42"
    i.title = "Test Issue"
    i.body = "Test body"
    i.state = "open"
    i.comments = 0
    i.closed_at = None
    i.pull_request = None
    i.created_at = MagicMock(**{"__str__": lambda s: "2026-01-15T10:30:00+00:00"})
    label = MagicMock()
    label.name = "bug"
    i.labels = [label]
    assignee = MagicMock()
    assignee.login = "alice"
    i.assignees = [assignee]
    return i


@pytest.fixture
def pull_request():
    p = MagicMock()
    p.number = 123
    p.html_url = "https://github.com/testowner/testrepo/pull/123"
    p.title = "Test PR"
    p.body = "PR body"
    p.state = "open"
    p.draft = False
    p.merged = False
    p.mergeable = True
    p.mergeable_state = "clean"
    p.head.ref, p.head.sha = "feature", "def456"
    p.base.ref, p.base.sha = "main", "abc123"
    p.additions, p.deletions, p.changed_files = 10, 5, 3
    label = MagicMock()
    label.name = "enhancement"
    p.labels = [label]
    return p