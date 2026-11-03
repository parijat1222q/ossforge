from pathlib import Path
import pytest
from ossforge import cli, guides


def test_export_only_public_companions_no_workflow_state(tmp_path):
    target = tmp_path/'new-workspace'
    paths = guides.export(str(target))
    assert {Path(p).relative_to(target).as_posix() for p in paths} == set(guides.NAMES)
    assert (target/'AGENTS.md').read_text().startswith('# Operating ossforge')
    assert not (target/'ossforge.yaml').exists()
    assert not (target/'.state').exists()
    original = (target/'AGENTS.md').read_bytes()
    with pytest.raises(FileExistsError):
        guides.export(str(target))
    assert (target/'AGENTS.md').read_bytes() == original


def test_export_refuses_existing_file_symlink_and_traversal(tmp_path):
    file = tmp_path/'file'
    file.write_text('preserve')
    with pytest.raises(FileExistsError):
        guides.export(str(file))
    with pytest.raises(ValueError, match='traversal'):
        guides.export(str(tmp_path/'sub'/'..'/'new'))
    link = tmp_path/'link'
    try:
        link.symlink_to(tmp_path, target_is_directory=True)
    except OSError:
        pytest.skip('symlink creation unavailable')
    with pytest.raises(ValueError, match='symlinks'):
        guides.export(str(link/'new'))
    assert file.read_text() == 'preserve'


def test_guide_does_not_load_config_or_open_workflow_state(monkeypatch, capsys):
    monkeypatch.setattr(cli, 'load', lambda *a: pytest.fail('guide must not load config'))
    monkeypatch.setattr(cli, '_ctx', lambda *a: pytest.fail('guide must not open workflow state'))
    assert cli.main(['guide']) == 0
    assert 'research-session' in capsys.readouterr().out
