"""FastAPI application entry point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import settings
from app.database import init_schema, list_tables
from app.routers import admin, dev

TEMPLATES_DIR = settings.project_root / "templates"
STATIC_DIR = settings.project_root / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup tasks before serving requests."""
    settings.ensure_directories()
    init_schema()
    yield


app = FastAPI(
    title="Homework Makeup",
    description="Classroom tool for traceable homework distribution to absent students.",
    version="0.1.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

app.include_router(admin.router)
app.include_router(dev.router)


@app.get("/health")
def health_check() -> dict[str, str | list[str]]:
    """Simple health endpoint for verifying the server and database."""
    tables = list_tables()
    expected = {
        "students",
        "attendance_uploads",
        "attendance_records",
        "assignments",
        "claim_tokens",
        "claim_logs",
    }
    missing = sorted(expected - set(tables))

    return {
        "status": "ok" if not missing else "degraded",
        "database": "ready" if not missing else "missing_tables",
        "tables": tables,
        "missing_tables": missing,
    }


@app.get("/", response_class=HTMLResponse)
def home(request: Request) -> HTMLResponse:
    """Landing page shown to students and teachers."""
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={"title": "Homework Makeup"},
    )