from __future__ import annotations

import difflib

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from review_agent.graph import Deps, build_graph
from review_agent.workspace import LocalWorkspace
from tests.fakes import FakeClient, tool_use

BEFORE = "import sqlite3\n\n\ndef ping():\n    return 'ok'\n"
AFTER = BEFORE + (
    "\n\ndef find_user(conn, email):\n"
    "    cur = conn.cursor()\n"
    "    cur.execute(f\"SELECT * FROM users WHERE email = '{email}'\")\n"
    "    return cur.fetchone()\n"
)
DIFF = "".join(difflib.unified_diff(BEFORE.splitlines(True), AFTER.splitlines(True),
                                    "a/app/db.py", "b/app/db.py"))

FINDING = {"path": "app/db.py", "line": 10, "severity": "critical", "category": "security",
           "title": "SQL injection", "explanation": "email is interpolated into SQL.",
           "suggestion": "Use a parameterized query."}


def scripted_client(extra_reviewer_turn: bool = False) -> FakeClient:
    reviewer_turns = []
    if extra_reviewer_turn:  # reviewer first reads a file, then submits
        reviewer_turns.append([tool_use("read_file", {"path": "app/db.py"})])
    reviewer_turns.append([tool_use("submit_result", {"findings": [FINDING]})])
    return FakeClient({
        "planning agent": [[tool_use("submit_result", {
            "files": [{"path": "app/db.py", "risk": "high", "reason": "builds SQL"}],
            "focus_areas": ["SQL built from input"]})]],
        "senior engineer": reviewer_turns,
        "lead reviewer": [[tool_use("submit_result", {"summary": "Adds find_user.", "comments": [FINDING]})]],
    })


def test_dry_run_produces_review_and_usage():
    client = scripted_client(extra_reviewer_turn=True)
    graph = build_graph(Deps(workspace=LocalWorkspace(diff=DIFF, files={"app/db.py": AFTER}),
                             client=client, dry_run=True))
    out = graph.invoke({})
    assert out["changed_files"] == ["app/db.py"]
    assert out["review"]["comments"][0]["line"] == 10
    assert out["usage"]["llm_calls"] == 4  # planner 1 + reviewer 2 + summarizer 1
    assert out["test_report"]["status"] == "skipped"
    assert {"planner", "security_reviewer", "summarizer"} <= set(out["timings"])
    # the read_file tool result went back to Claude
    reviewer_second_call = [c for c in client.calls if "senior engineer" in c["system"]][1]
    assert "def find_user" in str(reviewer_second_call["messages"][-1]["content"])


def test_human_in_the_loop_approve_posts_review():
    ws = LocalWorkspace(diff=DIFF, files={"app/db.py": AFTER})
    graph = build_graph(Deps(workspace=ws, client=scripted_client()), checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t1"}}

    first = graph.invoke({}, config)
    assert "__interrupt__" in first
    assert ws.posted == []  # nothing posted before approval

    final = graph.invoke(Command(resume={"approved": True}), config)
    assert final["approved"] is True
    assert ws.posted[0]["inline"][0]["line"] == 10


def test_human_in_the_loop_reject_posts_nothing():
    ws = LocalWorkspace(diff=DIFF, files={"app/db.py": AFTER})
    graph = build_graph(Deps(workspace=ws, client=scripted_client()), checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "t2"}}
    graph.invoke({}, config)
    final = graph.invoke(Command(resume={"approved": False}), config)
    assert final["approved"] is False
    assert ws.posted == []


def test_invalid_submission_is_sent_back_for_correction():
    bad = {"findings": [{"path": "app/db.py"}]}  # missing required fields
    client = FakeClient({
        "planning agent": [[tool_use("submit_result", {"files": [], "focus_areas": []})]],
        "senior engineer": [[tool_use("submit_result", bad)],
                            [tool_use("submit_result", {"findings": [FINDING]})]],
        "lead reviewer": [[tool_use("submit_result", {"summary": "s", "comments": [FINDING]})]],
    })
    graph = build_graph(Deps(workspace=LocalWorkspace(diff=DIFF), client=client, dry_run=True))
    out = graph.invoke({})
    assert out["review"]["comments"][0]["title"] == "SQL injection"
    retry_msg = [c for c in client.calls if "senior engineer" in c["system"]][1]["messages"][-1]
    assert retry_msg["content"][0]["is_error"] is True


def test_no_findings_skips_summarizer_llm_call():
    client = FakeClient({
        "planning agent": [[tool_use("submit_result", {"files": [], "focus_areas": []})]],
        "senior engineer": [[tool_use("submit_result", {"findings": []})]],
    })
    out = build_graph(Deps(workspace=LocalWorkspace(diff=DIFF), client=client, dry_run=True)).invoke({})
    assert out["review"]["comments"] == []
    assert out["usage"]["llm_calls"] == 2


@pytest.mark.parametrize("path", ["other/file.py"])
def test_summarizer_drops_comments_on_unchanged_files(path):
    stray = {**FINDING, "path": path}
    client = FakeClient({
        "planning agent": [[tool_use("submit_result", {"files": [], "focus_areas": []})]],
        "senior engineer": [[tool_use("submit_result", {"findings": [FINDING]})]],
        "lead reviewer": [[tool_use("submit_result", {"summary": "s", "comments": [FINDING, stray]})]],
    })
    out = build_graph(Deps(workspace=LocalWorkspace(diff=DIFF), client=client, dry_run=True)).invoke({})
    assert [c["path"] for c in out["review"]["comments"]] == ["app/db.py"]
