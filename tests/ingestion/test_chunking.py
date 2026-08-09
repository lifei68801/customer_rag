from app.ingestion.chunking import Chunk, chunk_markdown, split_oversized_chunks


def test_splits_markdown_by_level_two_headings():
    text = (
        "## 安装指南\n"
        "下载安装包并运行安装程序。\n"
        "## 常见问题\n"
        "如果安装失败，请检查磁盘空间。\n"
    )

    chunks = chunk_markdown(text, source="install.md")

    assert len(chunks) == 2
    assert chunks[0].heading_path == ["安装指南"]
    assert "下载安装包" in chunks[0].text
    assert chunks[1].heading_path == ["常见问题"]
    assert "检查磁盘空间" in chunks[1].text


def test_text_without_headings_becomes_a_single_chunk():
    text = "这是一段没有任何标题的纯文本说明。"

    chunks = chunk_markdown(text, source="plain.md")

    assert len(chunks) == 1
    assert chunks[0].heading_path == []
    assert chunks[0].text == text


def test_chunk_under_threshold_is_returned_unchanged():
    chunk = Chunk(text="短文本", heading_path=["标题"], source="a.md")

    result = split_oversized_chunks([chunk], max_len=800)

    assert result == [chunk]


def test_chunk_with_parent_text_is_never_split():
    """parent-child chunk（比如 PDF 表格行）本来就很小，且二次拆分会
    破坏 parent-child 对应关系，即使文本超阈值也要跳过。"""
    long_text = "x" * 1000
    chunk = Chunk(
        text=long_text, heading_path=[], source="a.md", parent_text="完整表格文本"
    )

    result = split_oversized_chunks([chunk], max_len=800)

    assert result == [chunk]


def test_oversized_chunk_splits_on_paragraph_boundaries():
    text = ("第一段。" * 50) + "\n\n" + ("第二段。" * 50)
    chunk = Chunk(text=text, heading_path=["标题"], source="a.md")

    result = split_oversized_chunks([chunk], max_len=120, overlap=0)

    assert len(result) > 1
    for piece in result:
        assert piece.heading_path == ["标题"]
        assert piece.source == "a.md"
        assert piece.parent_text is None


def test_sub_chunks_include_overlap_from_previous_piece():
    text = "。".join(f"第{i}句内容比较长一些用于测试重叠效果" for i in range(30))
    chunk = Chunk(text=text, heading_path=[], source="a.md")

    result = split_oversized_chunks([chunk], max_len=100, overlap=20)

    assert len(result) > 1
    # 第二个子 chunk 应该以第一个子 chunk 结尾的 20 个字符开头
    assert result[1].text.startswith(result[0].text[-20:])


def test_falls_back_to_hard_cut_when_no_punctuation_or_paragraph_breaks():
    text = "a" * 2000
    chunk = Chunk(text=text, heading_path=[], source="a.md")

    result = split_oversized_chunks([chunk], max_len=500, overlap=0)

    assert len(result) == 4
    assert all(len(piece.text) <= 500 for piece in result)


def test_multiple_chunks_are_each_split_independently():
    """不同结构单元（比如两个不同的 ## 标题）之间不应该互相拼接/重叠。"""
    short_chunk = Chunk(text="短的", heading_path=["A"], source="a.md")
    long_chunk = Chunk(text="超长" * 500, heading_path=["B"], source="a.md")

    result = split_oversized_chunks([short_chunk, long_chunk], max_len=800)

    assert result[0] == short_chunk
    assert len(result) > 2
    assert all(piece.heading_path == ["B"] for piece in result[1:])
