from evals.cases import CASES
from evals.run_eval import percentile, run_case, score_case, summarize
from review_agent.diff import parse_unified_diff
from tests.fakes import FakeClient, tool_use


def test_thirty_unique_cases_with_bug_on_an_added_line():
    assert len(CASES) == 30
    assert len({c.id for c in CASES}) == 30
    for c in CASES:
        assert c.bug_line in parse_unified_diff(c.diff)[c.path].added_lines, c.id


def test_score_case_counts_hits_within_tolerance():
    c = CASES[0]
    comments = [
        {"path": c.path, "line": c.bug_line + 1},    # hit
        {"path": c.path, "line": c.bug_line + 10},   # false positive
        {"path": "other.py", "line": c.bug_line},    # false positive
    ]
    s = score_case(c, comments)
    assert s == {"detected": True, "true_positive_comments": 1,
                 "false_positive_comments": 2, "total_comments": 3}


def test_run_case_and_summary_end_to_end_with_fake_model():
    c = CASES[0]
    finding = {"path": c.path, "line": c.bug_line, "severity": "critical", "category": "security",
               "title": "SQL injection", "explanation": "x"}
    client = FakeClient({
        "planning agent": [[tool_use("submit_result", {"files": [], "focus_areas": []})]],
        "senior engineer": [[tool_use("submit_result", {"findings": [finding]})]],
        "lead reviewer": [[tool_use("submit_result", {"summary": "s", "comments": [finding]})]],
    })
    result = run_case(c, client, "claude-haiku-4-5-20251001")
    assert result["detected"] and result["error"] is None
    summary = summarize([result], "claude-haiku-4-5-20251001")
    assert summary["detection_rate"] == 1.0
    assert summary["false_positive_rate"] == 0
    assert summary["avg_cost_usd_per_review"] is not None


def test_percentile():
    assert percentile([1, 2, 3, 4], 0.5) == 2.5
    assert percentile([], 0.95) == 0.0
