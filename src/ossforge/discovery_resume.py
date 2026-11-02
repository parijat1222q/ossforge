"""Short-lived positions in partial read batches, never saved vetting decisions."""
from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from . import issue_reviews

TTL_SECONDS = 3600
MAX_BATCHES = 20
MAX_ITEMS = 100


def _key(query: str, limit: int) -> str:
    return hashlib.sha256(json.dumps([query, limit]).encode()).hexdigest()


def _active(path: Path) -> dict:
    now = time.time()
    return {k: v for k, v in issue_reviews.load(path).items()
            if isinstance(v, dict) and type(v.get('saved_at')) in (int, float)
            and 0 <= now - v['saved_at'] < TTL_SECONDS}


def load(path: Path, query: str, limit: int) -> list[dict] | None:
    batch = _active(path).get(_key(query, limit), {}).get('remaining')
    if not isinstance(batch, list) or not batch or len(batch) > MAX_ITEMS:
        return None
    if any(not isinstance(it, dict) or set(it) != {'repo', 'number'}
           or not isinstance(it['repo'], str) or it['repo'].count('/') != 1
           or any(not part or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-' for c in part)
                  for part in it['repo'].split('/'))
           or type(it['number']) is not int or it['number'] <= 0 for it in batch):
        return None
    return batch


def save(path: Path, query: str, limit: int, remaining: list[dict]) -> None:
    data = _active(path)
    key = _key(query, limit)
    data.pop(key, None)
    if remaining:
        data[key] = {'saved_at': time.time(), 'remaining': remaining[:MAX_ITEMS]}
    data = dict(sorted(data.items(), key=lambda p: p[1]['saved_at'])[-MAX_BATCHES:])
    issue_reviews._save(path, data)
