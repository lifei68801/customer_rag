from app.safety.rules import check_text


def test_clean_text_is_safe():
    result = check_text("你好，请问如何重置密码？")

    assert result.is_safe is True
    assert result.matched_terms == []


def test_text_with_phone_number_is_unsafe():
    result = check_text("我的手机号是13812345678，方便联系我")

    assert result.is_safe is False
    assert "phone_number" in result.matched_terms


def test_text_with_banned_term_is_unsafe():
    result = check_text("我们内部代号叫projectX", banned_terms=["projectX"])

    assert result.is_safe is False
    assert "projectX" in result.matched_terms
