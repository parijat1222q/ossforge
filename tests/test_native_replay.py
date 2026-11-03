import copy
import json

import pytest

from ossforge import native_replay
from test_environment import setup, git  # noqa: F401


def observations():
    base = {'process_exit': 1, 'collected': ['tests/test_value.py::test_value'],
            'reports': [{'nodeid': 'tests/test_value.py::test_value', 'when': 'call', 'outcome': 'failed', 'assertion': True}]}
    fixed = copy.deepcopy(base)
    fixed['process_exit'] = 0
    fixed['reports'][0]['outcome'] = 'passed'
    return base, fixed


@pytest.mark.parametrize('change', ['origin', 'missing', 'skip', 'collection', 'setup', 'different', 'notassertion'])
def test_compare_rejects_false_baseline(change):
    base, fixed = observations()
    if change == 'origin':
        base['process_exit'] = 127
    elif change == 'missing':
        base['reports'] = []
    elif change == 'skip':
        base['reports'][0]['outcome'] = 'skipped'
    elif change == 'collection':
        base['collected'] = []
    elif change == 'setup':
        base['reports'][0]['when'] = 'setup'
    elif change == 'different':
        fixed['collected'] = ['tests/other.py::test_other']
    else:
        base['reports'][0]['assertion'] = False
    with pytest.raises(native_replay.Invalid):
        native_replay._compare(base, fixed, ['tests/test_value.py::test_value'])


def test_compare_accepts_expected_assertion():
    base, fixed = observations()
    native_replay._compare(base, fixed, ['tests/test_value.py::test_value'])


def test_spec_covers_changed_source_without_promoting(setup):  # noqa: F811
    cfg, q, source, recipe, data = setup
    (source / 'value.py').write_text('value=2\n')
    (source / 'tests').mkdir()
    (source / 'tests/test_value.py').write_text('from value import value\ndef test_value():\n assert value == 2\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'fix and regression')
    spec = recipe.parent / 'replay.json'
    spec.write_text(json.dumps({'schema': 1, 'modules': {'value': 'value.py'}, 'tests': ['tests/test_value.py'], 'expected_failures': ['tests/test_value.py::test_value']}))
    # Spec validation is deterministic and independent of optional native deps.
    base = git(source, 'rev-parse', 'origin/HEAD')
    assert native_replay._spec(spec, source, base)['modules'] == {'value': 'value.py'}
    (source / 'config.ini').write_text('changed')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'unsupported extra input')
    with pytest.raises(native_replay.Invalid, match='every changed'):
        native_replay._spec(spec, source, base)
    assert q.items['i'].status == 'clean'


@pytest.mark.parametrize('mode', ['assertion', 'error', 'missing', 'copy', 'skip', 'collecterror'])
def test_actual_pytest_observer(mode, tmp_path):
    import hashlib
    import subprocess
    import sys
    import uuid
    from pathlib import Path
    from ossforge import pytest_observer
    source = tmp_path / 'source'
    source.mkdir()
    target = source / 'value.py'
    target.write_text('value=1\n')
    copy = tmp_path / 'copy.py'
    copy.write_text('value=2\n')
    test = source / 'test_value.py'
    code = 'import value\ndef test_value():\n assert value.value == 2\n'
    if mode == 'error':
        code = 'import value\ndef test_value():\n raise RuntimeError("broken dependency")\n'
    elif mode == 'missing':
        code = 'def test_value():\n assert True\n'
    elif mode == 'copy':
        code = f'import sys,importlib.util\ns=importlib.util.spec_from_file_location("value", {str(copy)!r})\nvalue=importlib.util.module_from_spec(s)\nsys.modules["value"]=value\ns.loader.exec_module(value)\ndef test_value():\n assert value.value == 2\n'
    elif mode == 'skip':
        code = 'import value,pytest\n@pytest.mark.skip\ndef test_value():\n assert True\n'
    elif mode == 'collecterror':
        code = 'import value\nraise ImportError("missing dependency")\n'
    test.write_text(code)
    nonce = uuid.uuid4().hex
    spec = tmp_path / 'input.json'
    output = tmp_path / 'output.json'
    spec.write_text(json.dumps({'nonce': nonce, 'modules': {'value': {'path': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()}}}))
    p = subprocess.run([sys.executable, str(Path(pytest_observer.__file__)), str(spec), str(output), '-q', str(test)], cwd=source, capture_output=True, text=True, timeout=30)
    observation = json.loads(output.read_text())
    assert observation['nonce'] == nonce
    if mode in {'missing', 'copy', 'collecterror'}:
        assert p.returncode == 127 and observation['problems']
    elif mode == 'skip':
        assert any(r['outcome'] == 'skipped' for r in observation['reports'])
    else:
        assert p.returncode == 1 and observation['problems'] == []
        call = next(r for r in observation['reports'] if r['when'] == 'call')
        assert call['assertion'] == (mode == 'assertion')
