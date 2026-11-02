import pytest
from ossforge import loop, scout
from ossforge.config import Config, load


def test_fix_only_mode_does_not_spend_reads_on_feature_search(tmp_path, monkeypatch):
    cfg = load_config(tmp_path, 'fix')
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.time, 'time', lambda: 86400)
    def bugs(*a, **k):
        scout.LAST_FUNNEL.clear()
        return []
    monkeypatch.setattr(scout, 'scout', bugs)
    monkeypatch.setattr(scout, 'scout_features', lambda *a, **k: pytest.fail('fix-only must not search features'))
    notes = loop.run_once(cfg)
    assert not any('feature funnel' in note for note in notes)


def load_config(tmp_path, mode):
    path = tmp_path/'config.yaml'
    path.write_text(f'work_kind: {mode}\nstate_dir: {tmp_path}/state\nkill_file: {tmp_path}/STOP\n')
    return load(path)


def test_default_keeps_both_modes(tmp_path, monkeypatch):
    cfg = Config(state_dir=str(tmp_path/'state'), kill_file=str(tmp_path/'STOP'))
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: [])
    monkeypatch.setattr(loop.time, 'time', lambda: 86400)
    calls = []
    def search(name):
        def go(*a, **k):
            calls.append(name)
            scout.LAST_FUNNEL.clear()
            return []
        return go
    monkeypatch.setattr(scout, 'scout', search('bugs'))
    monkeypatch.setattr(scout, 'scout_features', search('features'))
    loop.run_once(cfg)
    assert calls == ['bugs', 'features']


def test_unknown_work_kind_rejected_before_network(tmp_path, monkeypatch):
    cfg = load_config(tmp_path, 'mystery')
    monkeypatch.setattr(loop.patrol, 'patrol', lambda *a: pytest.fail('invalid mode must stop first'))
    with pytest.raises(ValueError, match='work_kind'):
        loop.run_once(cfg)
