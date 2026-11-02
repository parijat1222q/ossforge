import pytest
from ossforge.vet import comment_claims_issue


@pytest.mark.parametrize('body', [
    'Hi team, I traced #87836 and prepared a fix. I have this fix pushed to my fork branch and would be happy to open a PR!',
    'I have prepared a fix and pushed it to my fork.',
    'I prepared a patch and pushed it to my fork branch.',
    "I've pushed this fix to my fork and will open a PR.",
])
def test_prepared_fork_fix_is_active_work(body):
    assert comment_claims_issue(body)


@pytest.mark.parametrize('body', [
    'A possible fix would be increasing the buffer.',
    'Could someone prepare a fix and push it to a fork?',
    'The existing patch was prepared by someone else.',
    'I have not prepared a fix or pushed anything to my fork.',
])
def test_unprepared_suggestion_is_not_claim(body):
    assert not comment_claims_issue(body)
