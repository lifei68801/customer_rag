from app.providers.embedding import EmbeddingRegistry, EmbeddingRequest


class FakeEmbeddingProvider:
    def __init__(self, dimension: int) -> None:
        self._dimension = dimension

    async def embed(self, request: EmbeddingRequest):
        from app.providers.embedding import EmbeddingResult

        return EmbeddingResult(
            vectors=[[0.1] * self._dimension for _ in request.texts]
        )


async def test_run_routes_to_the_named_embedding_provider():
    registry = EmbeddingRegistry()
    registry.register("qwen-embedding", FakeEmbeddingProvider(dimension=4))
    registry.register("bge", FakeEmbeddingProvider(dimension=8))

    result = await registry.run(
        EmbeddingRequest(texts=["hello", "world"]),
        provider_name="bge",
    )

    assert len(result.vectors) == 2
    assert len(result.vectors[0]) == 8
