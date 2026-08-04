from app.eval.dataset import EvalCase
from app.eval.metrics import run_eval, score_context_recall


def test_all_expected_sources_found_scores_one():
    case = EvalCase(
        question="怎么重置密码？",
        expected_answer="在设置页面点击重置密码",
        expected_sources=["faq/reset_password.md"],
    )

    score = score_context_recall(
        case, retrieved_sources=["faq/reset_password.md", "faq/login.md"]
    )

    assert score == 1.0


def test_partial_expected_sources_found_scores_fraction():
    case = EvalCase(
        question="登录失败怎么排查？",
        expected_answer="先检查账号密码，再看是否触发了错误码E502",
        expected_sources=["faq/login.md", "errors/e502.md"],
    )

    score = score_context_recall(case, retrieved_sources=["faq/login.md"])

    assert score == 0.5


def test_run_eval_averages_context_recall_across_the_dataset():
    full_match_case = EvalCase(
        question="怎么重置密码？",
        expected_answer="在设置页面点击重置密码",
        expected_sources=["faq/reset_password.md"],
    )
    partial_match_case = EvalCase(
        question="登录失败怎么排查？",
        expected_answer="先检查账号密码，再看是否触发了错误码E502",
        expected_sources=["faq/login.md", "errors/e502.md"],
    )

    result = run_eval(
        [
            (full_match_case, ["faq/reset_password.md"]),
            (partial_match_case, ["faq/login.md"]),
        ]
    )

    assert result.per_case_scores == [1.0, 0.5]
    assert result.average_context_recall == 0.75
