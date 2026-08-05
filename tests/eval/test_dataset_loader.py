from app.eval.dataset import load_eval_cases


def test_load_eval_cases_parses_jsonl(tmp_path):
    path = tmp_path / "seed.jsonl"
    path.write_text(
        '{"question": "怎么重置密码？", "expected_answer": "在设置页面点击重置密码", '
        '"expected_sources": ["faq/reset_password.md"]}\n'
        '{"question": "登录失败怎么办？", "expected_answer": "检查账号密码", '
        '"expected_sources": ["faq/login.md"]}\n',
        encoding="utf-8",
    )

    cases = load_eval_cases(path)

    assert len(cases) == 2
    assert cases[0].question == "怎么重置密码？"
    assert cases[0].expected_sources == ["faq/reset_password.md"]
    assert cases[1].question == "登录失败怎么办？"


def test_load_eval_cases_skips_comment_lines(tmp_path):
    path = tmp_path / "seed.jsonl"
    path.write_text(
        "# 占位示例数据，非真实业务内容\n"
        '{"question": "A", "expected_answer": "a", "expected_sources": []}\n',
        encoding="utf-8",
    )

    cases = load_eval_cases(path)

    assert len(cases) == 1
    assert cases[0].question == "A"


def test_load_eval_cases_skips_blank_lines(tmp_path):
    path = tmp_path / "seed.jsonl"
    path.write_text(
        '{"question": "A", "expected_answer": "a", "expected_sources": []}\n'
        "\n"
        '{"question": "B", "expected_answer": "b", "expected_sources": []}\n',
        encoding="utf-8",
    )

    cases = load_eval_cases(path)

    assert len(cases) == 2
