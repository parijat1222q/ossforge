"""Persistent work queue. Status flow:
found -> clean (no duplicates) -> verified (confirmed by reading + failing test + patch) -> submitted
Also: duplicate, rejected, anti_ai.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

STATUSES = {"found", "clean", "duplicate", "verified", "submitted", "rejected", "anti_ai", "held"}


@dataclass
class Item:
    id: str
    repo: str
    path: str
    line: int
    kind: str
    detail: str
    status: str = "found"
    notes: list[str] = field(default_factory=list)
    clone: str = ""
    branch: str = ""
    issue_title: str = ""
    issue_body: str = ""
    pr_title: str = ""
    pr_body: str = ""
    source_issue: int = 0  # existing bug issue; validate live and reuse, never recreate
    consent_issue: int = 0  # existing upstream issue with explicit maintainer approval label
    verification: dict = field(default_factory=dict)  # exact tested tree/base/branch and commands
    submitted: dict = field(default_factory=dict)  # partial progress lives here (issue_url, fork_pushed, pr_url)
    work: str = "fix"  # fix | feature
    rank: float = 0.0
    hold_reason: str = ""  # unresolved clean work, not a detector false positive
    research_id: str = ""  # exact originating repo investigation, not clone-path identity
    contribution_base: dict | None = None  # exact reviewed repo/branch/revision; empty legacy only
    snippet: str = ""  # a few lines around the finding so a human can accept or reject it quickly


class InvalidState(ValueError):
    """Existing queue cannot be trusted; preserve it for recovery, never erase it."""


class Queue:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.items: dict[str, Item] = {}
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        except (OSError, UnicodeError) as exc:
            raise InvalidState("queue state unreadable; preserve it and recover before continuing") from exc
        try:
            rows = json.loads(raw)
            if not isinstance(rows, list):
                raise ValueError("queue must be a list")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("queue entries must be objects")
                if "contribution_base" in row:
                    from . import contribution_base
                    contribution_base.validate(row["contribution_base"], row.get("repo"), explicit=True)
                item = Item(**row)
                if not isinstance(item.id, str) or not item.id or item.id in self.items or item.status not in STATUSES:
                    raise ValueError("invalid or repeated item ID/status")
                self.items[item.id] = item
        except (ValueError, TypeError) as exc:
            raise InvalidState("queue state invalid; preserve it and recover before continuing") from exc

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent,
                                             prefix=self.path.name + ".", delete=False) as out:
                temp = Path(out.name)
                json.dump([{k: v for k, v in asdict(i).items() if k != "contribution_base" or v is not None} for i in self.items.values()], out, indent=1)
                out.flush()
                os.fsync(out.fileno())
            temp.replace(self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)

    def add(self, item: Item) -> bool:
        if item.id in self.items:
            return False
        self.items[item.id] = item
        return True

    def by_status(self, status: str) -> list[Item]:
        return [i for i in self.items.values() if i.status == status]

    def set(self, id_: str, **kw) -> Item:
        it = self.items[id_]
        for k, v in kw.items():
            setattr(it, k, v)
        return it


def item_id(repo: str, path: str, kind: str, line: int) -> str:
    return f"{repo}:{path}:{kind}:{line}"
