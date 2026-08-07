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


def test_check_text_flags_id_card_number():
    result = check_text("我的身份证号是11010519491231002X，麻烦核对一下")

    assert result.is_safe is False
    assert "id_card" in result.matched_terms


def test_check_text_flags_all_digit_id_card_number():
    result = check_text("身份证号440524188001010014可以查一下吗")

    assert result.is_safe is False
    assert "id_card" in result.matched_terms


def test_check_text_flags_email_address():
    result = check_text("请发到 test.user+tag@example.com 谢谢")

    assert result.is_safe is False
    assert "email" in result.matched_terms


def test_check_text_does_not_flag_unrelated_text_as_pii():
    result = check_text("今天天气怎么样")

    assert result.is_safe is True
    assert result.matched_terms == []


def test_check_text_does_not_flag_long_order_number_as_id_card():
    result = check_text("我的订单号是20250807123456789012，帮我查一下")

    assert result.is_safe is True
    assert result.matched_terms == []


def test_check_text_include_email_false_does_not_flag_email():
    result = check_text(
        "如需帮助请联系 support@example.com", include_email=False
    )

    assert result.is_safe is True
    assert result.matched_terms == []


def test_check_text_include_email_true_still_flags_email_by_default():
    result = check_text("如需帮助请联系 support@example.com")

    assert result.is_safe is False
    assert "email" in result.matched_terms
