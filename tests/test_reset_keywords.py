from app.channels.reset_keywords import RESET_KEYWORDS, is_reset_keyword


def test_exact_match_case_insensitive():
    assert is_reset_keyword("/reset")
    assert is_reset_keyword("  /RESET  ")
    assert is_reset_keyword("취향 초기화")
    assert is_reset_keyword("reset taste")


def test_non_match():
    assert not is_reset_keyword(None)
    assert not is_reset_keyword("")
    assert not is_reset_keyword("reset")
    assert not is_reset_keyword("/start")
    assert not is_reset_keyword("미니멀한 코트 찾아줘")


def test_keyword_set_frozen():
    assert isinstance(RESET_KEYWORDS, frozenset)
    assert "/reset" in RESET_KEYWORDS
