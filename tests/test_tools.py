"""Tests for the github-actions MCP server.

Two layers are covered:
  * the registered tool surface (schemas, annotations, confirm gates), read
    through the MCP registry rather than the Python functions, because
    ``@mcp.tool()`` returns the function untouched and attaches nothing to it
  * tool bodies, called directly with PyGithub mocked out
"""

from __future__ import annotations

import asyncio
import base64
from unittest.mock import MagicMock

import pytest
from github import GithubException

import main
from mcp.server.mcpserver.exceptions import ToolError

TOOL_COUNT = 66


def registered_tools() -> list:
    return asyncio.run(main.mcp.list_tools())


# --------------------------------------------------------------------------- #
# tool surface
# --------------------------------------------------------------------------- #
class TestToolSurface:
    def test_exactly_66_tools_are_registered(self):
        assert len(registered_tools()) == TOOL_COUNT

    def test_every_tool_has_full_annotations(self):
        for t in registered_tools():
            ann = t.annotations
            assert ann is not None, f"{t.name} has no annotations"
            assert ann.title, f"{t.name} has no title"
            assert ann.read_only_hint is not None, f"{t.name} has no readOnlyHint"
            assert ann.destructive_hint is not None, f"{t.name} has no destructiveHint"
            assert ann.idempotent_hint is not None, f"{t.name} has no idempotentHint"
            assert ann.open_world_hint is not None, f"{t.name} has no openWorldHint"

    def test_read_only_tools_are_consistent(self):
        for t in registered_tools():
            if t.annotations.read_only_hint:
                assert t.annotations.destructive_hint is False, f"{t.name} is read-only but destructive"
                assert t.annotations.idempotent_hint is True, f"{t.name} is read-only but not idempotent"

    def test_every_mutating_tool_takes_confirm(self):
        for t in registered_tools():
            if not t.annotations.read_only_hint:
                assert "confirm" in (t.input_schema.get("properties") or {}), f"{t.name} has no confirm param"
                assert t.annotations.destructive_hint is True, f"{t.name} mutates but is not marked destructive"

    def test_required_arguments_are_validated(self):
        """Schema bounds must exist so the SDK rejects bad input before the body runs."""
        by_name = {t.name: t for t in registered_tools()}
        assert by_name["read_file"].input_schema["required"] == ["path"]
        per_page = by_name["list_commits"].input_schema["properties"]["per_page"]
        assert per_page["minimum"] == 1 and per_page["maximum"] == 100
        assert by_name["get_issue"].input_schema["properties"]["number"]["exclusiveMinimum"] == 0

    def test_unknown_tool_and_bad_args_report_cleanly(self):
        with pytest.raises(ToolError):
            asyncio.run(main.mcp.call_tool("get_issue", {}))
        with pytest.raises(ToolError):
            asyncio.run(main.mcp.call_tool("get_issue", {"number": 0}))


