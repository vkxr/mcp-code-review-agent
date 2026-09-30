"""Structured outputs the agents must return (enforced via a `submit_result` tool)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["critical", "high", "medium", "low"]


class FileRisk(BaseModel):
    path: str
    risk: Literal["high", "medium", "low"]
    reason: str = Field(description="One sentence on why this file needs (or doesn't need) close review")


class Plan(BaseModel):
    files: list[FileRisk] = Field(description="Every changed file, ranked by review risk")
    focus_areas: list[str] = Field(
        description="Specific things reviewers should check, e.g. 'SQL built from request params in db.py'"
    )


class Finding(BaseModel):
    path: str = Field(description="File path exactly as shown in the diff")
    line: int = Field(description="Line number in the NEW version of the file (the numbers shown in the diff)")
    severity: Severity
    category: str = Field(description="e.g. security, correctness, concurrency, performance, reliability")
    title: str = Field(description="Short headline for the issue")
    explanation: str = Field(description="What is wrong and what can go wrong at runtime")
    suggestion: str | None = Field(default=None, description="Concrete fix")


class Findings(BaseModel):
    findings: list[Finding] = Field(description="Real issues only. Return an empty list if there are none.")


class TestTriage(BaseModel):
    summary: str = Field(description="What failed and the most likely cause")
    findings: list[Finding] = Field(default_factory=list)


class ReviewSummary(BaseModel):
    summary: str = Field(description="2-4 sentence overview of the pull request and its main risks")
    comments: list[Finding] = Field(
        description="Final, de-duplicated review comments. Drop anything speculative or unsupported."
    )
