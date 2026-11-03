"""Private pytest entry point for bounded source replay, never a generic driver.

Runs inside the requested native namespace. Repository tests/plugins remain
trusted local code; this is stale-input detection, not hostile-code attestation.
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main() -> int:
    import pytest
    spec_file, output_file, *args = sys.argv[1:]
    spec = json.loads(Path(spec_file).read_text())
    reports, collected, problems, seen = [], [], [], {}

    def inspect():
        for name, expected in spec['modules'].items():
            module = sys.modules.get(name)
            if module is None:
                continue
            raw = getattr(module, '__file__', None)
            if not raw:
                problems.append('module has no file: ' + name)
                continue
            path = Path(raw).resolve()
            if str(path) != expected['path']:
                problems.append('module origin differs: ' + name + ': ' + str(path))
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected['sha256']:
                problems.append('module content differs: ' + name)
            seen[name] = {'path': str(path), 'sha256': digest}

    class Observer:
        def pytest_collection_finish(self, session):
            collected.extend(item.nodeid for item in session.items)
            inspect()

        def pytest_collectreport(self, report):
            if report.failed:
                problems.append('collection error: ' + report.nodeid)

        def pytest_runtest_logreport(self, report):
            inspect()
            assertion = False
            if report.failed and report.when == 'call':
                # Pytest's structured crash location is evidence, not substring
                # matching arbitrary captured stdout from a test.
                crash = getattr(report.longrepr, 'reprcrash', None)
                message = getattr(crash, 'message', '')
                assertion = message.startswith(('AssertionError', 'assert '))
            reports.append({'nodeid': report.nodeid, 'when': report.when,
                            'outcome': report.outcome, 'assertion': assertion})

        def pytest_sessionfinish(self, session, exitstatus):
            inspect()
            for name in spec['modules']:
                if name not in seen:
                    problems.append('required module not imported: ' + name)
            if not collected or not reports:
                problems.append('no collected/executed tests')
            record = {'schema': 1, 'nonce': spec['nonce'], 'collected': collected,
                      'reports': reports, 'modules': seen,
                      'problems': sorted(set(problems)), 'exit_code': int(exitstatus)}
            Path(output_file).write_text(json.dumps(record))
            if problems:
                session.exitstatus = 127

    return int(pytest.main(args, plugins=[Observer()]))


if __name__ == '__main__':
    raise SystemExit(main())
