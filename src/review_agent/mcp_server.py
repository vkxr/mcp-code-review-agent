"""MCP server exposing the reviewer's GitHub tools.

Any MCP client (Claude Desktop, Claude Code, your own agent) can connect to
this server over stdio and call these tools directly.

Run:  review-agent-mcp          (stdio, for Claude Desktop)
      review-agent-mcp --http   (streamable HTTP on :8765)
"""

from __future__ import annotations

import argparse

from mcp.server.mcpserver import MCPServer

from .diff import number_diff_lines
from .tools import run_tests as _run_tests
from .workspace import GitHubClient

mcp = MCPServer("code-review-tools")
_client: GitHubClient | None = None


def gh() -> GitHubClient:
    global _client
    if _client is None:
        _client = GitHubClient()
    return _client


@mcp.tool()
def get_pr_diff(owner: str, repo: str, number: int) -> str:
    """Fetch a pull request's unified diff, with new-file line numbers on every added/context line."""
    return number_diff_lines(gh().get_pull_diff(owner, repo, number))


@mcp.tool()
def read_file(owner: str, repo: str, path: str, ref: str | None = None) -> str:
    """Read a file from the repository at `ref` (branch, tag or commit SHA; defaults to the default branch)."""
    return gh().get_file(owner, repo, path, ref)


@mcp.tool()
def run_tests(repo_path: str, test_path: str | None = None) -> dict:
    """Run pytest in a local checkout under REVIEW_WORKDIR. Returns status, exit code and output."""
    return _run_tests(repo_path, test_path)


@mcp.tool()
def post_review_comment(owner: str, repo: str, number: int, path: str, line: int, body: str) -> str:
    """Post one inline review comment on a line of the pull request's new version. Returns its URL."""
    head_sha = gh().get_pull(owner, repo, number)["head"]["sha"]
    return gh().create_review_comment(owner, repo, number, head_sha, path, line, body)


def main() -> None:
    parser = argparse.ArgumentParser(description="Code review MCP server")
    parser.add_argument("--http", action="store_true", help="Serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if args.http:
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
