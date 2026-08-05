from app.ingestion.pipeline import ingest_directory, ingest_markdown_file
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.retrieval.vector_store import InMemoryVectorStore


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


async def test_ingest_markdown_file_chunks_embeds_and_upserts(tmp_path):
    md_file = tmp_path / "network.md"
    md_file.write_text(
        "## 网络故障\n网络断开时请先重启路由器。\n"
        "## 登录问题\n登录失败请检查账号密码。\n",
        encoding="utf-8",
    )

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    count = await ingest_markdown_file(
        md_file,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
    )

    assert count == 2
    results = await vector_store.search(query_vector=[0.1, 0.2], top_k=2)
    assert len(results) == 2
    texts = {record.text for record in results}
    assert "网络断开时请先重启路由器。" in texts
    assert "登录失败请检查账号密码。" in texts


async def test_ingest_directory_processes_markdown_and_pdf_but_skips_other_extensions(
    tmp_path,
):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.cidfonts import UnicodeCIDFont
    from reportlab.pdfgen import canvas

    pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))

    (tmp_path / "a.md").write_text(
        "## 主题A\n内容A。\n", encoding="utf-8"
    )
    (tmp_path / "notes.txt").write_text("不应被处理", encoding="utf-8")

    pdf_path = tmp_path / "b.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.setFont("STSong-Light", 12)
    c.drawString(100, 750, "内容B。")
    c.showPage()
    c.save()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    total = await ingest_directory(
        tmp_path,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
    )

    assert total == 2
    results = await vector_store.search(query_vector=[0.1, 0.2], top_k=10)
    texts = {record.text for record in results}
    assert texts == {"内容A。", "内容B。"}
