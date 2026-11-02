import pytest
from ossforge.vet import comment_claims_issue


@pytest.mark.parametrize('body', [
    "I'd like to take #1993 on an M4 Pro with 48 GB. My proposed slice is one dense encoder.",
    'I would like to take this issue. My plan is to reproduce and test it.',
    'I’d like to take this on; I can attach native evidence.',
    "I'd like to work on this.",
    'I have started implementing a fix and will send a PR.',
])
def test_explicit_first_person_work_plan_is_claim(body):
    assert comment_claims_issue(body)


@pytest.mark.parametrize('body', [
    'This is an unclaimed proposal for triage, not an implementation claim.',
    'Would you like to take this issue?',
    'The API should take a string argument.',
    'I would like to use this feature after it lands.',
])
def test_non_claim_control(body):
    assert not comment_claims_issue(body)
