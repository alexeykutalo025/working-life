"""The local web server.

Binds to 127.0.0.1 and nothing else. Serves its own HTML, CSS and JavaScript
from disk - there is no CDN link anywhere in this program, so it works with the
network cable unplugged, which is how it is meant to be used.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .. import __version__
from ..config import Settings
from ..db import connect
from ..logging_setup import get_logger

log = get_logger("api")

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
STATIC_DIR = WEB_DIR / "static"


class Db:
    """One SQLite connection per thread.

    SQLite connections are not safe to share across threads, and the server has
    several. Each thread gets its own; WAL mode lets them read while a job
    writes.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._local = threading.local()

    def __call__(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = connect(self.path)
            self._local.conn = conn
        return conn


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(
        title="Recall",
        version=__version__,
        docs_url=None,       # no docs UI: it pulls JavaScript from a CDN
        redoc_url=None,
        openapi_url=None,
    )
    app.state.settings = settings
    app.state.db = Db(settings.db_path)

    from . import findings as findings_router
    from . import home as home_router
    from . import people as people_router
    from . import readall as readall_router
    from . import search as search_router
    from . import sources as sources_router
    from . import timeline as timeline_router

    app.include_router(sources_router.router, prefix="/api")
    app.include_router(readall_router.router, prefix="/api")
    app.include_router(timeline_router.router, prefix="/api")
    app.include_router(findings_router.router, prefix="/api")
    app.include_router(people_router.router, prefix="/api")
    app.include_router(search_router.router, prefix="/api")
    app.include_router(home_router.router, prefix="/api")

    @app.exception_handler(Exception)
    async def unhandled(request: Request, exc: Exception) -> JSONResponse:
        """Never show a stack trace to the user; never hide it from the log."""
        log.exception("Unhandled error on %s", request.url.path)
        return JSONResponse(
            status_code=500,
            content={
                "error": "Something went wrong inside Recall.",
                "detail": f"{exc.__class__.__name__}: {exc}",
                "where": request.url.path,
                "advice": "The full details are in workdir/logs/.",
            },
        )

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__}

    # Everything is served off a local disk, so caching buys nothing and costs
    # something real: after Recall is updated the browser would keep running
    # the old JavaScript against the new API, and the user would see a broken
    # screen with no way to know why. A hard refresh is not a thing to ask of
    # someone who did not know the file was cached.
    @app.middleware("http")
    async def no_cache(request: Request, call_next):
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        response.headers["Pragma"] = "no-cache"
        return response

    if STATIC_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/{path:path}")
    def spa(path: str) -> FileResponse:
        """Hash routing means every page is index.html."""
        return FileResponse(WEB_DIR / "index.html")

    return app


def serve(settings: Settings, *, port: int | None = None, open_browser: bool = False) -> None:
    """Run the server until Ctrl-C."""
    import uvicorn

    host = settings.server.host
    port = port or settings.server.port
    if host != "127.0.0.1":
        raise RuntimeError(
            f"Refusing to listen on {host}. Recall binds to 127.0.0.1 only."
        )

    app = create_app(settings)
    url = f"http://{host}:{port}"

    if open_browser:
        import webbrowser

        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    log.info("Recall is at %s", url)
    uvicorn.run(app, host=host, port=port, log_level="warning", access_log=False)
