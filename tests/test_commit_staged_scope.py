import subprocess
import pytest
from ossforge import identity
from ossforge.config import Config


def test_explicit_commit_refuses_unrelated_staged_file(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Owner')
    git('config', 'user.email', 'owner@example.invalid')
    (tmp_path/'source.py').write_text('value = 1\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    (tmp_path/'source.py').write_text('value = 2\n')
    (tmp_path/'unrelated.txt').write_text('unrelated staged work\n')
    git('add', 'unrelated.txt')
    monkeypatch.setattr(identity, 'check_login', lambda cfg: [])
    cfg = Config(github_user='owner', git_name='Owner', git_email='owner@example.invalid')
    with pytest.raises(identity.IdentityError, match='staged'):
        identity.commit(cfg, str(tmp_path), 'fix: source', ['source.py'])
    assert git('rev-parse', 'HEAD') == base
    assert git('diff', '--cached', '--name-only') == 'unrelated.txt'


def test_selected_staged_path_is_allowed(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, capture_output=True, text=True, check=True).stdout.strip()
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Owner')
    git('config', 'user.email', 'owner@example.invalid')
    (tmp_path/'source.py').write_text('value = 1\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    (tmp_path/'source.py').write_text('value = 2\n')
    git('add', 'source.py')
    monkeypatch.setattr(identity, 'check_login', lambda cfg: [])
    cfg = Config(github_user='owner', git_name='Owner', git_email='owner@example.invalid')
    identity.commit(cfg, str(tmp_path), 'fix: source', ['source.py'])
    assert git('show', '--name-only', '--format=', 'HEAD') == 'source.py'
    assert not git('status', '--porcelain')
