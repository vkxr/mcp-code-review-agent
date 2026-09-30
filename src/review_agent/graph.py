"""The multi-agent review graph.

    START -> planner -+-> security_reviewer -+-> summarizer -> human_approval -> post_review -> END
                      +-> test_runner -------+                       |
                                                                     +-> END (rejected)

* planner            ranks changed files by risk and lists focus areas
* security_reviewer  finds bugs and security issues, reading full files when needed
* test_runner        runs the test suite (if enabled) and turns failures into findings
* summarizer         de-duplicates, drops weak findings, writes the review summary
* human_approval     pauses the graph (LangGraph interrupt) until a person approves
* post_review        posts the approved review to GitHub

The reviewer and test runner run in parallel. LLM nodes retry with backoff.
"""

from __future__ import annotations

import operator
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Annotated, Any, TypedDict

import anthropic
import httpx
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy, interrupt

from .diff import number_diff_lines, parse_unified_diff
from .llm import AgentLoopError, Tool, add_usage, empty_usage, run_tool_loop
from .schemas import Findings, Plan, ReviewSummary, TestTriage
from .tools import run_tests
from .workspace import Workspace

MAX_DIFF_CHARS = 60_000


def merge_dicts(a: dict | None, b: dict | None) -> dict:
    return {**(a or {}), **(b or {})}


class ReviewState(TypedDict, total=False):
    pr: dict  # {"owner", "repo", "number"} for GitHub reviews; lets a server rebuild the workspace
    diff: str
    changed_files: list[str]
    plan: dict
    findings: Annotated[list[dict], operator.add]
    test_report: dict
    review: dict
    approved: bool
    review_url: str
    usage: Annotated[dict, add_usage]
    timings: Annotated[dict, merge_dicts]


@dataclass
class Deps:
    workspace: Workspace
    client: Any = None                # anthropic.Anthropic(); a fake in tests
    model: str | None = None
    enable_tests: bool = False
    auto_approve: bool = False
    dry_run: bool = False             # stop after the summarizer (used by evals)
    extra: dict = field(default_factory=dict)


RETRY = RetryPolicy(
    max_attempts=3,
    initial_interval=1.0,
    retry_on=(anthropic.APIConnectionError, anthropic.RateLimitError, anthropic.InternalServerError,
              AgentLoopError, httpx.TransportError),
)

PLANNER_SYSTEM = """You are the planning agent in a code review team.
Given a pull request diff, rank every changed file by review risk and list concrete focus areas
for the reviewers (for example: user input reaching SQL, auth checks, money math, concurrency,
resource cleanup). Use read_file only if the diff alone is not enough to judge risk."""

REVIEWER_SYSTEM = """You are a senior engineer reviewing a pull request for bugs and security issues.
Report only real problems that a careful reviewer would block or flag: injection, auth/authz gaps,
secrets, unsafe deserialization, broken crypto, data races, resource leaks, logic and boundary errors,
error handling that hides failures, and serious performance problems.

Rules:
- Only comment on code this pull request adds or changes.
- `line` must be the new-file line number shown at the start of each diff line.
- Each finding must explain the concrete failure it causes. No style nits, no speculation.
- Use read_file to check how changed code is used before claiming a bug depends on context.
- If there are no real issues, submit an empty list. Precision matters more than volume."""

TEST_TRIAGE_SYSTEM = """You are the test-runner agent. The pull request's test suite failed.
Explain the most likely cause and, where the failure points to a specific changed line, report it
as a finding with the new-file line number. Use read_file to inspect code if needed."""

SUMMARIZER_SYSTEM = """You are the lead reviewer. Merge the team's findings into the final review:
- Merge duplicates that describe the same problem on the same code.
- Drop findings that are speculative, stylistic, or not supported by the diff.
- Keep path and line exactly as given for findings you keep.
- Write a 2-4 sentence summary of what the pull request does and its main risks."""


def _timed(name: str, fn: Callable[[ReviewState], dict]) -> Callable[[ReviewState], dict]:
    def wrapper(state: ReviewState) -> dict:
        start = time.perf_counter()
        out = fn(state)
        out.setdefault("timings", {})[name] = round(time.perf_counter() - start, 3)
        return out
    return wrapper


