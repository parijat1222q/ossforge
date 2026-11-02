"""Repo policy gate.

Repos whose docs forbid AI-assisted contributions are never targeted. They are
recorded as ``anti_ai`` so the operator can see them in the report; no issue,
comment or PR is ever created for them.

Keyword table adapted from oss-scout's anti-llm-policy (MIT).
"""

from __future__ import annotations

import re
import fnmatch
from dataclasses import dataclass, field

ANTI_AI_KEYWORDS: tuple[str, ...] = (
    "no ai-generated", "no ai generated", "no ai-assisted", "no ai assisted",
    "no llm-generated", "no llm generated", "no copilot-generated",
    "no chatgpt-generated", "human-authored only", "human authored only",
    "human-written only", "human written only", "ai-free contributions",
    "llm-free contributions", "ai-generated code is not allowed",
    "ai-generated code will not be accepted", "do not submit ai-generated",
    "do not submit llm-generated", "do not use ai to", "do not use llms",
    "do not use copilot", "do not use chatgpt", "without ai assistance",
    "without llm assistance", "no use of generative ai", "ban on ai-generated",
    "prohibit ai-generated", "prohibits ai-generated",
    "not be completed by automated agents", "must not be done by automated agents",
    "tasks are for humans only", "do not generate code",
    "no unsupervised agentic tools", "no unsupervised agents",
    "do not use unsupervised agentic tools",
    "explicitly rejected the generative use of agents",
    "you are explicitly forbidden from modifying",
)

POLICY_FILES: tuple[str, ...] = (
    "CONTRIBUTING.md", ".github/CONTRIBUTING.md", "docs/CONTRIBUTING.md",
    "CONTRIBUTING.rst", ".github/CONTRIBUTING.rst", "docs/CONTRIBUTING.rst",
    "docs/contrib.rst", "doc/CONTRIBUTING.md", "doc/CONTRIBUTING.rst", "doc/contrib.rst",
    "doc/PULL_REQUEST_TEMPLATE.md", "doc/pull_request_template.md",
    ".github/PULL_REQUEST_TEMPLATE/*.md", ".github/PULL_REQUEST_TEMPLATE/*.rst",
    ".github/PULL_REQUEST_TEMPLATE/*.txt",
    "PULL_REQUEST_TEMPLATE.md", ".github/PULL_REQUEST_TEMPLATE.md", "docs/PULL_REQUEST_TEMPLATE.md",
    "pull_request_template.md", ".github/pull_request_template.md", "docs/pull_request_template.md",
    "CODE_OF_CONDUCT.md", ".github/CODE_OF_CONDUCT.md",
    "AGENTS.md", "AI_POLICY.md", ".github/AI_POLICY.md", "README.md", "README.rst",
)


@dataclass
class PolicyResult:
    anti_ai: bool
    matched: list[str] = field(default_factory=list)
    source: str | None = None


def scan_text(text: str, source: str | None = None) -> PolicyResult:
    # Markdown emphasis must not hide literal prohibitions such as **no**.
    hay = re.sub(r"[*_`]", "", (text or "").lower())
    hits = [k for k in ANTI_AI_KEYWORDS if k in hay]
    return PolicyResult(bool(hits), hits, source if hits else None)


def scan_files(files: dict[str, str]) -> PolicyResult:
    """``files`` maps repo path -> text. First file with a match wins."""
    for path, text in files.items():
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in POLICY_FILES):
            r = scan_text(text, path)
            if r.anti_ai:
                return r
    return PolicyResult(False)


_AI = re.compile(r"(?:\bai\b|\bllms?\b|copilot|chatgpt|claude|gemini|generative|machine[- ]generated)", re.I)
_VERB = re.compile(r"(?:disclos\w*|declare|indicate|acknowledge|mention|state|label)", re.I)
_ASK = re.compile(r"(?:\bmust\b|\bshould\b|\brequired?\b|\bplease\b|need to|\bask\b|expected|mandatory|\balways\b)", re.I)
_NEG = re.compile(r"(?:no need|not required|not necessary|optional|don'?t need|do not need|need not)", re.I)


