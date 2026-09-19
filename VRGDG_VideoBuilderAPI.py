"""VRGDG Video Builder — Agent API (thin adapter over the existing Builder).

This module exposes a normalized, agent-facing REST surface under
``/api/v1/video-builder/...`` on top of the existing
``VRGDG_MusicVideoBuilderNodes`` primitives. It does NOT rewrite the Builder.

Design (see ``.hermes/plans/2026-09-19_vrgdg-video-builder-agent-api.md``):

* ``vrgdg_builder_session.json`` stays the lossless canonical store. The API is
  a semantic facade: every mutation = ``deepcopy(existing session)`` -> apply a
  normalized patch -> preserve unknown fields -> existing ``_save_builder_session``
  (which regenerates SRT / context / reference files, then commits the session
  last).
* Project ID is derived from the folder identity (no second ID system). Scene ID
  is ``segment["id"]`` when present, else ``scene_<zero-padded scene_number>``.
* Optimistic concurrency: every mutation carries ``base_revision`` (or an
  ``If-Match`` header); a mismatch vs ``builder_save_revision`` -> 409.
* Merge engine: scalar -> replace; object -> recursive merge; ``null`` -> clear;
  array -> replace; identity-keyed catalog (``references``) -> merge by ID;
  structural collections -> explicit actions; server-owned fields -> rejected;
  unknown legacy fields -> preserved.
* Jobs are transient; render logs are durable.

The pure logic (normalization, merge engine, revision check) lives at module top
level and has NO dependency on the Builder, so it is unit-testable without a live
ComfyUI. Builder-dependent route handlers are defined inside
``_register_video_builder_api`` and import the Builder lazily, mirroring the
existing ``_ensure_music_builder_routes`` pattern.
"""

import copy
import json
import os
import time
import uuid

# ---------------------------------------------------------------------------
# Pure logic (import-safe, no Builder dependency)
# ---------------------------------------------------------------------------

# Server-owned fields that a PATCH must never modify.
_PROJECT_READ_ONLY = {"id", "project_folder", "revision", "status", "scenes", "overlays", "jobs"}
_SCENE_READ_ONLY = {"id", "generation", "job_id"}
_RENDER_READ_ONLY = {"report_json_path", "report_text_path", "active_log_id"}

# Fields that map 1:1 between a normalized scene and a legacy segment.
_SCENE_FIELD_MAP = {
    # normalized key -> legacy segment key
    "label": "label",
    "concept": "concept",
    "scene_summary": "scene_summary",
    "director_note": "director_note",
    "scene_note": "scene_note",
    "timeline_note": "timeline_note",
    "timeline_notes": "timeline_notes",
    "notes": "notes",
    "no_character_present": "no_character_present",
    "lyric_text": "lyric_text",
    "lyric_singers": "lyric_singers",
    "lyric_instrumental": "lyric_instrumental",
    "lyric_no_lip_sync": "lyric_no_lip_sync",
    "lyric_section": "lyric_section",
    "mapped_subjects": "mapped_subjects",
    "subject_reference_mode": "subject_reference_mode",
    "location_reference_mode": "location_reference_mode",
    "t2i_prompt": "t2i_prompt",
    "i2v_prompt": "i2v_prompt",
    "concept_prompt": "concept_prompt",
    "approved_image_path": "approved_image_path",
    "custom_image_path": "custom_image_path",
    "ref_image_path": "ref_image_path",
    "flux_subject_image_path": "flux_subject_image_path",
    "flux_location_image_path": "flux_location_image_path",
    "image_history": "image_history",
    "image_history_index": "image_history_index",
    "video_path": "video_path",
    "video_status": "video_status",
    "video_folder": "video_folder",
    "start": "start",
    "end": "end",
}
# Legacy keys that are normalized away (identity/number) and must not be in metadata.
_SCENE_IDENTITY_KEYS = {"id", "scene_number", "start", "end"}


def project_id_from_folder(project_folder):
    """Stable API project identifier derived from the folder identity."""
    folder = str(project_folder or "").strip().strip('"').strip()
    if not folder:
        return ""
    return os.path.basename(os.path.abspath(folder))


