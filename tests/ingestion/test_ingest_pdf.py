from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

from app.ingestion.pipeline import ingest_pdf_file
from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest, EmbeddingResult
from app.retrieval.vector_store import InMemoryVectorStore

pdfmetrics.registerFont(UnicodeCIDFont("STSong-Light"))


class FakeEmbeddingProvider:
    async def embed(self, request: EmbeddingRequest) -> EmbeddingResult:
        return EmbeddingResult(vectors=[[0.1, 0.2] for _ in request.texts])


async def test_ingest_pdf_file_chunks_embeds_and_upserts(tmp_path):
    pdf_path = tmp_path / "manual.pdf"
    c = canvas.Canvas(str(pdf_path))
    c.setFont("STSong-Light", 12)
    c.drawString(100, 750, "网络断开时请先重启路由器。")
    c.showPage()
    c.setFont("STSong-Light", 12)
    c.drawString(100, 750, "登录失败请检查账号密码。")
    c.showPage()
    c.save()

    embedding_registry = EmbeddingRegistry()
    embedding_registry.register("fake-embedding", FakeEmbeddingProvider())
    vector_store = InMemoryVectorStore()

    count = await ingest_pdf_file(
        pdf_path,
        embedding_registry=embedding_registry,
        embedding_provider_name="fake-embedding",
        vector_store=vector_store,
    )

    assert count == 2
    results = await vector_store.search(query_vector=[0.1, 0.2], top_k=2)
    texts = {record.text for record in results}
    assert "网络断开时请先重启路由器。" in texts
    assert "登录失败请检查账号密码。" in texts
