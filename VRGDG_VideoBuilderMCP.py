"""VRGDG Video Builder — thin stdio MCP server over the Agent REST API.

This is a **thin adapter**: every tool is a one-to-one wrapper around the
``/api/v1/video-builder/...`` endpoints on a running ComfyUI instance. It adds
no generation logic; the ComfyUI instance (which loads ``VRGDG_VideoBuilderAPI``)
does all the work. The server talks to the instance over HTTP and speaks MCP
over stdio, so any MCP client (Hermes Desktop, Claude Desktop, ...) can drive the
Video Builder.

Run (stdio, default):
    python VRGDG_VideoBuilderMCP.py

Configuration (env vars, with sensible defaults for the HERMES instance):
    VRGDG_API_BASE   e.g. http://127.0.0.1:8188/api/v1/video-builder
    VRGDG_BASE_URL   e.g. http://127.0.0.1:8188  (fallback for VRGDG_API_BASE)

The tools return the raw JSON envelope from the REST API (``{"ok": ...}``), so
an agent sees exactly what the HTTP API returns — including ``revision`` and the
``409 REVISION_CONFLICT`` error shape, which it uses to reconcile and retry.
"""

import json
import os
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("vrgdg-video-builder")

_BASE = os.environ.get("VRGDG_API_BASE")
if not _BASE:
    _base_url = os.environ.get("VRGDG_BASE_URL", "http://127.0.0.1:8188")
    _BASE = _base_url.rstrip("/") + "/api/v1/video-builder"


def _http(method, path, body=None):
    """Call the REST API and return the parsed JSON envelope (dict)."""
    url = _BASE + path
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Surface the API's error envelope (e.g. 409 REVISION_CONFLICT) verbatim.
        try:
            return json.loads(exc.read().decode("utf-8"))
        except Exception:
            return {"ok": False, "error": {"code": "HTTP_%s" % exc.code,
                                          "message": str(exc)}}


# ---------------------------------------------------------------------------
# Project lifecycle
# ---------------------------------------------------------------------------
@mcp.tool()
def list_projects() -> dict:
    """List all Video Builder projects."""
    return _http("GET", "/projects")


@mcp.tool()
def create_project(project_name: str) -> dict:
    """Create a new Video Builder project. Returns the project (with id) and revision."""
    return _http("POST", "/projects", {"project_name": project_name})


@mcp.tool()
def get_project(project_id: str) -> dict:
    """Get a project's full normalized state (scenes, story, references, revision)."""
    return _http("GET", "/projects/%s" % project_id)


@mcp.tool()
def delete_project(project_id: str) -> dict:
    """Delete a project."""
    return _http("DELETE", "/projects/%s" % project_id)


@mcp.tool()
def branch_project(project_id: str, new_name: str) -> dict:
    """Create a branch (copy) of a project under a new name."""
    return _http("POST", "/projects/%s/branch" % project_id, {"project_name": new_name})


@mcp.tool()
def get_project_status(project_id: str) -> dict:
    """Get project status: scene totals, render state, active job, revision."""
    return _http("GET", "/projects/%s/status" % project_id)


# ---------------------------------------------------------------------------
# Scenes (optimistic concurrency: pass base_revision to avoid 409)
# ---------------------------------------------------------------------------
@mcp.tool()
def list_scenes(project_id: str) -> dict:
    """List a project's scenes (normalized)."""
    return _http("GET", "/projects/%s/scenes" % project_id)


@mcp.tool()
def get_scene(project_id: str, scene_id: str) -> dict:
    """Get one scene by id (or 1-based ordinal)."""
    return _http("GET", "/projects/%s/scenes/%s" % (project_id, scene_id))


@mcp.tool()
def add_scene(project_id: str, scene: dict, base_revision: int = None) -> dict:
    """Add a scene. `scene` is the normalized scene object. Pass base_revision
    (the current revision) to avoid a 409 REVISION_CONFLICT."""
    body = {"scene": scene}
    if base_revision is not None:
        body["base_revision"] = base_revision
    return _http("POST", "/projects/%s/scenes" % project_id, body)


@mcp.tool()
def patch_scene(project_id: str, scene_id: str, patch: dict, base_revision: int = None) -> dict:
    """Patch a scene (merge semantics: scalar replace, object merge, null clear,
    array replace). Pass base_revision to avoid a 409 REVISION_CONFLICT."""
    body = {"patch": patch}
    if base_revision is not None:
        body["base_revision"] = base_revision
    return _http("PATCH", "/projects/%s/scenes/%s" % (project_id, scene_id), body)


