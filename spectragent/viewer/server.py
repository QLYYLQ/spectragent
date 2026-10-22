"""HTTP presentation of the shared inspection repository.

No runtime and no trace writer: the one thing it writes is label lines, into
the annotation directory it was started with.
"""

from __future__ import annotations

import getpass
import ipaddress
import webbrowser
from pathlib import Path, PurePosixPath
from threading import Timer
from typing import Annotated, Any, Literal

import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from spectragent.annotation import AnnotationError, AnnotationStore, QueueChangedError
from spectragent.core.trace.models import TraceManifest
from spectragent.inspection.repository import RepositoryError, open_repository
from spectragent.viewer.annotations import annotation_router, host_refusal, write_refusal

_LOOPBACK_NAMES = {"localhost"}


def _key(value: str) -> str:
    """A repository-relative identifier in its one canonical spelling.

    Never a filesystem URL or path; ``./run``, ``run/.`` and ``run/`` are all
    ``run``, so one run has one identity (ad-hoc labels are keyed by it).
    """
    path = PurePosixPath(value)
    if not value or path.is_absolute() or ".." in path.parts or "\\" in value or "\x00" in value:
        raise HTTPException(400, "需要根目录内的相对路径（relative repository key）")
    return path.as_posix()


def _image_type(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise HTTPException(422, "记录的截图不是支持的图片格式")


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
        api = request.url.path.startswith("/api/")
        writes = request.method not in {"GET", "HEAD"} and request.url.path.startswith(
            "/api/review/"
        )
        refusal = (host_refusal(request) if api else None) or (
            write_refusal(request) if writes else None
        )
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

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Located problems only: never echo a request's values (they may not
        # even be JSON, such as a NaN box coordinate).
        problems = [
            {"loc": list(error["loc"]), "msg": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ]
        return JSONResponse(status_code=422, content={"detail": problems})

    @app.exception_handler(RepositoryError)
    async def repository_error(request: Request, exc: RepositoryError) -> JSONResponse:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(AnnotationError)
    async def annotation_error(request: Request, exc: AnnotationError) -> JSONResponse:
        status = 409 if isinstance(exc, QueueChangedError) else 400
        return JSONResponse(status_code=status, content={"detail": str(exc)})

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
            raise HTTPException(404, "这一步没有已提交的截图")
        return Response(content=data, media_type=_image_type(data))

    @app.get("/api/marks")
    def marks(key: str, index: Annotated[int, Query(ge=0)]) -> dict[str, Any]:
        return repository.observation_marks(_key(key), index)

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
                503, "查看器页面文件缺失；请在 frontend/viewer 里运行 npm ci && npm run build"
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
        if _is_run_trace(folder):
            raise AnnotationError(
                f"The annotation directory {path} is inside the recorded run {folder}; "
                "labels never go into a trace"
            )
    return AnnotationStore(path, annotator=annotator or getpass.getuser())


def _is_run_trace(folder: Path) -> bool:
    """Whether *folder* holds a run trace (a trace manifest), not just any manifest.json."""
    manifest = folder / "manifest.json"
    if not manifest.is_file():
        return False
    try:
        TraceManifest.model_validate_json(manifest.read_bytes())
    except (OSError, ValidationError):
        return False
    return True


def _is_loopback(host: str) -> bool:
    if host in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


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
    if annotations is not None and not _is_loopback(host):
        raise AnnotationError(
            f"--annotations writes labels, so the viewer must listen on a loopback address; "
            f"--host {host} is not one. Drop --host (or use 127.0.0.1) to label, or drop "
            "--annotations to share a read-only view; reach a remote viewer through SSH forwarding"
        )
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
        store = app.state.annotations
        if store is None:
            print("Read-only local evidence.", flush=True)
        else:
            print(
                f"Traces stay read-only. Labels by {store.annotator} are appended under "
                f"{store.directory}.",
                flush=True,
            )
        print("Press Ctrl-C to stop (up to 2s for active requests).", flush=True)
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
