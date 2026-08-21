import time
from contextlib import asynccontextmanager

import redis
from fastapi import APIRouter, FastAPI, Header, HTTPException, Request, status
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse, Response
from sqlalchemy import text
from fastapi.middleware.cors import CORSMiddleware

from notepatch.modules.admin.api import admin, management
from notepatch.modules.ai.api import ai
from notepatch.modules.documents.api import artifacts, documents, webhooks
from notepatch.modules.identity.api import auth, presence, profile, workspaces
from notepatch.modules.learning.api import assets, homeworks, knowledge, learning, mistakes
from notepatch.modules.tasks.api import tasks, workflows
from notepatch.platform.config import get_settings
from notepatch.platform.database import SessionLocal
from notepatch.platform.metrics import HTTP_LATENCY, HTTP_REQUESTS, render_metrics
from notepatch.platform.storage import StorageService
from notepatch.shared.api import ApiError
from notepatch.platform.startup import validate_production_settings

settings = get_settings()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    validate_production_settings(settings)
    yield


app = FastAPI(
    title=settings.app_name,
    root_path=settings.public_path_prefix,
    docs_url="/api/v1/docs",
    openapi_url="/api/v1/openapi.json",
    redoc_url=None,
    lifespan=lifespan,
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
    profile.router,
    workspaces.router,
    admin.router,
    management.router,
    documents.router,
    artifacts.router,
    tasks.router,
    workflows.router,
    workflows.document_router,
    assets.router,
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


def _uses_api_envelope(path: str) -> bool:
    return path.startswith("/api/v1/user/") or ("/ai/conversations/" in path and path.endswith("/revisions"))


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": exc.code, "message": exc.message, "data": exc.data},
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    if not _uses_api_envelope(request.url.path):
        return await request_validation_exception_handler(request, exc)
    return JSONResponse(
        status_code=422,
        content={
            "code": "validation_error",
            "message": "Request validation failed",
            "data": {"errors": jsonable_encoder(exc.errors())},
        },
    )


@app.exception_handler(HTTPException)
async def envelope_http_error_handler(request: Request, exc: HTTPException):
    if not _uses_api_envelope(request.url.path):
        return await http_exception_handler(request, exc)
    message = exc.detail if isinstance(exc.detail, str) else "Request failed"
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": f"http_{exc.status_code}", "message": message, "data": None},
        headers=exc.headers,
    )


@app.middleware("http")
async def observe_http(request, call_next):
    started = time.monotonic()
    response = await call_next(request)
    route = getattr(request.scope.get("route"), "path", request.url.path)
    HTTP_REQUESTS.labels(request.method, route, str(response.status_code)).inc()
    HTTP_LATENCY.labels(request.method, route).observe(time.monotonic() - started)
    return response


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok",
        "revision": settings.release_revision,
        "build_time": settings.release_build_time,
        "schema_revision": settings.schema_revision,
        "environment": settings.environment,
    }


@app.get("/ready", include_in_schema=False)
def ready() -> dict:
    dependencies = {"database": "unavailable", "redis": "unavailable", "storage": "unavailable"}
    actual_revision = None
    try:
        with SessionLocal() as db:
            actual_revision = db.scalar(text("SELECT version_num FROM alembic_version LIMIT 1"))
        dependencies["database"] = "ok" if actual_revision == settings.schema_revision else "schema_mismatch"
    except Exception:
        pass
    try:
        redis.from_url(settings.redis_url, socket_connect_timeout=1, socket_timeout=1).ping()
        dependencies["redis"] = "ok"
    except Exception:
        pass
    try:
        if StorageService().bucket_exists():
            dependencies["storage"] = "ok"
    except Exception:
        pass
    if any(value != "ok" for value in dependencies.values()):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "status": "not_ready",
                "dependencies": dependencies,
                "schema_revision": actual_revision,
                "expected_schema_revision": settings.schema_revision,
            },
        )
    return {"status": "ready", "dependencies": dependencies, "revision": settings.release_revision}


@app.get("/metrics", include_in_schema=False)
def metrics(authorization: str | None = Header(default=None)) -> Response:
    if settings.metrics_token and authorization != f"Bearer {settings.metrics_token}":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid metrics token")
    payload, content_type = render_metrics()
    return Response(payload, media_type=content_type)
