"""Thin GitHub access through the authenticated `gh` CLI (no token handling here).

Adds a disk TTL cache for GETs and a sliding-window budget for the search API
(9 req/min), the same idea as oss-scout's http-cache and search-budget.
"""

from __future__ import annotations

import base64
import contextlib
from . import locking
import fnmatch
import hashlib
import json
import math
import os
import subprocess
import time
from pathlib import Path
from typing import Any

_CACHE_DIR: Path | None = None
_TTL = 900
_READ_DEADLINE: float | None = None


class ReadBudgetExceeded(RuntimeError):
    pass


def read_remaining() -> float:
    if _READ_DEADLINE is None:
        return float("inf")
    remaining = _READ_DEADLINE - time.monotonic()
    if remaining <= 0:
        raise ReadBudgetExceeded("GitHub read budget exhausted; partial results are not a completed search")
    return remaining


@contextlib.contextmanager
def read_budget(seconds: float):
    global _READ_DEADLINE
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("read budget must be finite and positive")
    previous = _READ_DEADLINE
    _READ_DEADLINE = min(previous or float("inf"), time.monotonic() + seconds)
    try:
        yield
    finally:
        _READ_DEADLINE = previous


def _sleep(seconds: float):
    remaining = read_remaining()
    if seconds >= remaining:
        raise ReadBudgetExceeded("GitHub read budget cannot cover the required pacing/cooldown wait")
    time.sleep(seconds)


SEARCH_PER_MIN = 9  # well under GitHub's secondary limit; bursts of searches look like scraping


class GhError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None, retry_after: float = 0.0):
        super().__init__(msg)
        self.status = status
        self.retry_after = retry_after


class RateLimited(GhError):
    """Primary or secondary GitHub rate limit. Callers stop the pass instead of retrying."""


PACING_S = 0.4  # gap after every uncached call; keeps bursts under the secondary limit


MAX_RETRIES = 2          # in-call retries after a rate-limit response
MAX_INLINE_WAIT = 90.0   # longer waits stop the pass instead of sleeping
MIN_SECONDARY_WAIT = 60.0  # GitHub: with no Retry-After on a secondary limit, wait at least a minute
WRITE_GAP_S = 1.0        # GitHub asks for >= 1s between mutating requests
_COOLDOWN_FILE: Path | None = None


def split_response(out: str) -> tuple[dict[str, str], str]:
    """Split `gh api -i` output into lower-cased headers (plus ``status``) and body."""
    if not out.startswith("HTTP/"):
        return {}, out
    head, _, body = out.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    h = {"status": (lines[0].split() + ["", ""])[1]}
    for ln in lines[1:]:
        k, _, v = ln.partition(":")
        h[k.strip().lower()] = v.strip()
    return h, body


def status_of(headers: dict[str, str], err: str) -> int | None:
    if headers.get("status", "").isdigit():
        return int(headers["status"])
    for code in (429, 403, 404):
        if str(code) in err:
            return code
    return None


def is_rate_limited(status: int | None, headers: dict[str, str], err: str) -> bool:
    low = err.lower()
    if status == 429:
        return True
    if status == 403 and (headers.get("retry-after") or headers.get("x-ratelimit-remaining") == "0"):
        return True
    return "rate limit" in low or "abuse detection" in low


def retry_wait(headers: dict[str, str], attempt: int = 0, now: float | None = None) -> float:
    """Seconds to wait, following GitHub's documented rules: Retry-After first, then the
    reset time when the quota is exhausted, else at least a minute with exponential backoff."""
    ra = headers.get("retry-after", "")
    if ra.isdigit():
        return float(ra) + 1
    if headers.get("x-ratelimit-remaining") == "0" and headers.get("x-ratelimit-reset", "").isdigit():
        return max(1.0, float(headers["x-ratelimit-reset"]) - (now or time.time())) + 1
    return MIN_SECONDARY_WAIT * (2 ** attempt)


def _state_dir() -> Path | None:
    return _COOLDOWN_FILE.parent if _COOLDOWN_FILE else None


@contextlib.contextmanager
def _shared_lock():
    """Cross-process lock so several ossforge runs share one budget. No state dir = in-process only."""
    d = _state_dir()
    if d is None:
        yield None
        return
    d.mkdir(parents=True, exist_ok=True)
    with (d / "ratelimit.lock").open("a+") as fh:
        locking.acquire(fh, blocking=True)
        try:
            yield d
        finally:
            locking.release(fh)