# --------------------------------------------------------------------------- #
# confirm gate
# --------------------------------------------------------------------------- #
class TestConfirmGate:
    """Every mutating tool must refuse to run without confirm=true.

    Each entry is the exact call with confirm omitted. ``create_gist`` is the one
    mutating tool that takes no ``repo`` argument, since gists are user-scoped.
    """

    CASES = [
        (main.create_or_update_file, {"repo": None, "path": "a.txt", "content": "x", "message": "m"}),
        (main.delete_file, {"repo": None, "path": "a.txt", "message": "m", "sha": "abc"}),
        (main.create_branch, {"repo": None, "name": "feat"}),
        (main.create_tag, {"repo": None, "name": "v1"}),
        (main.create_issue, {"repo": None, "issue_title": "t", "issue_text": "b"}),
        (main.update_issue, {"repo": None, "number": 1, "title": "t"}),
        (main.comment_on_issue, {"repo": None, "number": 1, "body": "b"}),
        (main.lock_issue, {"repo": None, "number": 1}),
        (main.create_label, {"repo": None, "name": "bug"}),
        (main.create_milestone, {"repo": None, "title": "v1"}),
        (main.create_pull_request, {"repo": None, "title": "t", "head": "feat"}),
        (main.update_pull_request, {"repo": None, "number": 1, "title": "t"}),
        (main.merge_pull_request, {"repo": None, "number": 1}),
        (main.create_pull_review, {"repo": None, "number": 1, "body": "b"}),
        (main.request_reviewers, {"repo": None, "number": 1, "reviewers": ["a"]}),
        (main.create_release, {"repo": None, "tag": "v1"}),
        (main.upload_release_asset, {"repo": None, "tag": "v1", "path": "f.bin"}),
        (main.dispatch_workflow, {"repo": None, "workflow_id": "ci.yml"}),
        (main.rerun_workflow, {"repo": None, "run_id": 1}),
        (main.cancel_workflow_run, {"repo": None, "run_id": 1}),
        (main.create_check_run, {"repo": None, "name": "ci", "head_sha": "abc"}),
        (main.add_collaborator, {"repo": None, "username": "a"}),
        (main.remove_collaborator, {"repo": None, "username": "a"}),
        (main.edit_repo, {"repo": None, "description": "d"}),
        (main.create_webhook, {"repo": None, "url": "https://example.test/hook"}),
        (main.delete_webhook, {"repo": None, "hook_id": 1}),
        (main.create_gist, {"filename": "f.txt", "content": "c"}),
        (main.archive_repo, {"repo": None}),
    ]

    def test_all_28_mutating_tools_are_covered(self):
        mutating = [t for t in registered_tools() if not t.annotations.read_only_hint]
        assert len(mutating) == len(self.CASES) == 28

    @pytest.mark.parametrize("tool,kwargs", CASES, ids=[case[0].__name__ for case in CASES])
    def test_refuses_without_confirm(self, tool, kwargs, gh):
        with pytest.raises(ToolError, match="confirm=true"):
            tool(confirm=False, **kwargs)
        gh["repo"].assert_not_called()


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
class TestHelpers:
    def test_limit_caps_iterables(self):
        assert list(main._limit(range(10), 3)) == [0, 1, 2]
        assert list(main._limit(range(3), 10)) == [0, 1, 2]
        assert list(main._limit(range(3), 0)) == []

    def test_opts_drops_none(self):
        assert main._opts(a=1, b=None, c="x") == {"a": 1, "c": "x"}

    def test_file_block_encodes_both_ways(self):
        assert main._file_block("hi", "utf-8") == b"hi"
        assert main._file_block(base64.b64encode(b"hi").decode(), "base64") == b"hi"
        assert main._file_block(None, "utf-8") == b""

    def test_link_returns_none_when_absent(self):
        assert main._link(main.NotSet, "sha") is None

    def test_gh_err_keeps_status_and_reason(self):
        err = main._gh_err(GithubException(404, {"message": "Not Found"}), "get_issue")
        assert "get_issue" in str(err)
        assert "404" in str(err)

    def test_gh_err_defaults_to_500_without_status(self):
        assert "500" in str(main._gh_err(GithubException(None, None), "x"))

    def test_confirmed_raises_only_when_false(self):
        assert main._confirmed(True) is None
        with pytest.raises(ToolError):
            main._confirmed(False)


