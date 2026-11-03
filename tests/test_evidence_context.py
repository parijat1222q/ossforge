from pathlib import Path

from ossforge import evidence, submit
from ossforge.config import Config
from ossforge.queue import Item


def test_submission_forwards_configured_context(monkeypatch, tmp_path):
    cfg = Config(state_dir=str(tmp_path / 'selected-state'))
    item = Item('i', 'org/repo', 'x.py', 1, 'bug', 'candidate', clone=str(tmp_path), branch='fix')
    seen = []
    monkeypatch.setattr(submit, '_item_base', lambda _: 'base')
    monkeypatch.setattr(evidence, 'check', lambda *a, **kw: seen.append(kw['cfg']))
    submit._evidence_gate(item, cfg)
    assert seen == [cfg]
    assert seen[0].path('x') == tmp_path / 'selected-state' / 'x'


def test_ordinary_receipt_unchanged_with_or_without_context(monkeypatch, tmp_path):
    rec = {'schema': 1, 'base': 'base', 'tree': 'tree', 'branch': 'fix', 'commands': [['test']]}
    monkeypatch.setattr(evidence, 'capture', lambda *a, **kw: rec)
    evidence.check(Path(tmp_path), 'base', 'fix', rec)
    evidence.check(Path(tmp_path), 'base', 'fix', rec, cfg=Config(state_dir=str(tmp_path/'state')))
