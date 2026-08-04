from app.ingestion.chunking import chunk_markdown


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