def scene_public_id(segment, index):
    """Public scene id: ``segment["id"]`` when present, else ``scene_<NNN>``."""
    explicit = str(segment.get("id") or "").strip()
    if explicit:
        return explicit
    number = segment.get("scene_number")
    if number in (None, ""):
        number = (index or 0) + 1
    try:
        number = int(number)
    except (TypeError, ValueError):
        number = (index or 0) + 1
    return f"scene_{number:03d}"


def _deep_merge(base, patch):
    """Recursive object merge. ``None`` clears; nested dicts merge; lists replace."""
    for key, value in patch.items():
        if value is None:
            base[key] = None
        elif isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = copy.deepcopy(value)
    return base


def merge_catalog_by_id(base, patch, id_key="id"):
    """Identity-keyed merge for a list of catalog entries (e.g. reference subjects).

    Entries in ``patch`` that carry an ``id`` are merged into the matching base
    entry; unknown ids are appended; base entries not present in ``patch`` survive.
    A ``{"replace": [...]}`` wrapper replaces the whole list.
    """
    if isinstance(patch, dict) and set(patch.keys()) == {"replace"}:
        return copy.deepcopy(patch["replace"])
    if not isinstance(patch, list):
        return base
    result = copy.deepcopy(base) if isinstance(base, list) else []
    index = {}
    for entry in result:
        if isinstance(entry, dict) and entry.get(id_key) is not None:
            index[entry[id_key]] = entry
    for entry in patch:
        if not isinstance(entry, dict):
            result.append(copy.deepcopy(entry))
            continue
        key = entry.get(id_key)
        if key is not None and key in index:
            _deep_merge(index[key], entry)
        else:
            result.append(copy.deepcopy(entry))
            if key is not None:
                index[key] = result[-1]
    return result


def apply_patch(target, patch):
    """Apply a normalized patch to a dict using the merge-engine rules.

    ``null`` clears; nested objects merge recursively; lists replace by default.
    Callers use :func:`merge_catalog_by_id` explicitly for identity-keyed catalogs.
    """
    if not isinstance(patch, dict):
        return target
    return _deep_merge(target, patch)


def check_revision(base_revision, current_revision):
    """Return an error dict on a revision conflict, else ``None``."""
    if base_revision is None:
        return None
    try:
        base = int(base_revision)
    except (TypeError, ValueError):
        return None
    current = int(current_revision or 0)
    if base != current:
        return {
            "code": "REVISION_CONFLICT",
            "message": "Project was modified after the supplied base revision.",
            "details": {"base_revision": base, "current_revision": current},
        }
    return None


def _scene_to_api(segment, index):
    """Normalize a legacy segment into the agent-facing scene shape.

    Known fields are mapped; every other key is preserved under ``metadata`` so a
    round-trip (segment -> scene -> segment) is lossless.
    """
    segment = segment if isinstance(segment, dict) else {}
    out = {
        "id": scene_public_id(segment, index),
        "number": segment.get("scene_number", (index or 0) + 1),
        "label": segment.get("label", ""),
        "timing": {
            "start": float(segment.get("start", 0) or 0),
            "end": float(segment.get("end", 0) or 0),
        },
        "content": {
            "concept": segment.get("concept"),
            "concept_prompt": segment.get("concept_prompt"),
            "scene_summary": segment.get("scene_summary"),
            "director_note": segment.get("director_note"),
            "scene_note": segment.get("scene_note"),
            "timeline_note": segment.get("timeline_note"),
            "timeline_notes": segment.get("timeline_notes"),
            "notes": segment.get("notes"),
        },
        "lyrics": {
            "text": segment.get("lyric_text"),
            "singers": segment.get("lyric_singers", []),
            "instrumental": bool(segment.get("lyric_instrumental")),
            "no_lip_sync": bool(segment.get("lyric_no_lip_sync")),
            "section": segment.get("lyric_section"),
        },
        "subjects": {
            "mapped": segment.get("mapped_subjects", []),
            "character_present": not bool(segment.get("no_character_present")),
        },
        "references": {
            "subject_mode": segment.get("subject_reference_mode"),
            "location_mode": segment.get("location_reference_mode"),
        },
        "prompts": {
            "concept": segment.get("concept_prompt") or segment.get("concept"),
            "image": segment.get("t2i_prompt"),
            "video": segment.get("i2v_prompt"),
        },
        "media": {
            "image_path": segment.get("approved_image_path"),
            "custom_image_path": segment.get("custom_image_path"),
            "ref_image_path": segment.get("ref_image_path"),
            "video_path": segment.get("video_path"),
            "video_status": segment.get("video_status"),
            "video_folder": segment.get("video_folder"),
        },
    }
    out["timing"]["duration"] = max(0.0, out["timing"]["end"] - out["timing"]["start"])
    # Lossless remainder: preserve every key we did not explicitly normalize.
    normalized_keys = set(_SCENE_FIELD_MAP.values()) | _SCENE_IDENTITY_KEYS
    metadata = {
        key: value
        for key, value in segment.items()
        if key not in normalized_keys
    }
    if metadata:
        out["metadata"] = metadata
    return out


