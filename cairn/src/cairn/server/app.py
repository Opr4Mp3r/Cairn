from contextlib import asynccontextmanager
import asyncio
import json
import re
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from cairn import __version__
from cairn.server import db
from cairn.server.routers import audits, export, hints, intents, projects, settings, managed

STATIC_DIR = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.configure(db.DEFAULT_DB)
    service = getattr(app.state, "managed_service", None)
    if service is not None:
        service.start()
    try:
        yield
    finally:
        if service is not None:
            await asyncio.to_thread(service.close)


app = FastAPI(
    title="Cairn",
    description="Fact-graph based collaborative exploration protocol",
    version=__version__,
    lifespan=lifespan,
)


@app.middleware("http")
async def fence_runtime(request, call_next):
    from cairn.server import runtime
    fence_token = None
    match = re.match(r"/projects/([^/]+)(/.*)?$", request.url.path)
    if match and request.method not in ("GET", "HEAD", "OPTIONS"):
        project_id, suffix = match.group(1), match.group(2) or ""
        if suffix not in ("", "/title", "/status", "/audit/budget") and not suffix.startswith("/runtime/"):
            try:
                with db.get_conn() as conn:
                    data = runtime.load(conn, project_id)
                    if data is not None:
                        token = request.headers.get("X-Cairn-Submission-Token")
                        preparing = token is not None and (data["state"] == "preparing" or suffix == "/audit/start")
                        if token is not None and data["state"] == "resuming":
                            metadata = conn.execute("SELECT metadata_json FROM audits WHERE project_id=?", (project_id,)).fetchone()
                            preparing = metadata is not None and json.loads(metadata[0])["started_at"] is None
                        if preparing:
                            from cairn.server.submissions import creation_owner
                            if creation_owner(conn, token)["project_id"] != project_id:
                                raise HTTPException(403, "Submission does not own this preparation")
                        else:
                            if request.headers.get("X-Cairn-Generation") != str(data["generation"]):
                                raise HTTPException(409, "Runtime generation changed; old attempt cannot write")
                            if not (data["state"] == "finished" and suffix.endswith("/review")):
                                runtime.require_running(conn, project_id)
                        fence_token = runtime.REQUEST_FENCE.set((project_id,
                            request.headers.get("X-Cairn-Generation"), token if preparing else None, suffix))
            except HTTPException as exc:
                if fence_token is not None:
                    runtime.REQUEST_FENCE.reset(fence_token)
                return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)
    try:
        return await call_next(request)
    finally:
        if fence_token is not None:
            runtime.REQUEST_FENCE.reset(fence_token)

app.include_router(settings.router)
app.include_router(projects.router)
app.include_router(hints.router)
app.include_router(intents.router)
app.include_router(export.router)
app.include_router(audits.router)
app.include_router(managed.router)


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
