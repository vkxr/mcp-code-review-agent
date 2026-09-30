"""Test runner tool.

Running a pull request's tests means executing code from that pull request.
Only point this at checkouts inside REVIEW_WORKDIR, and run the agent inside a
container (see Dockerfile) so untrusted code can't reach the host.
"""

from __future__ import annotations

import os
import subprocess
import sys

MAX_OUTPUT_CHARS = 8_000


def allowed_root() -> str:
    return os.path.realpath(os.environ.get("REVIEW_WORKDIR", "/tmp/review-workspaces"))


def run_tests(repo_path: str, test_path: str | None = None, timeout: int = 180) -> dict:
    """Run pytest in `repo_path` and return exit code plus (truncated) output."""
    root = allowed_root()
    repo = os.path.realpath(repo_path)
    if not (repo == root or repo.startswith(root + os.sep)):
        return {"status": "refused", "exit_code": None,
                "output": f"repo_path must be inside REVIEW_WORKDIR ({root})"}
    if not os.path.isdir(repo):
        return {"status": "error", "exit_code": None, "output": f"{repo_path} is not a directory"}

    cmd = [sys.executable, "-m", "pytest", "-q", "--no-header", "-rf"]
    if test_path:
        target = os.path.realpath(os.path.join(repo, test_path))
        if not (target == repo or target.startswith(repo + os.sep)):
            return {"status": "refused", "exit_code": None, "output": "test_path escapes repo_path"}
        cmd.append(target)
    try:
        proc = subprocess.run(cmd, cwd=repo, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "exit_code": None, "output": f"Tests exceeded {timeout}s"}

    output = (proc.stdout + proc.stderr)[-MAX_OUTPUT_CHARS:]
    # pytest exit code 5 = no tests collected
    status = {0: "passed", 5: "no_tests"}.get(proc.returncode, "failed")
    return {"status": status, "exit_code": proc.returncode, "output": output}
