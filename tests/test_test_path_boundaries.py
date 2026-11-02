import subprocess
import sys
from pathlib import Path

import pytest

from ossforge import predictor, verify


@pytest.mark.parametrize('path', ['src/contest.py', 'src/latest.py', 'src/inspection.py', 'src/spectrum.py'])
def test_production_substrings_are_not_test_paths(path):
    assert not predictor.is_test_path(path)


@pytest.mark.parametrize('path', ['tests/a.py', 'test/a.js', '__tests__/a.ts', 'test_a.py', 'a_test.go', 'a.test.ts', 'a.spec.ts', 'ASpec.scala', 'FooTest.java', 'spec/a.rb', 'a_spec.rb'])
def test_conventional_test_paths(path):
    assert predictor.is_test_path(path)


def test_real_verifier_can_revert_contest_source(tmp_path):
    def git(*args):
        return subprocess.run(['git', *args], cwd=tmp_path, check=True, capture_output=True, text=True).stdout.strip()
    git('init', '-q', '-b', 'main')
    git('config', 'user.name', 'Fixture')
    git('config', 'user.email', 'fixture@example.invalid')
    (tmp_path/'contest.py').write_text('value = 1\n')
    git('add', '.')
    git('commit', '-qm', 'base')
    base = git('rev-parse', 'HEAD')
    git('switch', '-qc', 'fix')
    (tmp_path/'contest.py').write_text('value = 2\n')
    (tmp_path/'test_contest.py').write_text('from contest import value\nassert value == 2\n')
    git('add', '.')
    git('commit', '-qm', 'fix')
    result = verify.verify_fix(tmp_path, base, Path(sys.executable), [], test_command=[sys.executable, 'test_contest.py'])
    assert result.ok, result.log
    assert result.fix_files == ['contest.py']
    assert result.test_files == ['test_contest.py']
    assert not git('status', '--porcelain')