def _load_gates(d: Path | None) -> dict:
    if d is None:
        return _MEM_GATES
    try:
        return json.loads((d / "gates.json").read_text())
    except (OSError, ValueError):
        return {}


def _save_gates(d: Path | None, g: dict) -> None:
    if d is None:
        return
    tmp = d / f"gates.json.{os.getpid()}.tmp"
    tmp.write_text(json.dumps(g))
    os.replace(tmp, d / "gates.json")


_MEM_GATES: dict = {}


def _guard_cooldown() -> None:
    if not _COOLDOWN_FILE or not _COOLDOWN_FILE.exists():
        return
    try:
        until = float(_COOLDOWN_FILE.read_text())
    except (OSError, ValueError):
        until = time.time() + MIN_SECONDARY_WAIT  # unreadable marker: fail closed for a minute
    if until > time.time():
        raise RateLimited(f"rate-limit cooldown active, ~{int(until - time.time())}s left", 429, until - time.time())


def _set_cooldown(wait: float) -> None:
    if not _COOLDOWN_FILE:
        return
    with _shared_lock():
        until = time.time() + wait
        try:  # never shorten a cooldown another process already set
            until = max(until, float(_COOLDOWN_FILE.read_text()))
        except (OSError, ValueError):
            pass
        tmp = _COOLDOWN_FILE.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(str(until))
        os.replace(tmp, _COOLDOWN_FILE)


def pace_write() -> float:
    """Call before every mutating request (issue, fork, push, PR, comment). Shared across processes.

    Returns the reservation timestamp recorded under the lock; consecutive reservations are >= WRITE_GAP_S apart."""
    _guard_cooldown()
    with _shared_lock() as d:
        _guard_cooldown()
        g = _load_gates(d)
        gap = WRITE_GAP_S - (time.time() - g.get("last_write", 0.0))
        if gap > 0:
            _sleep(gap)
        _guard_cooldown()
        g["last_write"] = reserved = time.time()
        _save_gates(d, g)
    return reserved


def configure(cache_dir: str | Path | None, ttl: int = 900) -> None:
    global _CACHE_DIR, _TTL, _COOLDOWN_FILE
    _CACHE_DIR = Path(cache_dir) if cache_dir else None
    _COOLDOWN_FILE = _CACHE_DIR.parent / "ratelimit_until" if _CACHE_DIR else None
    _TTL = ttl


def _budget(path: str) -> None:
    if not path.startswith("search/"):
        return
    with _shared_lock() as d:
        g = _load_gates(d)
        now = time.time()
        times = [t for t in g.get("search", []) if now - t <= 60]
        if len(times) >= SEARCH_PER_MIN:
            _sleep(max(0.0, 60 - (now - times[0])) + 0.2)
            now = time.time()
            times = [t for t in times if now - t <= 60]
        times.append(time.time())
        g["search"] = times
        _save_gates(d, g)


def _cache_path(path: str, params: dict) -> Path | None:
    if _CACHE_DIR is None:
        return None
    key = hashlib.sha1(json.dumps([path, sorted(params.items())]).encode()).hexdigest()
    return _CACHE_DIR / f"{key}.json"


def api(path: str, /, *, fresh: bool = False, **params: Any) -> Any:
    read_remaining()
    cp = _cache_path(path, params)
    if cp and not fresh and cp.exists() and time.time() - cp.stat().st_mtime < _TTL:
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except ValueError:
            pass
    _guard_cooldown()
    cmd = ["gh", "api", "-i", "-X", "GET", path]
    for k, v in params.items():
        cmd += ["-f", f"{k}={v}"]
    for attempt in range(MAX_RETRIES + 1):
        _guard_cooldown()
        _budget(path)
        _guard_cooldown()
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", timeout=min(120, read_remaining()))
        except subprocess.TimeoutExpired as exc:
            read_remaining()
            raise GhError("GitHub API read timed out after 120 seconds; retry later") from exc
        headers, body = split_response(p.stdout)
        if p.returncode == 0:
            _sleep(PACING_S)
            data = json.loads(body) if body.strip() else None
            break
        err = (p.stderr.strip() + "\n" + body[:300]).strip()
        status = status_of(headers, err)
        if is_rate_limited(status, headers, err):
            wait = retry_wait(headers, attempt)
            _set_cooldown(wait)  # publish immediately, including when inline sleep/budget is interrupted
            if attempt < MAX_RETRIES and wait <= MAX_INLINE_WAIT:
                _sleep(wait)  # documented behaviour: honour Retry-After / reset, then try again
                continue
            raise RateLimited(f"{err.splitlines()[0] if err else 'rate limited'} (retry in ~{int(wait)}s)", status, wait)
        _sleep(PACING_S)
        raise GhError(err or "gh api failed", status)
    if cp and data is not None:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(data), encoding="utf-8")
    return data


