from app.safety.leakage_detection import detect_internal_leakage


def test_detects_python_stack_trace():
    text = (
        'Traceback (most recent call last):\n'
        '  File "app/graphrag/term_matcher.py", line 12, in match_terms'
    )
    result = detect_internal_leakage(text)

    assert result.is_leaked is True
    assert "stack_trace" in result.matched_categories


def test_detects_internal_file_path():
    result = detect_internal_leakage("报错发生在 app/graphrag/term_matcher.py 里")

    assert result.is_leaked is True
    assert "internal_file_path" in result.matched_categories


def test_detects_internal_env_var_name():
    result = detect_internal_leakage("请检查 CUSTOMER_RAG_LLM_API_KEY 是否配置正确")

    assert result.is_leaked is True
    assert "internal_env_var" in result.matched_categories


def test_detects_cypher_query_fragment():
    result = detect_internal_leakage("MATCH (t:Term) RETURN t")

    assert result.is_leaked is True
    assert "db_query_fragment" in result.matched_categories


def test_detects_sql_query_fragment():
    result = detect_internal_leakage("SELECT * FROM users WHERE id = 1")

    assert result.is_leaked is True
    assert "db_query_fragment" in result.matched_categories


def test_does_not_flag_normal_customer_support_reply():
    result = detect_internal_leakage("根据资料所述，重启路由器即可解决。")

    assert result.is_leaked is False
    assert result.matched_categories == []


def test_does_not_flag_chinese_create_account_instruction():
    # "创建"是正常客服业务用词，不应该被 db_query_fragment 规则误伤
    # （规则只匹配英文关键词 CREATE/SELECT/MATCH，不匹配中文）。
    result = detect_internal_leakage("请在设置页面创建一个新账号")

    assert result.is_leaked is False
    assert result.matched_categories == []


def test_does_not_flag_english_prose_with_create_or_select_lowercase():
    result = detect_internal_leakage("Please create (a ticket) first")

    assert result.is_leaked is False
    assert result.matched_categories == []


def test_does_not_flag_english_prose_with_select_from_lowercase():
    result = detect_internal_leakage("select the item from the list")

    assert result.is_leaked is False
    assert result.matched_categories == []
