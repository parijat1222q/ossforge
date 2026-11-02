import pytest
from ossforge.scout import is_spam

from ossforge.config import Config

KEYWORDS = Config().spam_keywords + ['crypto']

@pytest.mark.parametrize('text', [
    'tokenize crashes on empty input', 'WebRTC connection failure',
    'csrf_token is missing from generated form', 'TokenStream parser bug',
    'shell positional argument $1 is expanded incorrectly',
    'awk prints $10 instead of the last field', 'bash echo $0 and $123',
    'KaTeX renders $1$ and $12$ incorrectly', 'math $12 + 3$ is broken',
    'replace group `$1` is not expanded',
])
def test_legitimate_code_is_not_spam(text):
    assert not is_spam(text, '', KEYWORDS)

@pytest.mark.parametrize('text', [
    'pays $5 on merge', 'pays $9.99 on merge', 'pays US$200 on merge',
    'reward $1 for a fix', 'pay $10 for awk fix', 'bounty for parser fix',
    'receive 25 USDT', '2 BTC for a patch', 'crypto airdrop',
    'pays `$5` on merge', 'pays $5$ on merge',
])
def test_monetary_and_keyword_spam_remains_blocked(text):
    assert is_spam(text, '', KEYWORDS)


def test_custom_keywords_are_literal_terms():
    assert is_spam('request c++ bounty', '', ['c++'])
    assert not is_spam('tokenizer', '', ['token'])
    assert not is_spam('plain report', '', [''])
