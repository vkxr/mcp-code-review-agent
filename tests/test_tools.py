import asyncio
import os

from review_agent import mcp_server
from review_agent.diff import number_diff_lines, parse_unified_diff
from review_agent.tools import run_tests
from review_agent.workspace import LocalWorkspace, split_inline_comments
from tests.test_graph import DIFF


def test_mcp_server_exposes_the_four_tools():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert {t.name for t in tools} == {"get_pr_diff", "read_file", "run_tests", "post_review_comment"}
    by_name = {t.name: t for t in tools}
    assert set(by_name["post_review_comment"].input_schema["required"]) >= {"owner", "repo", "number",
                                                                            "path", "line", "body"}


def test_mcp_run_tests_tool_refuses_paths_outside_workdir(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_WORKDIR", str(tmp_path / "work"))
    result = asyncio.run(mcp_server.mcp.call_tool("run_tests", {"repo_path": "/etc"}))
    assert "refused" in str(result)


def test_run_tests_passes_and_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_WORKDIR", str(tmp_path))
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "test_ok.py").write_text("def test_ok():\n    assert 1 + 1 == 2\n")
    assert run_tests(str(repo))["status"] == "passed"
    (repo / "test_bad.py").write_text("def test_bad():\n    assert 1 + 1 == 3\n")
    result = run_tests(str(repo))
    assert result["status"] == "failed" and "test_bad" in result["output"]


def test_diff_parsing_and_numbering():
    fd = parse_unified_diff(DIFF)["app/db.py"]
    assert fd.added_lines == {6, 7, 8, 9, 10, 11}
    assert fd.hunk_lines == {3, 4, 5, 6, 7, 8, 9, 10, 11}  # 3-5 are context lines
    numbered = number_diff_lines(DIFF)
    assert "   10 +    cur.execute" in numbered


def test_split_inline_comments():
    inline, outside = split_inline_comments(DIFF, [{"path": "app/db.py", "line": 10},
                                                   {"path": "app/db.py", "line": 1}])
    assert [c["line"] for c in inline] == [10]
    assert [c["line"] for c in outside] == [1]


def test_local_workspace_blocks_path_escape(tmp_path):
    (tmp_path / "a.py").write_text("x = 1")
    ws = LocalWorkspace(diff="", repo_path=str(tmp_path))
    assert ws.read_file("a.py") == "x = 1"
    try:
        ws.read_file("../" + os.path.basename(os.path.dirname(tmp_path)))
        raise AssertionError("should not read outside repo")
    except FileNotFoundError:
        pass