def search_issues(q: str, per_page: int = 30, sort: str = "updated", fresh: bool = False, require_complete: bool = False) -> list[dict]:
    result = api("search/issues", fresh=fresh, q=q, per_page=per_page, sort=sort, order="desc")
    if require_complete and ("total_count" not in result or result.get("incomplete_results") or result["total_count"] > len(result["items"])):
        raise GhError("search coverage incomplete; narrow and review before publication")
    return result["items"]


def search_page(q: str, per_page: int = 30, sort: str = "updated") -> dict:
    """One search call returning both ``total_count`` and ``items`` (saves a separate count query)."""
    return api("search/issues", q=q, per_page=per_page, sort=sort, order="desc")


def search_count(q: str) -> int:
    return api("search/issues", q=q, per_page=1)["total_count"]


def search_repositories(q: str, per_page: int = 30, sort: str = "stars", fresh: bool = False, page: int = 1) -> list[dict]:
    result = api("search/repositories", fresh=fresh, q=q, per_page=per_page, sort=sort, order="desc", page=page)
    if not isinstance(result, dict):
        return []
    return result.get("items", [])


def file_text_at(repo: str, path: str, ref: str) -> str | None:
    return file_text(repo, path, ref)


def cache_get(key: str, ttl: int) -> Any | None:
    """Generic derived-value cache (e.g. a repo's health score) with its own TTL."""
    cp = _cache_path("derived:" + key, {})
    if cp and cp.exists() and time.time() - cp.stat().st_mtime < ttl:
        try:
            return json.loads(cp.read_text(encoding="utf-8"))
        except ValueError:
            return None
    return None


def cache_put(key: str, value: Any) -> None:
    cp = _cache_path("derived:" + key, {})
    if cp:
        cp.parent.mkdir(parents=True, exist_ok=True)
        cp.write_text(json.dumps(value), encoding="utf-8")


def existing_files(repo: str, paths: tuple[str, ...], *, fresh: bool = False, strict: bool = False, ref: str | None = None) -> list[str]:
    """Which of `paths` exist, using one directory listing per parent (3 calls) instead of one probe per path."""
    parents = {p.rsplit("/", 1)[0] if "/" in p else "" for p in paths}
    present: set[str] = set()
    for d in parents:
        try:
            listing = api(f"repos/{repo}/contents/{d}".rstrip("/"), fresh=fresh, **({"ref": ref} if ref is not None else {}))
        except RateLimited:
            raise
        except GhError as exc:
            if strict and (exc.status != 404 or not d):
                raise
            continue
        if d and isinstance(listing, dict) and listing.get("type") == "file" and "path" in listing:
            present.add(listing["path"])
            continue
        if strict and (not isinstance(listing, list) or len(listing) >= 1000 or any(not isinstance(x, dict) or "path" not in x for x in listing)):
            raise GhError("policy directory listing unavailable")
        if isinstance(listing, list):
            present.update(x["path"] for x in listing if x.get("type") == "file")
    return sorted(p for p in present if any(fnmatch.fnmatchcase(p, pattern) for pattern in paths))


_MISSING: set[tuple[str, str, str | None]] = set()


def file_text(repo: str, path: str, ref: str | None = None, *, fresh: bool = False) -> str | None:
    if not fresh and (repo, path, ref) in _MISSING:
        return None
    try:
        d = api(f"repos/{repo}/contents/{path}", fresh=fresh, **({"ref": ref} if ref else {}))
    except GhError as e:
        if e.status == 404 and not isinstance(e, RateLimited):
            _MISSING.add((repo, path, ref))
            return None
        raise
    if isinstance(d, dict) and d.get("encoding") == "base64":
        return base64.b64decode(d["content"]).decode("utf-8", "replace")
    return None