def _scene_to_segment(scene, existing_segment=None, target=None):
    """Apply a normalized scene onto a legacy segment (lossless).

    When ``target`` is a dict, changes are merged into it in place and returned;
    otherwise a new segment is built from ``existing_segment`` (deep-copied) so any
    field not present in the normalized scene survives. Only known fields are
    overwritten.
    """
    if target is not None:
        segment = target
    else:
        segment = copy.deepcopy(existing_segment) if isinstance(existing_segment, dict) else {}
    scene = scene if isinstance(scene, dict) else {}

    timing = scene.get("timing") or {}
    if "start" in timing:
        segment["start"] = float(timing["start"])
    if "end" in timing:
        segment["end"] = float(timing["end"])

    content = scene.get("content") or {}
    for norm in ("concept", "concept_prompt", "scene_summary", "director_note",
                 "scene_note", "timeline_note", "timeline_notes", "notes"):
        if norm in content:
            segment[_SCENE_FIELD_MAP[norm]] = content[norm]

    lyrics = scene.get("lyrics") or {}
    if "text" in lyrics:
        segment["lyric_text"] = lyrics["text"]
    if "singers" in lyrics:
        segment["lyric_singers"] = lyrics["singers"]
    if "instrumental" in lyrics:
        segment["lyric_instrumental"] = bool(lyrics["instrumental"])
    if "no_lip_sync" in lyrics:
        segment["lyric_no_lip_sync"] = bool(lyrics["no_lip_sync"])
    if "section" in lyrics:
        segment["lyric_section"] = lyrics["section"]

    subjects = scene.get("subjects") or {}
    if "mapped" in subjects:
        segment["mapped_subjects"] = subjects["mapped"]
    if "character_present" in subjects:
        segment["no_character_present"] = not bool(subjects["character_present"])

    refs = scene.get("references") or {}
    if "subject_mode" in refs:
        segment["subject_reference_mode"] = refs["subject_mode"]
    if "location_mode" in refs:
        segment["location_reference_mode"] = refs["location_mode"]

    prompts = scene.get("prompts") or {}
    if "image" in prompts:
        segment["t2i_prompt"] = prompts["image"]
    if "video" in prompts:
        segment["i2v_prompt"] = prompts["video"]
    if "concept" in prompts:
        segment["concept_prompt"] = prompts["concept"]

    media = scene.get("media") or {}
    for norm, legacy in (
        ("image_path", "approved_image_path"),
        ("custom_image_path", "custom_image_path"),
        ("ref_image_path", "ref_image_path"),
        ("video_path", "video_path"),
        ("video_status", "video_status"),
        ("video_folder", "video_folder"),
    ):
        if norm in media:
            segment[legacy] = media[norm]

    # Preserve the lossless remainder from a previously-normalized scene.
    if isinstance(scene.get("metadata"), dict):
        for key, value in scene["metadata"].items():
            segment[key] = value

    if "number" in scene and isinstance(scene["number"], int):
        segment["scene_number"] = scene["number"]
    if "label" in scene:
        segment["label"] = scene["label"]
    return segment


