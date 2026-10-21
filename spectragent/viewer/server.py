"""HTTP presentation of the shared inspection repository.

No runtime and no trace writer: the one thing it writes is label lines, into
the annotation directory it was started with.
"""

from __future__ import annotations

import getpass
import webbrowser
from pathlib import Path, PurePosixPath
from threading import Timer
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from spectragent.annotation import AnnotationError, AnnotationStore
from spectragent.inspection.repository import RepositoryError, open_repository
from spectragent.viewer.annotations import annotation_router, write_refusal


def _key(value: str) -> str:
    """Accept repository-relative identifiers, never a filesystem URL or path."""
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise HTTPException(400, "Expected a relative repository key")
    return value


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise HTTPException(422, "Recorded screenshot is not a supported raster image")


def create_app(
    root: Path,
    initial_run: str | None = None,
    *,
    config_source: Path | str | None = None,
    config_hash: str | None = None,
    annotations: Path | str | None = None,
    annotator: str | None = None,
) -> FastAPI:
    """Create an isolated app: the repository's reads, plus label writes into *annotations*.

    Without *annotations* the app is read-only. With it, label lines are
    appended inside that directory and nowhere else; *annotator* (default:
    the OS user name) is who every line is attributed to.
    """
    root = Path(root).expanduser().resolve()
    source = Path(config_source).expanduser().resolve() if config_source is not None else None
    repository = open_repository(root, config_source=source, config_hash=config_hash)
    store = _annotation_store(annotations, annotator)
    if initial_run is not None:
        # A focus is validated before binding the server. It does not narrow
        # the repository: sibling runs and source-run links stay available.
        repository.run(initial_run)
        initial_run = PurePosixPath(initial_run).as_posix()
        if not repository.matches(initial_run):
            raise RepositoryError(
                f"Initial run {initial_run!r} does not match the selected configuration filters"
            )
    app = FastAPI(
        title="All4One experiment viewer", docs_url=None, redoc_url=None, openapi_url=None
    )

    @app.middleware("http")
    async def response_headers(request: Request, call_next: Any) -> Response:
        writes = request.method not in {"GET", "HEAD"} and request.url.path.startswith(
            "/api/review/"
        )
        refusal = write_refusal(request) if writes else None
        if refusal is not None:
            response: Response = JSONResponse(
                status_code=refusal[0], content={"detail": refusal[1]}
            )
        else:
            response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data: blob:; connect-src 'self'; object-src 'none'; "
            "base-uri 'self'; frame-ancestors 'none'"
        )
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(RepositoryError)
    async def repository_error(request: Request, exc: RepositoryError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(AnnotationError)
    async def annotation_error(request: Request, exc: AnnotationError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(FileNotFoundError)
    async def missing_record(request: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.get("/api/workspace")
    def workspace() -> dict[str, Any]:
        return {
            "root": str(root),
            "initial_run": initial_run,
            "config_source": str(source) if source is not None else None,
            "config_hash": config_hash,
            "annotations": None
            if store is None
            else {"directory": str(store.directory), "annotator": store.annotator},
        }

    @app.get("/api/runs")
    def runs(search: str = "") -> list[dict[str, Any]]:
        return repository.list_runs(search=search)

    @app.get("/api/run")
    def run(key: str) -> dict[str, Any]:
        return repository.run(_key(key))

    @app.get("/api/step")
    def step(key: str, index: Annotated[int, Query(ge=0)]) -> dict[str, Any]:
        return repository.step(_key(key), index)

    @app.get("/api/screenshot")
    def screenshot(
        key: str, index: Annotated[int, Query(ge=0)], view: Literal["browser", "agent"] = "browser"
    ) -> Response:
        data = repository.screenshot(_key(key), index, view=view)
        if data is None:
            raise HTTPException(404, "This step has no committed screenshot")
        return Response(content=data, media_type=_image_type(data))

    @app.get("/api/compare")
    def compare(key: Annotated[list[str], Query(min_length=2, max_length=8)]) -> dict[str, Any]:
        return repository.compare([_key(value) for value in key])

    @app.get("/api/compare-groups")
    def compare_groups(left: str, right: str, evaluator: str) -> dict[str, Any]:
        return repository.compare_groups(left=left, right=right, evaluator=evaluator)

    @app.get("/api/reports")
    def reports() -> list[dict[str, Any]]:
        return repository.reports()

    @app.get("/api/report")
    def report(key: str) -> dict[str, Any]:
        return repository.report(_key(key))

    app.state.annotations = store
    app.include_router(annotation_router(repository, store, _key, _image_type))

    static = Path(__file__).parent / "static"

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        page = static / "index.html"
        if not page.is_file():
            raise HTTPException(
                503, "Viewer assets are missing; run npm ci && npm run build in frontend/viewer"
            )
        return FileResponse(page, headers={"Cache-Control": "no-cache"})

    # Only packaged build assets are exposed. Recorded files are always resolved
    # by the inspection repository and its manifest-aware RunTrace accessors.
    assets = static / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")
    brand = static / "brand"
    if brand.is_dir():
        app.mount("/brand", StaticFiles(directory=brand), name="brand")
    return app


def _annotation_store(
    directory: Path | str | None, annotator: str | None
) -> AnnotationStore | None:
    if directory is None:
        return None
    path = Path(directory).expanduser().resolve()
    for folder in (path, *path.parents):
        if (folder / "manifest.json").is_file():
            raise AnnotationError(
                f"The annotation directory {path} is inside the recorded run {folder}; "
                "labels never go into a trace"
            )
    return AnnotationStore(path, annotator=annotator or getpass.getuser())


def serve(
    root: Path,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = False,
    initial_run: str | None = None,
    *,
    config_source: Path | str | None = None,
    config_hash: str | None = None,
    annotations: Path | str | None = None,
    annotator: str | None = None,
) -> None:
    """Run the local viewer; opening a browser is always an explicit choice."""
    app = create_app(
        root,
        initial_run=initial_run,
        config_source=config_source,
        config_hash=config_hash,
        annotations=annotations,
        annotator=annotator,
    )
    url_host = f"[{host}]" if ":" in host else host
    # Requests read evidence or append one label line. Do not wait indefinitely for a slow
    # client or query on exit. Own the listener outside the server lifecycle
    # as well, so an interrupted startup/shutdown cannot retain the port.
    config = uvicorn.Config(
        app, host=host, port=port, log_level="warning", timeout_graceful_shutdown=2
    )
    with config.bind_socket() as listener:
        print(f"All4One viewer: http://{url_host}:{listener.getsockname()[1]}/", flush=True)
        print(
            "Read-only local evidence. Press Ctrl-C to stop (up to 2s for active requests).",
            flush=True,
        )
        store = app.state.annotations
        if store is not None:
            print(
                f"Labels by {store.annotator} are appended under {store.directory}; "
                "traces stay read-only.",
                flush=True,
            )
        timer = None
        if open_browser:
            browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
            if ":" in browser_host:
                browser_host = f"[{browser_host}]"
            timer = Timer(
                0.8, webbrowser.open, args=(f"http://{browser_host}:{listener.getsockname()[1]}/",)
            )
            timer.daemon = True
            timer.start()
        try:
            uvicorn.Server(config).run(sockets=[listener])
        finally:
            if timer is not None:
                timer.cancel()
