"""Merge-likelihood score for an open PR (0-100), ported from GitSense predictor."""

from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass
class PRSignals:
    review_decision: str | None
    is_draft: bool
    conflicting: bool
    ci_failing: bool
    changed_files: int
    additions: int
    touches_tests: bool
    age_days: float | None


@dataclass
class PRPrediction:
    score: int
    label: str
    notes: list[str] = field(default_factory=list)


def score_pr(s: PRSignals) -> PRPrediction:
    score, notes = 50, []
    d = (s.review_decision or "").upper()
    if d == "APPROVED":
        score += 28
        notes.append("already approved")
    elif d == "CHANGES_REQUESTED":
        score -= 25
        notes.append("changes requested")
    else:
        notes.append("no review decision yet")
    if s.is_draft:
        score -= 18
        notes.append("still a draft")
    if s.conflicting:
        score -= 18
        notes.append("has merge conflicts")
    if s.ci_failing:
        score -= 20
        notes.append("CI failing")
    else:
        score += 8
    if s.changed_files <= 3 and s.additions <= 100:
        score += 15
        notes.append("small, focused diff")
    elif s.changed_files >= 30 or s.additions >= 1000:
        score -= 15
        notes.append("large diff")
    if s.touches_tests:
        score += 10
        notes.append("includes tests")
    else:
        notes.append("no test changes")
    if s.age_days is not None and s.age_days > 60:
        score -= 12
        notes.append("open 60+ days, may be stalled")
    score = max(0, min(100, score))
    label = "likely" if score >= 70 else "uncertain" if score >= 45 else "unlikely"
    return PRPrediction(score, label, notes)


def is_test_path(path: str) -> bool:
    """Recognize test directories/naming boundaries, not production substrings.

    A convention hint, not proof that a file contains tests. Native checks and
    operator review still decide whether the regression is meaningful.
    """
    parts = path.replace("\\", "/").split("/")
    markers = {"test", "tests", "spec", "specs", "__tests__"}
    if any(part.lower() in markers for part in parts[:-1]):
        return True
    name = parts[-1]
    if any(token in markers for token in re.split(r"[._-]", name.lower())):
        return True
    stem = name.rsplit(".", 1)[0]
    return bool(re.search(r"(?:Test|Tests|Spec|Specs)$", stem)
                or re.match(r"^(?:test|spec)[A-Z]", stem))
