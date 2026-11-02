import pytest
from ossforge import safety


@pytest.mark.parametrize('path', ['.github/CONTRIBUTING.md', 'CONTRIBUTING.rst', 'docs/CONTRIBUTING.rst', 'doc/contrib.rst', 'AI_POLICY.md', '.github/AI_POLICY.md', '.github/CODE_OF_CONDUCT.md'])
def test_recognized_governance_docs_cannot_be_changed(path):
    assert safety.protected(path)
    assert safety.check_diff([path], '+edited policy\n') == [f'protected path: {path}']


@pytest.mark.parametrize('path', ['README.md', 'README.rst', 'docs/guide.md', 'src/policy.py', 'tests/test_policy.py', '.github/PULL_REQUEST_TEMPLATE/fix.md'])
def test_ordinary_docs_templates_and_source_not_blanket_protected(path):
    assert not safety.protected(path)


def test_existing_root_protections_retained():
    for path in ['CONTRIBUTING.md', 'AGENTS.md', 'LICENSE', '.github/workflows/ci.yml']:
        assert safety.protected(path)
