from fastapi import FastAPI

from app.api.agent_routes import router as agent_router
from app.api.qa_routes import router as qa_router

app = FastAPI()
app.include_router(qa_router)
app.include_router(agent_router)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