def session_to_project(session, project_folder):
    """Normalize a loaded Builder session into the agent-facing project shape."""
    session = session if isinstance(session, dict) else {}
    segments = session.get("segments", [])
    if not isinstance(segments, list):
        segments = []
    overlays = session.get("overlay_segments", [])
    if not isinstance(overlays, list):
        overlays = []
    return {
        "id": project_id_from_folder(project_folder),
        "name": os.path.basename(os.path.abspath(str(project_folder or ""))),
        "project_folder": os.path.abspath(str(project_folder or "")),
        "revision": int(session.get("builder_save_revision") or 0),
        "audio": {"path": session.get("audio_path", "")},
        "story": {
            "layer": session.get("builder_story_layer", {}),
            "defaults": session.get("builder_storyboard_defaults", {}),
        },
        "references": session.get("flux_reference_builder", {}),
        "scenes": [_scene_to_api(seg, i) for i, seg in enumerate(segments) if isinstance(seg, dict)],
        "overlays": [_scene_to_api(seg, i) for i, seg in enumerate(overlays) if isinstance(seg, dict)],
        "render": {
            "logs": session.get("render_logs", []),
            "active_log_id": session.get("active_render_log_id", ""),
        },
        "updated_at": session.get("updated"),
    }


def find_scene(session, scene_id=None, scene_number=None):
    """Resolve a scene id or number to its segment (and index) in ``segments``."""
    segments = session.get("segments", [])
    if not isinstance(segments, list):
        return None, None
    for i, seg in enumerate(segments):
        if not isinstance(seg, dict):
            continue
        if scene_id is not None and scene_public_id(seg, i) == str(scene_id):
            return seg, i
        if scene_number is not None:
            try:
                if int(seg.get("scene_number") or -1) == int(scene_number):
                    return seg, i
            except (TypeError, ValueError):
                pass
    return None, None


# ---------------------------------------------------------------------------
# Builder-dependent route registration (deferred; imports Builder lazily)
# ---------------------------------------------------------------------------

_VRGDG_VIDEO_BUILDER_API_REGISTERED = False


