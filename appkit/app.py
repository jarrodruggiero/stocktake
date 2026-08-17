"""appkit.app — a configured FastAPI instance so apps don't repeat boilerplate.

`create_app()` wires up structured logging, `/healthz` + `/readyz` probes (matching
the kube_app module's expectations), optional static files and Jinja2 templates, and
stashes the validated settings on `app.state` for routes to read.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import BaseAppSettings


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_app(
    settings: BaseAppSettings,
    *,
    templates_dir: str | None = None,
    static_dir: str | None = None,
    readiness_check: Callable[[], bool] | None = None,
    lifespan=None,
) -> FastAPI:
    """Build a FastAPI app from validated settings.

    templates_dir / static_dir: optional per-app paths for Jinja2 + StaticFiles.
    readiness_check: optional callable (e.g. a DB ping) gating `/readyz`.
    lifespan: optional FastAPI lifespan context manager (background jobs etc.).
    """
    configure_logging(settings.log_level)
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.state.settings = settings

    @app.get("/healthz", response_class=PlainTextResponse, include_in_schema=False)
    def healthz() -> str:  # liveness — process is up
        return "ok"

    @app.get("/readyz", response_class=PlainTextResponse, include_in_schema=False)
    def readyz() -> PlainTextResponse:  # readiness — dependencies are reachable
        if readiness_check is not None and not readiness_check():
            return PlainTextResponse("not ready", status_code=503)
        return PlainTextResponse("ok")

    if static_dir and Path(static_dir).is_dir():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")
    if templates_dir:
        app.state.templates = Jinja2Templates(directory=templates_dir)

    return app