# --------------------------------------------------------------------------- #
# repo metadata
# --------------------------------------------------------------------------- #
class TestRepoMetadata:
    def test_repo_info_shape(self, gh):
        r = gh["repo"]
        r.full_name, r.description, r.private = "testowner/testrepo", "d", False
        r.default_branch, r.language = "main", "Python"
        r.get_topics.return_value = ["mcp"]
        r.stargazers_count, r.forks_count, r.open_issues_count, r.size = 100, 20, 5, 1024
        r.license = None
        r.permissions = {"admin": True}

        out = main.repo_info(repo=None)

        assert out["success"] is True
        assert out["full_name"] == "testowner/testrepo"
        assert out["stars"] == 100
        assert out["topics"] == ["mcp"]

    def test_repo_branches_filters_protected(self, gh):
        protected, plain = MagicMock(), MagicMock()
        protected.name, protected.protected, protected.commit.sha = "main", True, "aaa"
        plain.name, plain.protected, plain.commit.sha = "dev", False, "bbb"
        gh["repo"].get_branches.return_value = [protected, plain]

        assert main.repo_branches(repo=None, protected_only=False)["count"] == 2
        only = main.repo_branches(repo=None, protected_only=True)
        assert only["count"] == 1 and only["branches"][0]["name"] == "main"

    def test_repo_env_fallback(self, gh, monkeypatch):
        monkeypatch.delenv("GITHUB_REPO", raising=False)
        with pytest.raises(ToolError, match="GITHUB_REPO"):
            main.repo_info(repo=None)


# --------------------------------------------------------------------------- #
# files
# --------------------------------------------------------------------------- #
class TestFiles:
    def test_read_file_truncates(self, gh):
        f = gh["repo"].get_contents.return_value
        f.decoded_content = b"abcdef"
        f.sha = "blob1"

        full = main.read_file(repo=None, path="a.txt")
        assert full == {"success": True, "path": "a.txt", "content": "abcdef", "size": 6, "truncated": False, "sha": "blob1"}

        cut = main.read_file(repo=None, path="a.txt", max_bytes=3)
        assert cut["content"] == "abc" and cut["truncated"] is True and cut["size"] == 6

    def test_read_file_rejects_directories(self, gh):
        gh["repo"].get_contents.return_value = [object()]
        with pytest.raises(ToolError, match="list_directory"):
            main.read_file(repo=None, path="src")

    def test_create_uses_sha_to_decide_create_or_update(self, gh):
        res = {"content": MagicMock(path="a.txt", sha="blob2"), "commit": MagicMock(html_url="https://c")}
        gh["repo"].create_file.return_value = res
        gh["repo"].update_file.return_value = res

        created = main.create_or_update_file(repo=None, path="a.txt", content="x", message="m", confirm=True)
        assert created["operation"] == "create"
        gh["repo"].create_file.assert_called_once()
        gh["repo"].update_file.assert_not_called()

        updated = main.create_or_update_file(
            repo=None, path="a.txt", content="x", message="m", sha="blob1", confirm=True
        )
        assert updated["operation"] == "update"
        gh["repo"].update_file.assert_called_once()


# --------------------------------------------------------------------------- #
# issues
# --------------------------------------------------------------------------- #
class TestIssues:
    def test_create_issue_returns_locator(self, gh, issue):
        gh["repo"].create_issue.return_value = issue
        out = main.create_issue(repo=None, issue_title="T", issue_text="B", confirm=True)
        assert out["success"] is True and out["number"] == 42
        assert out["url"].endswith("/issues/42")

    def test_list_issues_flags_pull_requests(self, gh, issue):
        issue.pull_request = {"url": "https://api/pull/42"}
        gh["repo"].get_issues.return_value = [issue]
        out = main.list_issues(repo=None, state="open", labels=[], sort="created", direction="desc", per_page=30)
        assert out["count"] == 1
        assert out["issues"][0]["is_pr"] is True
        assert out["issues"][0]["labels"] == ["bug"]
        assert out["issues"][0]["assignees"] == ["alice"]

    def test_update_issue_re_reads_after_edit(self, gh, issue):
        gh["repo"].get_issue.return_value = issue
        out = main.update_issue(repo=None, number=42, state="closed", confirm=True)
        issue.edit.assert_called_once_with(state="closed")
        assert out["success"] is True and out["state"] == "open"


