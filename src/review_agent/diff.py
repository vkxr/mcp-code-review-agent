"""Minimal unified-diff parser.

GitHub only accepts inline review comments on lines that appear in a diff hunk
(on the RIGHT side, i.e. the new version of the file). This module works out
which lines those are, so the agent can decide whether a finding becomes an
inline comment or goes into the review body.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


@dataclass
class FileDiff:
    path: str
    added_lines: set[int] = field(default_factory=set)
    hunk_lines: set[int] = field(default_factory=set)  # added + context lines on the RIGHT side
    is_deleted: bool = False


def parse_unified_diff(diff: str) -> dict[str, FileDiff]:
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    new_line = 0

    for raw in diff.splitlines():
        if raw.startswith("diff --git "):
            current = None
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            if target == "/dev/null":
                # File deleted: nothing to comment on in the new version.
                current = FileDiff(path="/dev/null", is_deleted=True)
                continue
            path = target[2:] if target.startswith("b/") else target
            current = files.setdefault(path, FileDiff(path=path))
            continue
        match = HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            continue
        if current is None or current.is_deleted:
            continue
        if raw.startswith("+"):
            current.added_lines.add(new_line)
            current.hunk_lines.add(new_line)
            new_line += 1
        elif raw.startswith("-"):
            pass  # removed line: exists only on the LEFT side
        elif raw.startswith("\\"):
            pass  # "\ No newline at end of file"
        else:
            current.hunk_lines.add(new_line)
            new_line += 1
    return files


def number_diff_lines(diff: str) -> str:
    """Prefix each added/context line with its new-file line number.

    LLMs are unreliable at counting lines inside a hunk. Giving them explicit
    numbers makes the line numbers in their findings far more accurate.
    """
    out: list[str] = []
    new_line = 0
    in_hunk = False
    for raw in diff.splitlines():
        match = HUNK_RE.match(raw)
        if match:
            new_line = int(match.group(1))
            in_hunk = True
            out.append(raw)
            continue
        if raw.startswith(("diff --git ", "--- ", "+++ ", "index ")):
            in_hunk = False
            out.append(raw)
            continue
        if not in_hunk:
            out.append(raw)
        elif raw.startswith("-"):
            out.append(f"{'':>5} {raw}")
        elif raw.startswith("\\"):
            out.append(raw)
        else:
            out.append(f"{new_line:>5} {raw}")
            new_line += 1
    return "\n".join(out)
