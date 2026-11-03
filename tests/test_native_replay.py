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


def test_tracked_unchecked_hash_bytecode_is_real_and_refused(setup, tmp_path):  # noqa: F811
    import py_compile
    import subprocess
    import sys
    cfg, q, source, recipe, data = setup
    # Store wrong executable bytes as unchecked-hash bytecode, then restore
    # mapped source bytes: Python really executes999 despite disk source value1.
    target = source / 'value.py'
    target.write_text('value=999\n')
    py_compile.compile(str(target), doraise=True, invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH)
    target.write_text('value=1\n')
    git(source, 'add', '.')
    git(source, 'add', '-f', '__pycache__')
    git(source, 'commit', '-qm', 'preexisting tracked cache')
    base = git(source, 'rev-parse', 'HEAD')
    output = subprocess.check_output([sys.executable, '-c', 'import value;print(value.value)'], cwd=source, text=True)
    assert output.strip() == '999'
    target.write_text('value=2\n')
    (source / 'tests').mkdir()
    (source / 'tests/test_value.py').write_text('from value import value\ndef test_value():\n assert value==2\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'fix and regression')
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'schema': 1, 'modules': {'value': 'value.py'}, 'tests': ['tests/test_value.py'], 'expected_failures': ['tests/test_value.py::test_value']}))
    with pytest.raises(native_replay.Invalid, match='tracked Python bytecode/cache'):
        native_replay._spec(spec, source, base)
    assert q.items['i'].status == 'clean'


@pytest.mark.parametrize('mode', ['duplicate', 'symlink_parent'])
def test_test_selections_refuse_duplicates_and_symlink_ancestors(setup, tmp_path, mode):  # noqa: F811
    cfg, q, source, recipe, data = setup
    (source / 'value.py').write_text('value=2\n')
    tests = source / 'tests'
    tests.mkdir()
    (tests / 'test_value.py').write_text('def test_value():\n assert True\n')
    git(source, 'add', '.')
    git(source, 'commit', '-qm', 'fix and test')
    args = ['tests/test_value.py']
    if mode == 'duplicate':
        args *= 2
        match = 'distinct'
    else:
        outside = tmp_path / 'outside'
        outside.mkdir()
        (outside / 'test_other.py').write_text('def test_other():\n assert True\n')
        (source / 'test_link').symlink_to(outside, target_is_directory=True)
        git(source, 'add', 'test_link')
        git(source, 'commit', '-qm', 'tracked directory symlink')
        # Include link as a test path; mapping's changed-source guard also
        # refuses symlink source entries, so use prior fixcommit as diff base.
        base = git(source, 'rev-parse', 'HEAD')
        (source / 'value.py').write_text('value=3\n')
        (tests / 'test_value.py').write_text('def test_value():\n assert False\n')
        git(source, 'add', '.')
        git(source, 'commit', '-qm', 'next fix and test')
        args = ['test_link/test_other.py']
        match = 'regular test files'
    if mode == 'duplicate':
        base = git(source, 'rev-parse', 'origin/HEAD')
    spec = tmp_path / 'spec.json'
    spec.write_text(json.dumps({'schema':1,'modules':{'value':'value.py'},'tests':args,'expected_failures':['tests/test_value.py::test_value']}))
    with pytest.raises(native_replay.Invalid, match=match):
        native_replay._spec(spec, source, base)
