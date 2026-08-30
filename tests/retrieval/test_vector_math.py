import math

import pytest

from app.retrieval.vector_math import cosine_similarity


def test_identical_vectors_score_one():
    assert cosine_similarity([1.0, 2.0, 3.0], [1.0, 2.0, 3.0]) == pytest.approx(1.0)


def test_orthogonal_vectors_score_zero():
    assert cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)


def test_opposite_vectors_score_minus_one():
    assert cosine_similarity([1.0, 2.0], [-1.0, -2.0]) == pytest.approx(-1.0)


def test_zero_vector_scores_zero_instead_of_dividing_by_zero():
    """零向量返回 0.0，不抛 ZeroDivisionError。

    这条守卫是必须的，不是防御性编程：记忆条目和向量库记录里都可能出现
    没写过 embedding 的历史数据，调用方按"分数低就排在后面"处理，不预期
    这里抛异常。四份被合并掉的副本都带着这个分支，合并后必须保住。
    """
    assert cosine_similarity([0.0, 0.0], [1.0, 2.0]) == 0.0
    assert cosine_similarity([1.0, 2.0], [0.0, 0.0]) == 0.0
    assert cosine_similarity([0.0, 0.0], [0.0, 0.0]) == 0.0


def test_shorter_vector_truncates_the_comparison():
    """长度不等时按较短的那个截断（zip 的语义）。

    合并前四份副本都用 zip，行为一致；这里把它钉住，避免以后有人"顺手"
    改成补零或抛错——那会静默改变所有调用方的排序结果。
    """
    assert cosine_similarity([1.0, 0.0, 99.0], [1.0, 0.0]) == pytest.approx(
        1.0 / math.sqrt(1 + 99 * 99)
    )
