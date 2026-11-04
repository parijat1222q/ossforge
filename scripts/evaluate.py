"""Developer-only replay of public regression cases, never live contribution proof."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
STAGES = {
    'selection': ['tests/test_repo_code_ranking.py', 'tests/test_repo_suitability.py'],
    'budget_checkpoint': ['tests/test_research_session.py', 'tests/test_agent_next.py'],
    'policy_duplicates': ['tests/test_publication_routing.py', 'tests/test_reference_scope.py'],
    'verification': ['tests/test_external_regression.py', 'tests/test_evidence_context.py'],
    'onboarding': ['tests/test_public_guides.py', 'tests/test_doctor.py', 'tests/test_install_contract.py'],
}


def summarize(path: Path) -> dict:
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == 'testsuite' else list(root.findall('testsuite'))
    counts = {key: sum(int(s.get(key, 0)) for s in suites) for key in ('tests', 'failures', 'errors', 'skipped')}
    counts['passed'] = counts['tests'] - counts['failures'] - counts['errors'] - counts['skipped']
    return counts


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=STAGES, action='append', help='repeat to select stages; default all')
    args = parser.parse_args(argv)
    results = []
    with tempfile.TemporaryDirectory(prefix='ossforge-evaluation-') as directory:
        for stage in dict.fromkeys(args.stage or STAGES):
            report = Path(directory) / f'{stage}.xml'
            command = [sys.executable, '-m', 'pytest', '-q', *STAGES[stage], f'--junitxml={report}']
            try:
                run = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, timeout=180)
                counts = summarize(report) if report.is_file() else None
                result = {'stage': stage, 'exit_code': run.returncode, 'counts': counts,
                          'status': 'passed' if run.returncode == 0 and counts and counts['passed'] > 0 else 'failed'}
                if result['status'] != 'passed':
                    print(run.stdout + run.stderr, file=sys.stderr)
            except (subprocess.TimeoutExpired, OSError, ET.ParseError, ValueError) as exc:
                result = {'stage': stage, 'status': 'error', 'reason': str(exc)}
            results.append(result)
    print(json.dumps({'kind': 'developer_regression_replay', 'live_ide_proof': False, 'stages': results}, indent=2))
    return 0 if all(r['status'] == 'passed' for r in results) else 1


if __name__ == '__main__':
    raise SystemExit(main())
