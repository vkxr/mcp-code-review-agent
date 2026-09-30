"""Run the reviewer over the 30 seeded-bug cases and score it.

    python -m evals.run_eval                      # all cases, default model
    python -m evals.run_eval --limit 5 --model claude-haiku-4-5-20251001

Metrics
- detection rate      seeded bugs found / seeded bugs (a hit = same file, within +/-LINE_TOLERANCE lines)
- false-positive rate comments that match no seeded bug / all comments posted
- tokens, cost, latency per review (p50 / p95)

Writes evals/results/<timestamp>_<model>.json and .md.
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import anthropic

from evals.cases import CASES, Case
from review_agent.graph import Deps, build_graph
from review_agent.llm import DEFAULT_MODEL, cost_usd
from review_agent.workspace import LocalWorkspace

LINE_TOLERANCE = 2
RESULTS_DIR = Path(__file__).parent / "results"


def score_case(case: Case, comments: list[dict]) -> dict:
    def matches(c: dict) -> bool:
        return c["path"] == case.path and abs(int(c["line"]) - case.bug_line) <= LINE_TOLERANCE

    hits = [c for c in comments if matches(c)]
    return {
        "detected": bool(hits),
        "true_positive_comments": len(hits),
        "false_positive_comments": len(comments) - len(hits),
        "total_comments": len(comments),
    }


def run_case(case: Case, client, model: str) -> dict:
    ws = LocalWorkspace(diff=case.diff, files={case.path: case.after})
    graph = build_graph(Deps(workspace=ws, client=client, model=model, dry_run=True))
    start = time.perf_counter()
    try:
        state = graph.invoke({})
        error = None
    except Exception as exc:  # a failed case counts as a miss, not a crash of the whole run
        state, error = {}, f"{type(exc).__name__}: {exc}"
    latency = time.perf_counter() - start
    comments = state.get("review", {}).get("comments", [])
    usage = state.get("usage", {"input_tokens": 0, "output_tokens": 0, "llm_calls": 0})
    return {
        "id": case.id, "category": case.category, "expected_line": case.bug_line,
        **score_case(case, comments),
        "latency_s": round(latency, 2), "usage": usage, "cost_usd": cost_usd(usage, model),
        "error": error, "comments": comments,
    }


def percentile(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    values = sorted(values)
    k = (len(values) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (k - lo)


def summarize(results: list[dict], model: str) -> dict:
    n = len(results)
    total_comments = sum(r["total_comments"] for r in results)
    fps = sum(r["false_positive_comments"] for r in results)
    latencies = [r["latency_s"] for r in results]
    tokens = [r["usage"]["input_tokens"] + r["usage"]["output_tokens"] for r in results]
    costs = [r["cost_usd"] for r in results if r["cost_usd"] is not None]
    by_category: dict[str, list[bool]] = {}
    for r in results:
        by_category.setdefault(r["category"], []).append(r["detected"])
    return {
        "model": model,
        "cases": n,
        "detection_rate": round(sum(r["detected"] for r in results) / n, 3) if n else 0,
        "false_positive_rate": round(fps / total_comments, 3) if total_comments else 0,
        "comments_per_review": round(total_comments / n, 2) if n else 0,
        "avg_tokens_per_review": round(statistics.mean(tokens)) if tokens else 0,
        "avg_cost_usd_per_review": round(statistics.mean(costs), 4) if costs else None,
        "latency_p50_s": round(percentile(latencies, 0.5), 1),
        "latency_p95_s": round(percentile(latencies, 0.95), 1),
        "errors": sum(1 for r in results if r["error"]),
        "detection_by_category": {k: f"{sum(v)}/{len(v)}" for k, v in sorted(by_category.items())},
    }


def to_markdown(summary: dict, results: list[dict]) -> str:
    cost = summary["avg_cost_usd_per_review"]
    lines = [
        f"# Eval results: {summary['model']}",
        "",
        f"- Cases: {summary['cases']}",
        f"- Detection rate: **{summary['detection_rate']:.0%}**",
        f"- False-positive rate: **{summary['false_positive_rate']:.0%}** "
        f"({summary['comments_per_review']} comments per review)",
        f"- Avg tokens per review: {summary['avg_tokens_per_review']:,}",
        f"- Avg cost per review: {'$%.4f' % cost if cost is not None else 'n/a (set REVIEW_PRICE_PER_MTOK)'}",
        f"- Latency p50 / p95: {summary['latency_p50_s']}s / {summary['latency_p95_s']}s",
        f"- Errors: {summary['errors']}",
        "",
        "| Case | Category | Detected | FP comments | Latency (s) |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        lines.append(f"| {r['id']} | {r['category']} | {'yes' if r['detected'] else 'no'} | "
                     f"{r['false_positive_comments']} | {r['latency_s']} |")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--only", nargs="*", help="Case ids to run")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--min-detection", type=float, default=None,
                        help="Exit non-zero if detection rate falls below this (for CI)")
    args = parser.parse_args()

    cases = [c for c in CASES if not args.only or c.id in args.only][: args.limit]
    client = anthropic.Anthropic()
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        results = list(pool.map(lambda c: run_case(c, client, args.model), cases))

    summary = summarize(results, args.model)
    RESULTS_DIR.mkdir(exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = RESULTS_DIR / f"{stamp}_{args.model}"
    base.with_suffix(".json").write_text(json.dumps({"summary": summary, "results": results}, indent=2))
    base.with_suffix(".md").write_text(to_markdown(summary, results))
    print(json.dumps(summary, indent=2))
    print(f"\nWrote {base}.json and .md")

    if args.min_detection is not None and summary["detection_rate"] < args.min_detection:
        raise SystemExit(f"Detection rate {summary['detection_rate']} below {args.min_detection}")


if __name__ == "__main__":
    main()
