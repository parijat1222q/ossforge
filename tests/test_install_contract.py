"""Keep normal-user tools independent of the developer evaluation suite."""
from pathlib import Path

from ossforge import doctor
from ossforge.config import Config


def test_normal_tools_extra_and_developer_extra_are_distinct():
    # Read project metadata as text to keep this test runnable on Python 3.10.
    text = (Path(__file__).resolve().parents[1] / 'pyproject.toml').read_text()
    assert 'tools = ["ruff>=0.5"]' in text
    assert 'dev = ["pytest>=8", "ruff>=0.5"]' in text
    assert 'dependencies = ["httpx>=0.27", "pyyaml>=6"]' in text


def test_missing_ruff_points_normal_users_to_tools_extra(monkeypatch):
    monkeypatch.setattr(doctor, '_out', lambda cmd: (1, 'No module named ruff') if 'ruff' in cmd else (0, 'configured'))
    row = next(row for row in doctor.check(Config()) if row[0].startswith('ruff'))
    assert not row[1]
    assert 'ossforge[tools]' in row[2]
    assert 'dev dependencies' not in row[2]
