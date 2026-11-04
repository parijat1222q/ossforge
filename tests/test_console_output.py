import io
import json
import sys

from ossforge import cli


def test_policy_review_on_cp1252_output_keeps_policy_and_finishes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli.policy_review, 'snapshot', lambda *a, **kw: {
        'docs': [{'path': 'CONTRIBUTING.md', 'url': 'https://example.org/policy',
                  'sha': 'a' * 40, 'text': 'Report bugs \U0001f41b. Read the full policy.'}],
        'anti_ai': False,
    })
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='cp1252')
    monkeypatch.setattr(sys, 'stdout', stream)
    result = cli.main(['policy-review', 'org/repo'])
    stream.flush()
    text = raw.getvalue().decode('cp1252')
    assert result == 0
    assert 'Report bugs \\U0001f41b. Read the full policy.' in text
    assert 'Also read the project' in text
    assert stream.encoding == 'cp1252'
    assert not (tmp_path / '.state/policy_reviews.json').exists()


def test_non_ascii_refusal_on_restricted_stderr_still_returns_failure(tmp_path, monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='ascii')
    monkeypatch.setattr(sys, 'stderr', stream)
    result = cli.main(['--config', str(tmp_path / '\U0001f41b.yaml'), 'queue'])
    stream.flush()
    assert result == 2
    assert 'REFUSED:' in raw.getvalue().decode('ascii')
    assert '\\U0001f41b.yaml' in raw.getvalue().decode('ascii')
    assert stream.encoding == 'ascii'


def test_utf8_output_preserves_unicode(tmp_path, monkeypatch):
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='utf-8')
    monkeypatch.setattr(sys, 'stderr', stream)
    assert cli.main(['--config', str(tmp_path / '\U0001f41b.yaml'), 'queue']) == 2
    stream.flush()
    assert '\U0001f41b.yaml' in raw.getvalue().decode('utf-8')
    assert stream.encoding == 'utf-8'


def test_json_output_remains_parseable_on_restricted_stream(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding='ascii')
    monkeypatch.setattr(sys, 'stdout', stream)
    assert cli.main(['--json', 'queue']) == 0
    stream.flush()
    assert json.loads(raw.getvalue().decode('ascii')) == []


def test_custom_capture_stream_without_reconfigure_is_supported(tmp_path, monkeypatch):
    stream = io.StringIO()
    monkeypatch.setattr(sys, 'stdout', stream)
    monkeypatch.chdir(tmp_path)
    assert cli.main(['--json', 'queue']) == 0
    assert json.loads(stream.getvalue()) == []
