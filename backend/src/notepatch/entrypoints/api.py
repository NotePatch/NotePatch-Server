from fastapi import APIRouter, FastAPI
from fastapi.middleware.cors import CORSMiddleware

from notepatch.modules.admin.api import admin
from notepatch.modules.ai.api import ai
from notepatch.modules.documents.api import artifacts, documents, webhooks
from notepatch.modules.identity.api import auth, presence, workspaces
from notepatch.modules.learning.api import homeworks, knowledge, learning, mistakes
from notepatch.modules.tasks.api import tasks
from notepatch.platform.config import get_settings

settings = get_settings()

app = FastAPI(
    title=settings.app_name,
    docs_url="/api/v1/docs",
    openapi_url="/api/v1/openapi.json",
    redoc_url=None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=settings.cors_origins != ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

api_v1 = APIRouter(prefix="/api/v1")
for router in (
    auth.router,
    workspaces.router,
    admin.router,
    documents.router,
    artifacts.router,
    tasks.router,
    homeworks.router,
    learning.router,
    knowledge.router,
    mistakes.router,
    ai.router,
    presence.router,
    webhooks.router,
):
    api_v1.include_router(router)
app.include_router(api_v1)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}