@dataclass
class Disclosure:
    """What the repo's own docs say about disclosing AI assistance.

    status: mandatory | waived | ambiguous | none. This is evidence for a human, not a compliance
    certificate: it is a sentence-level keyword reading of the docs, nothing more.
    """
    status: str
    source: str | None = None
    excerpt: str = ""


def _sentences(text: str):
    for sent in re.split(r"[.!?\n]+", text or ""):
        if _AI.search(sent) and _VERB.search(sent):
            yield sent.strip()


def classify_text(text: str) -> tuple[str, str]:
    seen: dict[str, str] = {}
    for sent in _sentences(text):
        kind = "waived" if _NEG.search(sent) else "mandatory" if _ASK.search(sent) else "ambiguous"
        seen.setdefault(kind, sent[:200])
    if "mandatory" in seen and "waived" in seen:
        return "ambiguous", seen["mandatory"] + " || " + seen["waived"]
    for k in ("mandatory", "waived", "ambiguous"):
        if k in seen:
            return k, seen[k]
    return "none", ""


def classify_disclosure(files: dict[str, str]) -> Disclosure:
    """Read every policy-bearing doc (README and AGENTS.md too, as scoped project evidence; their
    wording is only read, never obeyed). Conflicting documents are ambiguous."""
    found: dict[str, tuple[str, str]] = {}
    for path, text in files.items():
        if any(fnmatch.fnmatchcase(path, pattern) for pattern in POLICY_FILES):
            st, ex = classify_text(text)
            if st != "none":
                found[path] = (st, ex)
    kinds = {st for st, _ in found.values()}
    if not found:
        return Disclosure("none")
    if "mandatory" in kinds and kinds != {"mandatory"}:
        status = "ambiguous"
    elif "mandatory" in kinds:
        status = "mandatory"
    elif "ambiguous" in kinds:
        status = "ambiguous"
    else:
        status = "waived"
    pick = next((p for p, (st, _) in found.items() if st == status), None) or next(iter(found))
    return Disclosure(status, pick, found[pick][1])


class NeedsReview(Exception):
    """Policy text is unclear or conflicts with the configured setting: a human must decide."""


def decide_disclosure(setting, files: dict[str, str]) -> tuple[bool, Disclosure]:
    """Return (add_note, evidence). ``setting`` is "auto" (default), True ("always") or False ("never").

    - mandatory policy: the note is added; setting False conflicts and raises NeedsReview.
    - ambiguous or conflicting policy: auto raises NeedsReview; an explicit True/False is the human's call.
    - waived or silent policy: note only if setting is True.
    """
    d = classify_disclosure(files)
    auto = isinstance(setting, str) and setting.strip().lower() == "auto"
    always = setting is True or (isinstance(setting, str) and setting.strip().lower() in ("true", "yes", "always", "on"))
    if d.status == "mandatory":
        if not auto and not always:
            raise NeedsReview(f"{d.source} asks for AI disclosure ({d.excerpt!r}) but disclosure is set to false")
        return True, d
    if d.status == "ambiguous":
        if auto:
            raise NeedsReview(f"unclear AI-disclosure wording in {d.source}: {d.excerpt!r}. Read it, then set "
                              "disclosure: true or false in ossforge.yaml")
        return always, d
    return always, d


_AI_DOC_NAME = re.compile(r"ai[_-]?polic|llm[_-]?polic|ai[_-]?usage|generative", re.I)
_AI_DOC_TEXT = re.compile(r"(?:polic\w+ on (?:the )?use of (?:ai|llm)|ai tools? polic|generative ai|llm polic|ai[- ]assisted contribution)", re.I)


def ai_policy_docs(files: dict[str, str]) -> list[str]:
    """Docs that look like an AI/LLM contribution policy (by file name or heading text). Keyword reading only:
    a hit means a human must read it, a miss does not mean the repo has no rules."""
    return sorted(n for n, t in files.items() if _AI_DOC_NAME.search(n.rsplit("/", 1)[-1])
                  or _AI_DOC_TEXT.search((t or "")[:6000]))
