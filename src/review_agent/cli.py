"""Command line interface.

    review-agent review owner/repo#123            review a GitHub PR, ask before posting
    review-agent review owner/repo#123 --yes      post without asking
    review-agent review --diff change.diff        review a local diff (prints only)
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .graph import Deps, build_graph
from .llm import DEFAULT_MODEL, cost_usd
from .workspace import GitHubWorkspace, LocalWorkspace, format_comment


def print_review(review: dict) -> None:
    print("\n" + "=" * 72)
    print(review["summary"])
    print("=" * 72)
    if not review["comments"]:
        print("No comments.")
    for c in review["comments"]:
        print(f"\n{c['path']}:{c['line']}")
        print(format_comment(c))


def cmd_review(args: argparse.Namespace) -> int:
    model = args.model or DEFAULT_MODEL
    if args.diff:
        with open(args.diff, encoding="utf-8") as f:
            ws = LocalWorkspace(diff=f.read(), repo_path=args.root)
        deps = Deps(workspace=ws, model=model, enable_tests=args.tests, dry_run=True)
        pr = None
    else:
        ws = GitHubWorkspace.from_ref(args.pr, repo_path=args.root)
        deps = Deps(workspace=ws, model=model, enable_tests=args.tests, auto_approve=args.yes)
        pr = {"owner": ws.owner, "repo": ws.repo, "number": ws.number}

    conn = sqlite3.connect(args.db, check_same_thread=False)
    graph = build_graph(deps, checkpointer=SqliteSaver(conn))
    config = {"configurable": {"thread_id": f"{args.pr or args.diff}@{int(time.time())}"}}

    start = time.perf_counter()
    state = graph.invoke({"pr": pr} if pr else {}, config)

    if "__interrupt__" in state:  # waiting for a human decision
        print_review(state["review"])
        answer = input("\nPost this review to GitHub? [y/N] ").strip().lower()
        state = graph.invoke(Command(resume={"approved": answer == "y"}), config)
    else:
        print_review(state["review"])

    usage = state.get("usage", {})
    cost = cost_usd(usage, model) if usage else None
    stats = {"seconds": round(time.perf_counter() - start, 1), **usage}
    if cost is not None:
        stats["cost_usd"] = round(cost, 4)
    if state.get("review_url"):
        print(f"\nPosted: {state['review_url']}")
    elif pr and not state.get("approved"):
        print("\nNot posted.")
    print("\n" + json.dumps(stats), file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="review-agent")
    sub = parser.add_subparsers(dest="command", required=True)
    r = sub.add_parser("review", help="Review a pull request or a local diff")
    r.add_argument("pr", nargs="?", help="owner/repo#number")
    r.add_argument("--diff", help="Path to a unified diff file instead of a GitHub PR")
    r.add_argument("--root", help="Local checkout of the repo (needed for --tests and extra file reads)")
    r.add_argument("--tests", action="store_true", help="Run the test suite (untrusted code, use Docker)")
    r.add_argument("--yes", action="store_true", help="Skip the approval prompt and post immediately")
    r.add_argument("--model", help=f"Claude model (default {DEFAULT_MODEL})")
    r.add_argument("--db", default="reviews.sqlite", help="Checkpoint database")
    args = parser.parse_args(argv)

    if args.command == "review":
        if not (args.pr or args.diff):
            parser.error("give owner/repo#number or --diff")
        return cmd_review(args)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