@mcp.tool()
def delete_scene(project_id: str, scene_id: str, base_revision: int = None) -> dict:
    """Delete a scene. Pass base_revision to avoid a 409 REVISION_CONFLICT."""
    body = {}
    if base_revision is not None:
        body["base_revision"] = base_revision
    return _http("DELETE", "/projects/%s/scenes/%s" % (project_id, scene_id), body)


# ---------------------------------------------------------------------------
# Jobs (generation + render bridge)
# ---------------------------------------------------------------------------
@mcp.tool()
def generate_scene(project_id: str, scene_id: str, builder: str,
                   builder_payload: dict, collect: str = "video",
                   timeout_s: int = 900, save_folder: str = "") -> dict:
    """Generate a scene's media via the ComfyUI bridge. `builder` is one of:
    zimage, krea2, krea2_2pass, ernie_image, flux_klein, nb_image, i2v, t2v,
    rtv, minimax_h3, minimax_h3_2pass, minimax_h3_advanced_2pass,
    minimax_h3_3pass, ingredients, flf, id_lora. `collect` is 'video' or 'image'.
    Returns the job (status 'queued'); poll get_job until complete."""
    return _http("POST", "/jobs/generate", {
        "project_id": project_id, "scene_id": scene_id, "builder": builder,
        "builder_payload": builder_payload, "collect": collect,
        "timeout_s": timeout_s, "save_folder": save_folder,
    })


@mcp.tool()
def render_project(project_id: str, dry_run: bool = False,
                  scene_ids: list = None, audio_path: str = "") -> dict:
    """Stitch a project's rendered scene videos into the final video (ffmpeg
    concat + audio mux) and write a durable render log. `dry_run` reports the
    scene count without stitching. Returns the job; poll get_job until complete."""
    body = {"project_id": project_id, "dry_run": dry_run, "audio_path": audio_path}
    if scene_ids:
        body["scene_ids"] = scene_ids
    return _http("POST", "/jobs/render", body)


@mcp.tool()
def list_jobs(project_id: str = "") -> dict:
    """List jobs, optionally filtered by project."""
    path = "/jobs"
    if project_id:
        path += "?project_id=%s" % project_id
    return _http("GET", path)


@mcp.tool()
def get_job(job_id: str) -> dict:
    """Get a job's current state (status, message, result, error). Poll until
    status is 'complete' or 'failed'."""
    return _http("GET", "/jobs/%s" % job_id)


@mcp.tool()
def cancel_job(job_id: str) -> dict:
    """Request cooperative cancellation of a running job."""
    return _http("POST", "/jobs/%s/cancel" % job_id)


# ---------------------------------------------------------------------------
# Media / audio / references (thin wrappers)
# ---------------------------------------------------------------------------
@mcp.tool()
def scan_scene_videos(project_id: str) -> dict:
    """Scan a project for rendered scene videos."""
    return _http("POST", "/projects/%s/media" % project_id, {})


@mcp.tool()
def save_scene_image(project_id: str, scene_id: str, image: dict) -> dict:
    """Save an image as a scene's image."""
    return _http("POST", "/projects/%s/scenes/%s/image" % (project_id, scene_id),
                 {"image": image})


@mcp.tool()
def save_project_audio(project_id: str, audio_path: str) -> dict:
    """Attach a project audio file."""
    return _http("POST", "/projects/%s/audio" % project_id, {"audio_path": audio_path})


@mcp.tool()
def export_project(project_id: str) -> str:
    """Export a project as a ZIP. Returns the ZIP's absolute path on the
    ComfyUI host (the binary is streamed; this tool returns the saved path)."""
    # The REST export streams a ZIP. We fetch it and save to a temp file, then
    # return the path (MCP stdio tools return text/structured data, not raw bytes).
    import tempfile
    req = urllib.request.Request(_BASE + "/projects/%s/export" % project_id)
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = resp.read()
    tmp = os.path.join(tempfile.gettempdir(), "vrgdg_export_%s.zip" % project_id)
    with open(tmp, "wb") as f:
        f.write(data)
    return tmp


def main():
    mcp.run()


if __name__ == "__main__":
    main()
