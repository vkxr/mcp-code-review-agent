"""Where the code under review lives.

`GitHubWorkspace` talks to the GitHub REST API for a real pull request.
`LocalWorkspace` holds a diff and file contents in memory; the eval harness and
tests use it so they never touch the network.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Protocol

import httpx

from .diff import parse_unified_diff

GITHUB_API = "https://api.github.com"
MAX_FILE_CHARS = 40_000


class Workspace(Protocol):
    repo_path: str | None

    def get_diff(self) -> str: ...

    def read_file(self, path: str) -> str: ...

    def post_review(self, body: str, comments: list[dict]) -> str: ...


def _truncate(text: str, limit: int = MAX_FILE_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} characters]"


def split_inline_comments(diff: str, comments: list[dict]) -> tuple[list[dict], list[dict]]:
    """Separate comments GitHub will accept inline from those it would reject."""
    files = parse_unified_diff(diff)
    inline, outside = [], []
    for c in comments:
        fd = files.get(c["path"])
        if fd and c["line"] in fd.hunk_lines:
            inline.append(c)
        else:
            outside.append(c)
    return inline, outside


def format_comment(c: dict) -> str:
    text = f"**[{c['severity'].upper()}] {c['title']}** ({c['category']})\n\n{c['explanation']}"
    if c.get("suggestion"):
        text += f"\n\n**Suggested fix:** {c['suggestion']}"
    return text


# --------------------------------------------------------------------------- GitHub


class GitHubClient:
    """Thin wrapper over the GitHub REST endpoints the reviewer needs."""

    def __init__(self, token: str | None = None, base_url: str = GITHUB_API, timeout: float = 30.0):
        token = token or os.environ.get("GITHUB_TOKEN")
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self._http = httpx.Client(base_url=base_url, headers=headers, timeout=timeout)

    def get_pull(self, owner: str, repo: str, number: int) -> dict:
        r = self._http.get(f"/repos/{owner}/{repo}/pulls/{number}")
        r.raise_for_status()
        return r.json()

    def get_pull_diff(self, owner: str, repo: str, number: int) -> str:
        r = self._http.get(
            f"/repos/{owner}/{repo}/pulls/{number}", headers={"Accept": "application/vnd.github.diff"}
        )
        r.raise_for_status()
        return r.text

    def get_file(self, owner: str, repo: str, path: str, ref: str | None = None) -> str:
        r = self._http.get(
            f"/repos/{owner}/{repo}/contents/{path.lstrip('/')}",
            params={"ref": ref} if ref else {},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        r.raise_for_status()
        return r.text

    def create_review(self, owner: str, repo: str, number: int, commit_id: str, body: str,
                      comments: list[dict]) -> str:
        payload = {
            "commit_id": commit_id,
            "body": body,
            "event": "COMMENT",
            "comments": [
                {"path": c["path"], "line": c["line"], "side": "RIGHT", "body": format_comment(c)}
                for c in comments
            ],
        }
        r = self._http.post(f"/repos/{owner}/{repo}/pulls/{number}/reviews", json=payload)
        r.raise_for_status()
        return r.json().get("html_url", "")

    def create_review_comment(self, owner: str, repo: str, number: int, commit_id: str, path: str,
                              line: int, body: str) -> str:
        payload = {"commit_id": commit_id, "path": path, "line": line, "side": "RIGHT", "body": body}
        r = self._http.post(f"/repos/{owner}/{repo}/pulls/{number}/comments", json=payload)
        r.raise_for_status()
        return r.json().get("html_url", "")


@dataclass
class GitHubWorkspace:
    owner: str
    repo: str
    number: int
    client: GitHubClient = field(default_factory=GitHubClient)
    repo_path: str | None = None  # local checkout, only needed for the test runner
    _head_sha: str | None = None
    _diff: str | None = None

    @classmethod
    def from_ref(cls, ref: str, **kwargs) -> GitHubWorkspace:
        """Parse 'owner/repo#123'."""
        repo_part, _, num = ref.partition("#")
        owner, _, repo = repo_part.partition("/")
        if not (owner and repo and num.isdigit()):
            raise ValueError(f"Expected owner/repo#number, got {ref!r}")
        return cls(owner=owner, repo=repo, number=int(num), **kwargs)

    @property
    def head_sha(self) -> str:
        if self._head_sha is None:
            self._head_sha = self.client.get_pull(self.owner, self.repo, self.number)["head"]["sha"]
        return self._head_sha

    def get_diff(self) -> str:
        if self._diff is None:
            self._diff = self.client.get_pull_diff(self.owner, self.repo, self.number)
        return self._diff

    def read_file(self, path: str) -> str:
        return _truncate(self.client.get_file(self.owner, self.repo, path, self.head_sha))

    def post_review(self, body: str, comments: list[dict]) -> str:
        inline, outside = split_inline_comments(self.get_diff(), comments)
        if outside:
            body += "\n\n### Findings outside the diff\n" + "\n\n".join(
                f"`{c['path']}:{c['line']}` {format_comment(c)}" for c in outside
            )
        return self.client.create_review(self.owner, self.repo, self.number, self.head_sha, body, inline)


# --------------------------------------------------------------------------- Local


@dataclass
class LocalWorkspace:
    diff: str
    files: dict[str, str] = field(default_factory=dict)
    repo_path: str | None = None
    posted: list[dict] = field(default_factory=list)

    def get_diff(self) -> str:
        return self.diff

    def read_file(self, path: str) -> str:
        path = path.lstrip("/")
        if path in self.files:
            return _truncate(self.files[path])
        if self.repo_path:
            root = os.path.realpath(self.repo_path)
            full = os.path.realpath(os.path.join(root, path))
            if full.startswith(root + os.sep) and os.path.isfile(full):
                with open(full, encoding="utf-8", errors="replace") as f:
                    return _truncate(f.read())
        raise FileNotFoundError(path)

    def post_review(self, body: str, comments: list[dict]) -> str:
        inline, outside = split_inline_comments(self.diff, comments)
        self.posted.append({"body": body, "inline": inline, "outside": outside})
        return "local://review"