# --------------------------------------------------------------------------- #
# pull requests
# --------------------------------------------------------------------------- #
class TestPullRequests:
    def test_create_pull_defaults_base_to_default_branch(self, gh, pull_request):
        gh["repo"].default_branch = "main"
        gh["repo"].create_pull.return_value = pull_request
        out = main.create_pull_request(repo=None, title="T", head="feat", body="", draft=False, confirm=True)
        assert out["number"] == 123
        assert gh["repo"].create_pull.call_args.kwargs["base"] == "main"

    def test_merge_survives_a_failed_merge(self, gh, pull_request):
        """A conflict comes back as a falsy merge result, not as an exception."""
        gh["repo"].get_pull.return_value = pull_request
        failed = MagicMock(sha=None, message="Merge conflict")
        failed.__bool__.return_value = False
        pull_request.merge.return_value = failed
        out = main.merge_pull_request(repo=None, number=123, merge_method="merge", confirm=True)
        assert out["merged"] is False and out["sha"] is None
        assert out["message"] == "Merge conflict"


# --------------------------------------------------------------------------- #
# security
# --------------------------------------------------------------------------- #
class TestSecretHandling:
    def test_list_secrets_never_returns_variable_values(self, gh):
        """Regression: variable values used to be serialised into the response."""
        secret = MagicMock(name="s", created_at="2026-01-01", updated_at="2026-01-02")
        secret.name = "API_KEY"
        variable = MagicMock(name="v")
        variable.name = "ENVIRONMENT"
        variable.value = "production"
        gh["repo"].get_secrets.return_value = [secret]
        gh["repo"].get_variables.return_value = [variable]

        out = main.list_secrets(repo=None)

        assert "production" not in repr(out)
        assert out["secrets"][0]["name"] == "API_KEY"
        assert out["variables"] == [{"name": "ENVIRONMENT"}]


# --------------------------------------------------------------------------- #
# error contract
# --------------------------------------------------------------------------- #
class TestErrorContract:
    def test_api_failure_becomes_a_tool_error_naming_the_tool(self, gh, issue):
        gh["repo"].get_issue.side_effect = GithubException(404, {"message": "Not Found"})
        with pytest.raises(ToolError) as excinfo:
            main.get_issue(repo=None, number=999)
        assert "get_issue" in str(excinfo.value)
        assert "404" in str(excinfo.value)

    def test_missing_token_is_a_tool_error_not_a_crash(self, gh, monkeypatch):
        monkeypatch.delenv("ACCESS_TOKEN", raising=False)
        with pytest.raises(ToolError, match="ACCESS_TOKEN"):
            main.repo_info(repo=None)

    def test_uncaught_api_failure_never_escapes_as_a_crash(self, gh):
        """Anything the body forgets is converted, so the model still reads the reason."""
        gh["repo"].get_branches.side_effect = GithubException(500, None)
        with pytest.raises(ToolError, match="repo_branches"):
            main.repo_branches(repo=None, protected_only=False)

    def test_milestone_rejects_a_bad_date_before_calling_github(self, gh):
        with pytest.raises(ToolError, match="YYYY-MM-DD"):
            main.create_milestone(repo=None, title="v1", due_on="31-01-2026", confirm=True)
        gh["repo"].create_milestone.assert_not_called()


# --------------------------------------------------------------------------- #
# health
# --------------------------------------------------------------------------- #
class TestHealth:
    def test_whoami_ok(self, gh):
        gh["repo"].full_name, gh["repo"].default_branch, gh["repo"].private = "testowner/testrepo", "main", False
        out = main.whoami(repo=None)
        assert out == {
            "success": True,
            "repo": "testowner/testrepo",
            "default_branch": "main",
            "private": False,
        }

    def test_whoami_reports_missing_config_without_raising(self, gh, monkeypatch):
        monkeypatch.delenv("ACCESS_TOKEN", raising=False)
        out = main.whoami(repo=None)
        assert out["success"] is False
        assert "ACCESS_TOKEN" in out["error"]

    def test_whoami_reports_an_invalid_token_without_raising(self, gh):
        gh["client"].get_repo.side_effect = GithubException(401, {"message": "Bad credentials"})
        out = main.whoami(repo=None)
        assert out["success"] is False and out["status"] == 401