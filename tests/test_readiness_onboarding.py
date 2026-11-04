"""The public new-user setup remains usable before an IDE prompt."""
from pathlib import Path
import shutil
import subprocess

from ossforge import cli, identity
from ossforge.config import load


def test_example_config_identity_can_be_filled_before_prompt(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    text = (root / 'ossforge.example.yaml').read_text(encoding='utf-8')
    for key, value in [('github_user', 'example-user'), ('git_name', 'Example User'),
                       ('git_email', 'example-user@example.org')]:
        text = text.replace(f'{key}: ""', f'{key}: "{value}"')
    text = text.replace('repo_first: false', 'repo_first: true')
    config = tmp_path / 'ossforge.yaml'
    config.write_text(text, encoding='utf-8')
    cfg = load(config)
    assert not identity.missing_settings(cfg)
    assert cfg.repo_first and cfg.daily_cap == 2
    assert not cfg.auto_submit and not cfg.signoff and cfg.signoff_repos == []
    monkeypatch.setattr(identity, 'check_login', lambda cfg: [])
    monkeypatch.setattr(identity, 'check_worktree', lambda *a: [])
    monkeypatch.setattr(identity, 'effective_idents', lambda cwd: {
        role: ('Example User', 'example-user@example.org') for role in ('author', 'committer')})
    assert cli.main(['--config', str(config), 'whoami']) == 0
    assert 'READY to publish' in capsys.readouterr().out
    assert not (tmp_path / '.state/research_session.json').exists()


def test_readme_orders_configuration_and_readiness_before_initial_prompt():
    root = Path(__file__).resolve().parents[1]
    text = (root / 'README.md').read_text(encoding='utf-8')
    assert text.index('### 3. Install:') < text.index('### 4. Fill your local config')
    assert text.index('### 4. Fill your local config') < text.index('### 5. Verify readiness')
    for command in ('doctor', 'whoami', 'agent-next'):
        assert f'ossforge --config ossforge.yaml {command}' in text
    assert '| `github_user` | gh login |' not in text


def test_local_user_config_is_ignored_but_public_example_is_tracked(tmp_path):
    root = Path(__file__).resolve().parents[1]
    shutil.copyfile(root / '.gitignore', tmp_path / '.gitignore')
    subprocess.run(['git', 'init', '-q', str(tmp_path)], check=True)
    result = subprocess.run(['git', 'check-ignore', 'ossforge.yaml', 'ossforge.example.yaml'],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.splitlines() == ['ossforge.yaml']
