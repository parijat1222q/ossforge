"""Local run journal. Recovery means explicit interrupted status, not replayed writes."""
from __future__ import annotations

from . import locking
import json
import re
import time
import uuid
from pathlib import Path


class Busy(RuntimeError):
    pass


class Run:
    def __init__(self, root: Path, emit=None, before_journal=None):
        self.root, self.emit = Path(root), emit
        self.before_journal = before_journal
        self.lock = None
        self.data = {}

    def __enter__(self):
        if any(p.is_symlink() for p in (self.root / 'run.lock', *self.root.parents)) or self.root.is_symlink():
            raise Busy('symlink state lock path; preserve it')
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = (self.root / 'run.lock').open('a')
        try:
            locking.acquire(self.lock)
        except BlockingIOError as exc:
            self.lock.close()
            raise Busy('another run holds this state directory') from exc
        try:
            if self.before_journal:
                self.before_journal()
        except BaseException:
            locking.release(self.lock)
            self.lock.close()
            raise
        try:
            old = json.loads((self.root / 'run.json').read_text())
        except (OSError, ValueError):
            old = {}
        if isinstance(old, dict) and old.get('status') == 'running':
            old.update(status='interrupted', ended_at=time.time())
            (self.root / 'runs').mkdir(exist_ok=True)
            self._write(self.root / 'runs' / f'{old.get("id") if re.fullmatch(r"[A-Za-z0-9_-]{1,80}", str(old.get("id", ""))) else uuid.uuid4().hex}.json', old)
        self.data = {'schema': 1, 'id': uuid.uuid4().hex, 'status': 'running',
                     'started_at': time.time(), 'events': []}
        self.note('run started')
        return self

    @staticmethod
    def _write(path, value):
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
        temp.replace(path)

    def note(self, text):
        self.data['events'].append({'at': time.time(), 'message': text})
        self._write(self.root / 'run.json', self.data)
        if self.emit:
            self.emit(text)

    def __exit__(self, kind, value, traceback):
        try:
            self.data.update(status='failed' if kind else self.data.get('outcome', 'completed'), ended_at=time.time())
            if kind:
                self.note(f'failed: {kind.__name__}')
            self._write(self.root / 'run.json', self.data)
            (self.root / 'runs').mkdir(exist_ok=True)
            self._write(self.root / 'runs' / f'{self.data["id"]}.json', self.data)
        finally:
            locking.release(self.lock)
            self.lock.close()
        return False