def _register_video_builder_api():
    """Register the agent API routes. Safe to call once at ComfyUI startup."""
    global _VRGDG_VIDEO_BUILDER_API_REGISTERED
    if _VRGDG_VIDEO_BUILDER_API_REGISTERED:
        return
    from server import PromptServer  # noqa: WPS433 (deferred on purpose)
    from aiohttp import web  # noqa: WPS433
    server_instance = getattr(PromptServer, "instance", None)
    if server_instance is None:
        return
    _VRGDG_VIDEO_BUILDER_API_REGISTERED = True

    # Import the Builder primitives lazily so this module is import-safe.
    from .VRGDG_MusicVideoBuilderNodes import (
        _new_builder_project,
        _save_builder_project_as,
        _save_builder_session,
        _load_builder_session,
        _list_builder_projects,
        _delete_builder_project,
        _prepare_builder_project_export,
        _import_builder_project_zip,
        _scan_builder_scene_videos,
        _restore_scene_video,
        _save_scene_image,
        _archive_scene_image,
        _extract_video_final_frame_as_scene_image,
        _save_flux_reference_image,
        _save_scene_audio,
        _save_project_audio,
        _trim_scene_audio,
        _prepare_scene_audio_mix,
        _save_builder_render_log,
        _BUILDER_SAVE_LOCK,
    )

    import asyncio

    base = "/api/v1/video-builder"

    # -- helpers -----------------------------------------------------------
    def _resolve(project_id):
        listing = _list_builder_projects()
        for item in listing.get("projects", []):
            if project_id_from_folder(item.get("project_folder")) == str(project_id):
                return item
        return None

    def _ok(data, revision=None):
        payload = {"ok": True, "data": data}
        if revision is not None:
            payload["revision"] = revision
        return web.json_response(payload)

    def _err(code, message, status=400, details=None, current=None):
        body = {
            "ok": False,
            "error": {"code": code, "message": message, "details": details or {}},
        }
        if current is not None:
            body["current"] = current
        return web.json_response(body, status=status)

    async def _read_json(request):
        try:
            return await request.json()
        except Exception:
            return {}

    def _base_revision(payload, request):
        if payload.get("base_revision") is not None:
            return payload.get("base_revision")
        if_match = request.headers.get("If-Match")
        if if_match:
            return if_match.strip('"').strip()
        return None

    # -- project lifecycle -------------------------------------------------
    @server_instance.routes.get(f"{base}/projects")
    async def list_projects(request):
        try:
            result = _list_builder_projects(request.query.get("project_root", ""))
        except Exception as exc:
            return _err("ERROR", str(exc), 500)
        return web.json_response({"ok": True, **result})

    @server_instance.routes.post(f"{base}/projects")
    async def create_project(request):
        payload = await _read_json(request)
        name = str(payload.get("name") or payload.get("project_name") or "").strip()
        try:
            created = _new_builder_project({
                "project_name": name,
                "project_root": payload.get("project_root", ""),
                "project_folder": payload.get("project_folder", ""),
            })
            folder = created["project_folder"]
            # Ensure a canonical session exists so the project is resolvable.
            if not os.path.isfile(created.get("session_path")):
                initial = {
                    "project_folder": folder,
                    "audio_path": "",
                    "segments": [],
                    "overlay_segments": [],
                    "builder_save_revision": 0,
                    "builder_story_layer": {},
                    "builder_storyboard_defaults": {},
                    "flux_reference_builder": {"subjects": [], "locations": []},
                    "render_logs": [],
                    "active_render_log_id": "",
                }
                with open(created["session_path"], "w", encoding="utf-8") as handle:
                    json.dump(initial, handle, indent=2, ensure_ascii=False)
                    handle.write("\n")
        except Exception as exc:
            return _err("ERROR", str(exc), 500)
        return _ok({
            "id": project_id_from_folder(created["project_folder"]),
            "name": os.path.basename(created["project_folder"]),
            "project_folder": created["project_folder"],
            "status": "created",
        }, revision=0)

    @server_instance.routes.get(f"{base}/projects/{{project_id}}")
    async def get_project(request):
        project = _resolve(request.match_info["project_id"])
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        try:
            loaded = await asyncio.to_thread(_load_builder_session, project["project_folder"])
        except Exception as exc:
            return _err("ERROR", str(exc), 500)
        project_view = session_to_project(loaded["session"], project["project_folder"])
        return web.json_response({"ok": True, "data": project_view, "revision": project_view["revision"]})

    @server_instance.routes.delete(f"{base}/projects/{{project_id}}")
    async def delete_project(request):
        project = _resolve(request.match_info["project_id"])
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        try:
            result = _delete_builder_project({"project_folder": project["project_folder"]})
        except Exception as exc:
            return _err("ERROR", str(exc), 500)
        return _ok(result)

    @server_instance.routes.post(f"{base}/projects/{{project_id}}/branch")
    async def branch_project(request):
        payload = await _read_json(request)
        project = _resolve(request.match_info["project_id"])
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        try:
            loaded = await asyncio.to_thread(_load_builder_session, project["project_folder"])
            result = _save_builder_project_as({
                "source_project_folder": project["project_folder"],
                "target_project_folder": payload.get("target_project_folder", ""),
                "project_name": payload.get("name", ""),
                "session": loaded["session"],
                "audio_path": loaded["session"].get("audio_path", ""),
            })
        except Exception as exc:
            return _err("ERROR", str(exc), 500)
        return _ok({
            "id": project_id_from_folder(result["project_folder"]),
            "project_folder": result["project_folder"],
        })

    @server_instance.routes.get(f"{base}/projects/{{project_id}}/export")
    async def export_project(request):
        project = _resolve(request.match_info["project_id"])
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        zip_path = ""
        response = None
        try:
            zip_path, download_name = await asyncio.to_thread(
                _prepare_builder_project_export, project["project_folder"]
            )
            response = web.StreamResponse(status=200, headers={
                "Content-Type": "application/zip",
                "Content-Disposition": f'attachment; filename="{download_name}"',
                "Content-Length": str(os.path.getsize(zip_path)),
                "Cache-Control": "no-store",
            })
            await response.prepare(request)
            with open(zip_path, "rb") as handle:
                while True:
                    chunk = await asyncio.to_thread(handle.read, 1024 * 1024)
                    if not chunk:
                        break
                    await response.write(chunk)
            await response.write_eof()
            return response
        except Exception as exc:
            if response is not None and response.prepared:
                raise
            return _err("ERROR", str(exc), 500)
        finally:
            if zip_path:
                try:
                    os.remove(zip_path)
                except OSError:
                    pass

    @server_instance.routes.post(f"{base}/projects/import")
    async def import_project(request):
        temp_path = ""
        try:
            reader = await request.multipart()
            requested_name = ""
            upload = None
            async for part in reader:
                if part.name == "project_name":
                    requested_name = (await part.text()).strip()
                elif part.name == "project_zip":
                    suffix = os.path.splitext(part.filename or "project.zip")[1] or ".zip"
                    temp_handle = __import__("tempfile").NamedTemporaryFile(
                        prefix="vrgdg_builder_api_import_", suffix=suffix, delete=False
                    )
                    temp_path = temp_handle.name
                    upload = temp_handle
                    try:
                        while True:
                            chunk = await part.read_chunk(size=1024 * 1024)
                            if not chunk:
                                break
                            upload.write(chunk)
                    finally:
                        upload.close()
            if not temp_path:
                return _err("BAD_REQUEST", "project_zip part was not provided.", 400)
            result = await asyncio.to_thread(_import_builder_project_zip, temp_path, requested_name)
        except Exception as exc:
            return _err("ERROR", str(exc), 500)
        finally:
            if temp_path:
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
        return _ok(result)

    # -- mutation helper ---------------------------------------------------
    async def _mutate_project(request, apply_fn):
        """Load session, check revision, apply, bump revision, save. Returns a response.

        Holds ``_BUILDER_SAVE_LOCK`` across the read-apply-save sequence so
        concurrent mutations serialize. The load/apply/save are called directly
        (synchronously, on the event-loop thread) — matching the existing Builder
        routes — so ``_save_builder_session``'s internal lock acquisition is
        reentrant and safe (NOT via ``asyncio.to_thread``, which would deadlock
        on a lock the event-loop thread still holds).
        """
        project = _resolve(request.match_info["project_id"])
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        payload = await _read_json(request)
        with _BUILDER_SAVE_LOCK:
            loaded = _load_builder_session(project["project_folder"])
            session = loaded["session"]
            current = int(session.get("builder_save_revision") or 0)
            base = _base_revision(payload, request)
            conflict = check_revision(base, current)
            if conflict:
                return _err("REVISION_CONFLICT", conflict["message"], 409,
                           details=conflict["details"], current={"revision": current})
            candidate = copy.deepcopy(session)
            try:
                apply_fn(candidate, payload)
            except KeyError as exc:
                return _err("READ_ONLY_FIELD", f"Field '{exc}' is server-managed.", 400)
            candidate["builder_save_revision"] = current + 1
            saved = _save_builder_session(
                {"project_folder": project["project_folder"], "session": candidate,
                 "audio_path": candidate.get("audio_path", "")}
            )
        return _ok(saved, revision=current + 1)

    # -- scene CRUD --------------------------------------------------------
    @server_instance.routes.get(f"{base}/projects/{{project_id}}/scenes")
    async def list_scenes(request):
        project = _resolve(request.match_info["project_id"])
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        loaded = await asyncio.to_thread(_load_builder_session, project["project_folder"])
        segments = loaded["session"].get("segments", [])
        return _ok([_scene_to_api(s, i) for i, s in enumerate(segments) if isinstance(s, dict)])

    @server_instance.routes.get(f"{base}/projects/{{project_id}}/scenes/{{scene_id}}")
    async def get_scene(request):
        project = _resolve(request.match_info["project_id"])
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        loaded = await asyncio.to_thread(_load_builder_session, project["project_folder"])
        seg, _ = find_scene(loaded["session"], scene_id=request.match_info["scene_id"])
        if seg is None:
            return _err("SCENE_NOT_FOUND", "Scene not found.", 404)
        return _ok(_scene_to_api(seg, 0))

    @server_instance.routes.post(f"{base}/projects/{{project_id}}/scenes")
    async def create_scene(request):
        payload = await _read_json(request)
        scene = payload.get("scene") or {}

        def apply(candidate, _payload):
            seg = _scene_to_segment(scene, {"start": 0.0, "end": 0.0})
            segments = candidate.setdefault("segments", [])
            if not isinstance(segments, list):
                segments = []
                candidate["segments"] = segments
            segments.append(seg)

        return await _mutate_project(request, apply)

    @server_instance.routes.patch(f"{base}/projects/{{project_id}}/scenes/{{scene_id}}")
    async def patch_scene(request):
        payload = await _read_json(request)
        patch = payload.get("patch") or {}

        def apply(candidate, _payload):
            seg, _ = find_scene(candidate, scene_id=request.match_info["scene_id"])
            if seg is None:
                raise KeyError("scene")
            _scene_to_segment(patch, None, target=seg)  # merge onto existing segment in place

        return await _mutate_project(request, apply)

    @server_instance.routes.delete(f"{base}/projects/{{project_id}}/scenes/{{scene_id}}")
    async def delete_scene(request):
        def apply(candidate, _payload):
            seg, idx = find_scene(candidate, scene_id=request.match_info["scene_id"])
            if seg is None:
                raise KeyError("scene")
            candidate["segments"].pop(idx)

        return await _mutate_project(request, apply)

    # -- status -----------------------------------------------------------
    @server_instance.routes.get(f"{base}/projects/{{project_id}}/status")
    async def project_status(request):
        project = _resolve(request.match_info["project_id"])
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        loaded = await asyncio.to_thread(_load_builder_session, project["project_folder"])
        session = loaded["session"]
        segments = session.get("segments", [])
        total = len([s for s in segments if isinstance(s, dict)])
        complete = len([s for s in segments if isinstance(s, dict) and s.get("video_status") == "done"])
        logs = session.get("render_logs", [])
        active_log = session.get("active_render_log_id", "")
        status = "ready"
        if active_log and logs:
            top = next((l for l in logs if l.get("id") == active_log), None)
            if top and top.get("status") == "running":
                status = "rendering"
        return _ok({
            "project_id": project_id_from_folder(project["project_folder"]),
            "status": status,
            "scenes": {"total": total, "complete": complete, "pending": total - complete},
            "active_job": {"id": active_log} if active_log else None,
            "final_video": (logs[0].get("final_video_path") if logs else None),
            "revision": int(session.get("builder_save_revision") or 0),
        })

    # -- media / audio / references (thin wrappers) ------------------------
    def _media_wrapper(fn, extra=None):
        async def handler(request):
            payload = await _read_json(request)
            project = _resolve(request.match_info.get("project_id", ""))
            if project:
                payload["project_folder"] = project["project_folder"]
            if extra:
                payload.update(extra(request))
            try:
                result = await asyncio.to_thread(fn, payload)
            except Exception as exc:
                return _err("ERROR", str(exc), 500)
            return _ok(result)
        return handler

    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/media"
    )(_media_wrapper(lambda p: _scan_builder_scene_videos(p.get("project_folder", ""))))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/scenes/{{scene_id}}/media/video/restore"
    )(_media_wrapper(_restore_scene_video))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/scenes/{{scene_id}}/image"
    )(_media_wrapper(_save_scene_image))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/scenes/{{scene_id}}/media/archive"
    )(_media_wrapper(_archive_scene_image))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/scenes/{{scene_id}}/media/frame"
    )(_media_wrapper(_extract_video_final_frame_as_scene_image))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/references"
    )(_media_wrapper(_save_flux_reference_image))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/audio"
    )(_media_wrapper(_save_project_audio))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/scenes/{{scene_id}}/audio"
    )(_media_wrapper(_save_scene_audio))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/scenes/{{scene_id}}/audio/trim"
    )(_media_wrapper(_trim_scene_audio))
    server_instance.routes.post(
        f"{base}/projects/{{project_id}}/audio/mix"
    )(_media_wrapper(_prepare_scene_audio_mix))

    print(f"[VRGDG Video Builder API] registered under {base}/")


# Compatibility alias for the registration entry point.
ensure_video_builder_api_routes = _register_video_builder_api

# Register at import time (mirrors the other route modules). The function is a
# no-op when PromptServer.instance is not yet available; it re-runs on the next
# call, so ComfyUI's deferred startup still picks the routes up.
try:
    _register_video_builder_api()
except Exception as _exc:  # pragma: no cover - startup guard
    print(f"[VRGDG Video Builder API] deferred registration: {_exc}")
