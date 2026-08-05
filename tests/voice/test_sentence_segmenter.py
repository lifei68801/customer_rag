from app.voice.sentence_segmenter import split_sentences


def test_splits_on_chinese_sentence_punctuation():
    sentences = split_sentences("网络断开时请先重启路由器。如果还不行，请检查网线连接！")

    assert sentences == ["网络断开时请先重启路由器。", "如果还不行，请检查网线连接！"]


def test_keeps_trailing_text_without_terminal_punctuation_as_last_sentence():
    sentences = split_sentences("第一句。还没说完的部分")

    assert sentences == ["第一句。", "还没说完的部分"]


def test_empty_text_returns_empty_list():
    assert split_sentences("") == []
    assert split_sentences("   ") == []
