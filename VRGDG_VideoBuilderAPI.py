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
import urllib.error
import urllib.request
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


def _find_segment(session, scene_id):
    """Return the segment dict for a scene id (exact or ordinal), or None."""
    segments = session.get("segments", []) or []
    if not segments:
        return None
    target = str(scene_id)
    for seg in segments:
        if str(seg.get("id", "")) == target:
            return seg
    # ordinal fallback: 1-based index
    idx = int(target) - 1 if target.isdigit() else -1
    if 0 <= idx < len(segments):
        return segments[idx]
    return None


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
# Job layer (transient; render logs are the durable record)
# ---------------------------------------------------------------------------
import threading

_JOBS = {}
_JOBS_LOCK = threading.Lock()
_JOBS_FILE_NAME = "vrgdg_jobs.json"


def _new_job(project_id, operation, scene_id=None, payload=None):
    job = {
        "id": "job_" + uuid.uuid4().hex[:12],
        "project_id": project_id,
        "scene_id": scene_id,
        "operation": operation,
        "status": "queued",
        "progress": 0.0,
        "message": None,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "started_at": None,
        "completed_at": None,
        "comfyui_prompt_id": None,
        "result": None,
        "error": None,
        "cancel_requested": False,
    }
    with _JOBS_LOCK:
        _JOBS[job["id"]] = job
    return job