def build_graph(deps: Deps, checkpointer=None):
    ws = deps.workspace

    read_file_tool = Tool(
        name="read_file",
        description="Read the full contents of a file in the repository (new version).",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
        fn=lambda path: ws.read_file(path),
    )

    def llm(**kwargs):
        return run_tool_loop(client=deps.client, model=deps.model, **kwargs)

    def fetch_diff(state: ReviewState) -> dict:
        diff = state.get("diff") or ws.get_diff()
        files = [p for p in parse_unified_diff(diff)]
        return {"diff": diff, "changed_files": files, "usage": empty_usage()}

    def planner(state: ReviewState) -> dict:
        numbered = number_diff_lines(state["diff"])[:MAX_DIFF_CHARS]
        plan, usage = llm(
            system=PLANNER_SYSTEM,
            prompt=f"Changed files: {state['changed_files']}\n\n<diff>\n{numbered}\n</diff>",
            output_model=Plan,
            tools=[read_file_tool],
            max_tokens=2048,
        )
        return {"plan": plan.model_dump(), "usage": usage}

    def security_reviewer(state: ReviewState) -> dict:
        numbered = number_diff_lines(state["diff"])[:MAX_DIFF_CHARS]
        result, usage = llm(
            system=REVIEWER_SYSTEM,
            prompt=(f"Review plan from the planner:\n{state.get('plan')}\n\n"
                    f"<diff>\n{numbered}\n</diff>"),
            output_model=Findings,
            tools=[read_file_tool],
        )
        findings = [{**f.model_dump(), "source": "security_reviewer"} for f in result.findings]
        return {"findings": findings, "usage": usage}

    def test_runner(state: ReviewState) -> dict:
        if not (deps.enable_tests and ws.repo_path):
            return {"test_report": {"status": "skipped"}}
        report = run_tests(ws.repo_path)
        if report["status"] != "failed":
            return {"test_report": report}
        triage, usage = llm(
            system=TEST_TRIAGE_SYSTEM,
            prompt=(f"<test_output>\n{report['output']}\n</test_output>\n\n"
                    f"<diff>\n{number_diff_lines(state['diff'])[:MAX_DIFF_CHARS]}\n</diff>"),
            output_model=TestTriage,
            tools=[read_file_tool],
        )
        findings = [{**f.model_dump(), "source": "test_runner"} for f in triage.findings]
        return {"test_report": {**report, "triage": triage.summary}, "findings": findings, "usage": usage}

    def summarizer(state: ReviewState) -> dict:
        findings = state.get("findings", [])
        test_report = state.get("test_report", {})
        if not findings and test_report.get("status") != "failed":
            return {"review": {"summary": "No blocking issues found.", "comments": []}}
        review, usage = llm(
            system=SUMMARIZER_SYSTEM,
            prompt=(f"Plan: {state.get('plan')}\n\nTest report: {test_report}\n\n"
                    f"Findings from the team:\n{findings}\n\n"
                    f"<diff>\n{number_diff_lines(state['diff'])[:MAX_DIFF_CHARS]}\n</diff>"),
            output_model=ReviewSummary,
        )
        changed = set(state["changed_files"])
        comments = [c.model_dump() for c in review.comments if c.path in changed]
        return {"review": {"summary": review.summary, "comments": comments}, "usage": usage}

    def human_approval(state: ReviewState) -> dict:
        if deps.auto_approve:
            return {"approved": True}
        # Pauses the graph. The run resumes with Command(resume={"approved": bool, "comments": [...]})
        decision = interrupt({"review": state["review"], "pr": state.get("pr")})
        update: dict = {"approved": bool(decision.get("approved"))}
        if decision.get("comments") is not None:  # reviewer may edit or remove comments
            update["review"] = {**state["review"], "comments": decision["comments"]}
        return update

    def post_review(state: ReviewState) -> dict:
        review = state["review"]
        body = f"## Automated review\n\n{review['summary']}"
        return {"review_url": ws.post_review(body, review["comments"])}

    g = StateGraph(ReviewState)
    g.add_node("fetch_diff", _timed("fetch_diff", fetch_diff), retry_policy=RETRY)
    g.add_node("planner", _timed("planner", planner), retry_policy=RETRY)
    g.add_node("security_reviewer", _timed("security_reviewer", security_reviewer), retry_policy=RETRY)
    g.add_node("test_runner", _timed("test_runner", test_runner), retry_policy=RETRY)
    g.add_node("summarizer", _timed("summarizer", summarizer), retry_policy=RETRY)
    g.add_node("human_approval", human_approval)
    g.add_node("post_review", _timed("post_review", post_review), retry_policy=RETRY)

    g.add_edge(START, "fetch_diff")
    g.add_edge("fetch_diff", "planner")
    g.add_edge("planner", "security_reviewer")
    g.add_edge("planner", "test_runner")
    g.add_edge(["security_reviewer", "test_runner"], "summarizer")
    if deps.dry_run:
        g.add_edge("summarizer", END)
    else:
        g.add_edge("summarizer", "human_approval")
        g.add_conditional_edges("human_approval", lambda s: "post_review" if s.get("approved") else END,
                                ["post_review", END])
        g.add_edge("post_review", END)
    return g.compile(checkpointer=checkpointer)
