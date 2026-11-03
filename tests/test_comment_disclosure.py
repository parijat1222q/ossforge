"""Comment publication stand-ins: no network or real GitHub writes."""
from types import SimpleNamespace

import pytest

from ossforge import comment, safety
from ossforge.config import Config


@pytest.mark.parametrize('setting,docs,expected', [
    (True, {}, True),
    ('auto', {'AI_POLICY.md': 'You must disclose AI use.'}, True),
    ('auto', {}, False),
    (False, {}, False),
])
@pytest.mark.parametrize('execute', [False, True])
def test_comment_disclosure_never_invents_human_review(tmp_path, monkeypatch, setting, docs, expected, execute):
    cfg = Config(state_dir=str(tmp_path), kill_file=str(tmp_path / 'STOP'), disclosure=setting)
    monkeypatch.setattr(comment.identity, 'check_login', lambda *a: [])
    monkeypatch.setattr(comment.vet, 'policy_texts', lambda *a: docs)
    monkeypatch.setattr(comment.policy_review, 'check', lambda *a: '')
    monkeypatch.setattr(comment.gh, 'pace_write', lambda: None)
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return SimpleNamespace(returncode=0, stdout='local fixture result', stderr='')

    monkeypatch.setattr(comment.subprocess, 'run', run)
    body = 'The proposed fix still fails for an empty input.'
    result = comment.post(cfg, safety.Audit(tmp_path / 'audit.jsonl'), 'org/repo', 7, body, execute)
    if execute:
        assert len(calls) == 1
        text = calls[0][calls[0].index('-b') + 1]
    else:
        assert not calls
        text = result.removeprefix('dry-run:\n')
    assert text.startswith(body)
    assert 'checked by a human' not in text
    assert ('This review was prepared with AI assistance.' in text) is expected


def test_comment_mandatory_disclosure_false_stops_before_write(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path), kill_file=str(tmp_path / 'STOP'), disclosure=False)
    monkeypatch.setattr(comment.identity, 'check_login', lambda *a: [])
    monkeypatch.setattr(comment.vet, 'policy_texts', lambda *a: {'AI_POLICY.md': 'You must disclose AI use.'})
    monkeypatch.setattr(comment.policy_review, 'check', lambda *a: '')
    monkeypatch.setattr(comment.subprocess, 'run', lambda *a, **kw: pytest.fail('no write on policy conflict'))
    with pytest.raises(comment.Refused, match='asks for AI disclosure'):
        comment.post(cfg, safety.Audit(tmp_path / 'audit.jsonl'), 'org/repo', 7, 'Concrete review.', True)