def _update_job(job_id, **fields):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        if not job:
            return None
        job.update(fields)
        if fields.get("status") in ("complete", "failed", "cancelled") and not job.get("completed_at"):
            job["completed_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if fields.get("status") == "running" and not job.get("started_at"):
            job["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        return dict(job)


def _get_job(job_id):
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _list_jobs(project_id=None):
    with _JOBS_LOCK:
        jobs = [dict(j) for j in _JOBS.values()]
    if project_id:
        jobs = [j for j in jobs if j["project_id"] == project_id]
    jobs.sort(key=lambda j: j.get("created_at") or "", reverse=True)
    return jobs


# ---------------------------------------------------------------------------
# ComfyUI generation bridge (reuses the existing build_*_api_prompt functions)
# ---------------------------------------------------------------------------
def _comfyui_base_url():
    port = os.environ.get("VRGDG_COMFYUI_PORT") or os.environ.get("COMFYUI_PORT")
    if not port:
        try:
            from server import PromptServer
            port = getattr(PromptServer.instance, "port", None)
        except Exception:
            port = None
    return "http://127.0.0.1:%s" % (port or 8188)


def _comfyui_get(path, timeout=10):
    req = urllib.request.Request(_comfyui_base_url() + path)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _comfyui_post(path, body, timeout=30):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(_comfyui_base_url() + path, data=data,
                                headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # Surface ComfyUI's error body (node_errors / missing_node_type) so the
        # job error is actionable instead of a bare status line.
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"status": exc.code}
        msg = detail.get("error", {}).get("message") or detail.get("message") or str(exc)
        node_errors = detail.get("node_errors")
        raise RuntimeError("ComfyUI %s: %s%s" % (path, msg,
                           (" | node_errors: %s" % node_errors) if node_errors else ""))


def _comfyui_queue_prompt(prompt, client_id=None):
    body = {"prompt": prompt}
    if client_id:
        body["client_id"] = client_id
    res = _comfyui_post("/prompt", body)
    return res.get("prompt_id") or res.get("id")


def _comfyui_wait_history(prompt_id, timeout_s, poll_s=2.0, job_id=None):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if job_id:
            j = _get_job(job_id)
            if j and j["cancel_requested"]:
                raise RuntimeError("cancelled")
        try:
            item = _comfyui_get("/history/%s" % prompt_id)
        except Exception:
            item = None
        if item:
            entry = next(iter(item.values()), {})
            status = entry.get("status", {}) or {}
            if status.get("status_str") == "error":
                raise RuntimeError("ComfyUI execution error: %s" % (status.get("messages") or status))
            if status.get("completed") or status.get("status_str") in ("success", "success "):
                return entry
        time.sleep(poll_s)
    raise TimeoutError("ComfyUI prompt %s did not complete within %ss" % (prompt_id, timeout_s))


# Builder whitelist: name -> function in VRGDG_WorkflowRunnerNodes.
# The agent supplies the builder name + builder_payload; the bridge calls the
# existing builder (which loads the correct template and patches it) and queues
# the resulting ComfyUI API prompt. This keeps the bridge engine-agnostic and
# reuses the exact generation path the Video Builder UI uses.
_BUILDER_WHITELIST = {
    "zimage": "_build_zimage_api_prompt",
    "krea2": "_build_krea2_api_prompt",
    "krea2_2pass": "_build_krea2_2pass_api_prompt",
    "ernie_image": "_build_ernie_image_api_prompt",
    "flux_klein": "_build_flux_klein_api_prompt",
    "nb_image": "_build_nb_image_api_prompt",
    "i2v": "_build_i2v_api_prompt",
    "t2v": "_build_t2v_api_prompt",
    "rtv": "_build_rtv_api_prompt",
    "minimax_h3": "_build_minimax_h3_api_prompt",
    "minimax_h3_2pass": "_build_minimax_h3_2pass_api_prompt",
    "minimax_h3_advanced_2pass": "_build_minimax_h3_advanced_2pass_api_prompt",
    "minimax_h3_3pass": "_build_minimax_h3_3pass_api_prompt",
    "ingredients": "_build_ingredients_api_prompt",
    "flf": "_build_flf_api_prompt",
    "id_lora": "_build_id_lora_api_prompt",
}


# ---------------------------------------------------------------------------
# Context-aware imports for the Builder / Runner modules.
# In the live ComfyUI instance this module is a package submodule, so relative
# imports work. In the standalone unit-test context it is loaded as a top-level
# module, so we fall back to absolute imports.
# ---------------------------------------------------------------------------
def _import_builder():
    try:
        from . import VRGDG_MusicVideoBuilderNodes as m
    except ImportError:
        import VRGDG_MusicVideoBuilderNodes as m
    return m


def _import_runner():
    try:
        from . import VRGDG_WorkflowRunnerNodes as m
    except ImportError:
        import VRGDG_WorkflowRunnerNodes as m
    return m


def _build_api_prompt(builder_name, builder_payload):
    W = _import_runner()
    funcs = {
        "zimage": W._build_zimage_api_prompt, "krea2": W._build_krea2_api_prompt,
        "krea2_2pass": W._build_krea2_2pass_api_prompt,
        "ernie_image": W._build_ernie_image_api_prompt,
        "flux_klein": W._build_flux_klein_api_prompt,
        "nb_image": W._build_nb_image_api_prompt,
        "i2v": W._build_i2v_api_prompt, "t2v": W._build_t2v_api_prompt,
        "rtv": W._build_rtv_api_prompt,
        "minimax_h3": W._build_minimax_h3_api_prompt,
        "minimax_h3_2pass": W._build_minimax_h3_2pass_api_prompt,
        "minimax_h3_advanced_2pass": W._build_minimax_h3_advanced_2pass_api_prompt,
        "minimax_h3_3pass": W._build_minimax_h3_3pass_api_prompt,
        "ingredients": W._build_ingredients_api_prompt,
        "flf": W._build_flf_api_prompt,
        "id_lora": W._build_id_lora_api_prompt,
    }
    fn = funcs.get(builder_name)
    if not fn:
        raise ValueError("unknown builder: %s (known: %s)" % (builder_name, sorted(funcs)))
    return fn(builder_payload or {})


def _extract_saved_path(res):
    if isinstance(res, str):
        return res
    if isinstance(res, dict):
        for key in ("path", "saved_path", "file_path", "output_path", "video_path"):
            v = res.get(key)
            if isinstance(v, str) and v:
                return v
        for key in ("image", "video", "file"):
            v = res.get(key)
            if isinstance(v, str) and v:
                return v
    return None


_VIDEO_EXTS = (".mp4", ".mov", ".webm", ".mkv", ".avi")


def _find_newest_video(folder, since=None):
    """Return the newest video file in a folder (optionally newer than `since` ts)."""
    if not folder or not os.path.isdir(folder):
        return None
    candidates = []
    for name in os.listdir(folder):
        if name.lower().endswith(_VIDEO_EXTS):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                candidates.append((os.path.getmtime(path), path))
    if not candidates:
        return None
    if since is not None:
        recent = [c for c in candidates if c[0] >= since]
        if recent:
            candidates = recent
    candidates.sort(reverse=True)
    return candidates[0][1]


def _run_generation_job(job_id, project_id, scene_id, builder, builder_payload,
                       timeout_s, collect, save_folder):
    """Worker (runs in a thread). Build -> queue -> poll -> collect/save -> persist."""
    B = _import_builder()
    W = _import_runner()
    _load_builder_session = B._load_builder_session
    _save_builder_session = B._save_builder_session
    _BUILDER_SAVE_LOCK = B._BUILDER_SAVE_LOCK
    _save_generated_image = W._save_generated_image
    _collect_scene_video = W._collect_scene_video
    try:
        _update_job(job_id, status="running", message="building prompt")
        built = _build_api_prompt(builder, builder_payload)
        api_prompt = built["prompt"]
        _update_job(job_id, message="queued to ComfyUI")
        prompt_id = _comfyui_queue_prompt(api_prompt)
        _update_job(job_id, comfyui_prompt_id=prompt_id, message="waiting for ComfyUI")
        history = _comfyui_wait_history(prompt_id, timeout_s, job_id=job_id)
        _update_job(job_id, message="collecting output")

        result = {"prompt_id": prompt_id}
        saved_path = None
        if collect == "image":
            # Find the first output image in the history outputs.
            image = None
            outputs = history.get("outputs", {}) or {}
            for node_out in outputs.values():
                for entry in (node_out.get("images") or []):
                    image = entry
                    break
                if image:
                    break
            if image:
                res = _save_generated_image({"image": image, "save_folder": save_folder})
                saved_path = _extract_saved_path(res)
            result["image"] = image
        elif collect == "video":
            # The i2v/t2v/rtv builders set output_folder; the produced video
            # file is written there with a runtime name. Discover the newest
            # video file, then collect it into the project's rendered folder.
            output_folder = built.get("output_folder")
            video_file = _find_newest_video(output_folder)
            if not video_file:
                raise RuntimeError("no video file found in output folder %s" % output_folder)
            res = _collect_scene_video({
                "source_path": video_file, "project_folder": project_id,
            })
            saved_path = res.get("video_path")
            result["video"] = saved_path
            result["source_video"] = video_file

        # Persist the generated media into the session (lossless, locked).
        if saved_path and scene_id:
            with _BUILDER_SAVE_LOCK:
                loaded = _load_builder_session(project_id)
                session = loaded["session"]
                seg = _find_segment(session, scene_id)
                if seg is not None:
                    if collect == "image":
                        seg["image_path"] = saved_path
                    else:
                        seg["video_path"] = saved_path
                session["builder_save_revision"] = int(session.get("builder_save_revision") or 0) + 1
                _save_builder_session({
                    "project_folder": project_id, "session": session,
                    "audio_path": session.get("audio_path", ""),
                })
                result["revision"] = session.get("builder_save_revision")

        _update_job(job_id, status="complete", progress=1.0, message="complete",
                   result=result)
    except Exception as exc:
        _update_job(job_id, status="failed", message="failed", error=str(exc))


def _run_render_job(job_id, project_id, dry_run, scene_ids, audio_path,
                    audio_start, audio_duration):
    """Worker (runs in a thread). Gather scene videos -> stitch -> render log."""
    B = _import_builder()
    W = _import_runner()
    _load_builder_session = B._load_builder_session
    _save_builder_render_log = B._save_builder_render_log
    _BUILDER_SAVE_LOCK = B._BUILDER_SAVE_LOCK
    _stitch_scene_videos = W._stitch_scene_videos
    try:
        _update_job(job_id, status="running", message="gathering scene videos")
        with _BUILDER_SAVE_LOCK:
            loaded = _load_builder_session(project_id)
        session = loaded["session"]
        segments = session.get("segments", []) or []
        if scene_ids:
            wanted = {str(s) for s in scene_ids}
            segments = [s for s in segments if str(s.get("id")) in wanted]
        scene_paths = [s.get("video_path") for s in segments if s.get("video_path")]
        if not scene_paths:
            raise ValueError("no rendered scene videos found for project %s" % project_id)

        result = {"scene_count": len(scene_paths), "scene_paths": scene_paths}
        if dry_run:
            _update_job(job_id, status="complete", progress=1.0, message="dry run",
                       result=result)
            return

        _update_job(job_id, message="stitching")
        res = _stitch_scene_videos({
            "scene_paths": scene_paths, "project_folder": project_id,
            "audio_path": audio_path, "audio_start": audio_start,
            "audio_duration": audio_duration,
        })
        final_path = res.get("final_video_path")
        result["final_video"] = final_path
        result["scene_count"] = res.get("scene_count", len(scene_paths))

        # Durable render log (this also persists the session's render_logs).
        with _BUILDER_SAVE_LOCK:
            _save_builder_render_log({
                "project_folder": project_id,
                "log": {
                    "id": "render_%s" % int(time.time()),
                    "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "scene_count": len(scene_paths),
                    "final_video_path": final_path,
                    "status": "complete",
                },
            })
        # Re-read revision for the response (render log already saved the session).
        with _BUILDER_SAVE_LOCK:
            loaded = _load_builder_session(project_id)
            result["revision"] = loaded["session"].get("builder_save_revision")

        _update_job(job_id, status="complete", progress=1.0, message="complete",
                   result=result)
    except Exception as exc:
        _update_job(job_id, status="failed", message="failed", error=str(exc))


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

    # -- jobs (generation + render bridge) --------------------------------
    import threading

    def _spawn(worker, *args):
        t = threading.Thread(target=worker, args=args, daemon=True)
        t.start()
        return t

    @server_instance.routes.post(f"{base}/jobs/generate")
    async def job_generate(request):
        payload = await _read_json(request)
        project_id = request.match_info.get("project_id", "") or payload.get("project_id", "")
        project = _resolve(project_id)
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        builder = str(payload.get("builder", "") or "").strip()
        if not builder:
            return _err("MISSING_FIELD", "builder is required.", 400)
        if builder not in _BUILDER_WHITELIST:
            return _err("UNKNOWN_BUILDER",
                       "unknown builder: %s (known: %s)" % (builder, sorted(_BUILDER_WHITELIST)),
                       400)
        scene_id = payload.get("scene_id")
        collect = str(payload.get("collect", "video") or "video").strip().lower()
        if collect not in ("video", "image"):
            return _err("MISSING_FIELD", "collect must be 'video' or 'image'.", 400)
        timeout_s = int(payload.get("timeout_s", 900) or 900)
        save_folder = payload.get("save_folder", "")
        job = _new_job(project_id, "generate", scene_id, payload)
        _spawn(_run_generation_job, job["id"], project["project_folder"], scene_id, builder,
               payload.get("builder_payload", {}), timeout_s, collect, save_folder)
        return _ok(_get_job(job["id"]))

    @server_instance.routes.post(f"{base}/jobs/render")
    async def job_render(request):
        payload = await _read_json(request)
        project_id = request.match_info.get("project_id", "") or payload.get("project_id", "")
        project = _resolve(project_id)
        if not project:
            return _err("PROJECT_NOT_FOUND", "Project not found.", 404)
        dry_run = bool(payload.get("dry_run", False))
        scene_ids = payload.get("scene_ids") or []
        audio_path = payload.get("audio_path", "")
        audio_start = float(payload.get("audio_start", 0) or 0)
        audio_duration = float(payload.get("audio_duration", 0) or 0)
        job = _new_job(project_id, "render", None, payload)
        _spawn(_run_render_job, job["id"], project["project_folder"], dry_run, scene_ids,
               audio_path, audio_start, audio_duration)
        return _ok(_get_job(job["id"]))

    @server_instance.routes.get(f"{base}/jobs")
    async def job_list(request):
        project_id = request.query.get("project_id", "")
        jobs = _list_jobs(project_id or None)
        return web.json_response({"ok": True, "jobs": jobs})

    @server_instance.routes.get(f"{base}/jobs/{{job_id}}")
    async def job_status(request):
        job = _get_job(request.match_info["job_id"])
        if not job:
            return _err("JOB_NOT_FOUND", "Job not found.", 404)
        return _ok(job)

    @server_instance.routes.post(f"{base}/jobs/{{job_id}}/cancel")
    async def job_cancel(request):
        job = _get_job(request.match_info["job_id"])
        if not job:
            return _err("JOB_NOT_FOUND", "Job not found.", 404)
        if job["status"] in ("complete", "failed", "cancelled"):
            return _err("JOB_DONE", "Job already finished.", 409)
        _update_job(job["id"], cancel_requested=True)
        return _ok(_get_job(job["id"]))

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
