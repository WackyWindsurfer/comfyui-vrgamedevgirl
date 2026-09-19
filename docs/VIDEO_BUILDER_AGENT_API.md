# Video Builder Agent API

A normalized, agent-facing REST surface for driving the **AI Video Builder** from an
external agent (Hermes Desktop, Claude, ChatGPT, custom Python agents, ...) without
touching the browser.

The API is a **thin adapter over the existing Builder** (`VRGDG_MusicVideoBuilderNodes.py`).
It does not rewrite the Builder; `vrgdg_builder_session.json` remains the lossless
canonical store. Every mutation is `deepcopy(existing session)` → apply a normalized
patch → preserve unknown fields → the existing `_save_builder_session()` (which
regenerates SRT / context / reference files, then commits the session last).

Implemented in `VRGDG_VideoBuilderAPI.py`; routes are registered on the ComfyUI
`PromptServer` at startup under:

```
/api/v1/video-builder/
```

> **Status:** Phase 1 (project/scene CRUD, status, media/audio/reference wrappers,
> export/import) is complete and live-verified. The job layer, the ComfyUI
> generation bridge, and final-render orchestration are Phase 2.

---

## Core concepts

- **Project ID** is derived from the folder identity
  (`basename(project_folder)`); there is no second ID system.
- **Scene ID** is `segment["id"]` when present, else `scene_<zero-padded scene_number>`
  (e.g. `scene_003`).
- **Optimistic concurrency:** every mutating request carries `base_revision` (or an
  `If-Match` header). A mismatch against `builder_save_revision` returns
  **409 `REVISION_CONFLICT`** with the current revision, so an agent can reconcile
  without an extra GET.
- **Merge engine:** scalar → replace; object → recursive merge; `null` → clear;
  array → replace; identity-keyed catalog (`references`) → merge by ID; structural
  collections → explicit actions; server-owned fields → rejected
  (`400 READ_ONLY_FIELD`); unknown legacy fields → **preserved**.
- **Jobs are transient; render logs are durable.** Job progress never bumps the
  project revision; durable render-log checkpoints do.
- **No arbitrary filesystem paths from the agent** — `project_id` resolves
  server-side to `project_folder` once.

---

## Envelopes

Successful mutation:

```json
{ "ok": true, "data": { }, "revision": 15 }
```

Error:

```json
{
  "ok": false,
  "error": {
    "code": "REVISION_CONFLICT",
    "message": "Project was modified after the supplied base revision.",
    "details": { "base_revision": 14, "current_revision": 16 }
  },
  "current": { "revision": 16 }
}
```

Async operations (Phase 2) return `{ "ok": true, "job": { } }`. Binary export is
returned as the raw ZIP HTTP response (not wrapped).

---

## Project

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/projects` | List projects (`?project_root=` optional) |
| `POST` | `/projects` | Create project — `{ "name": "..." , "project_root": "optional" }` |
| `GET` | `/projects/{id}` | Read the normalized project + `revision` |
| `DELETE` | `/projects/{id}` | Delete project |
| `POST` | `/projects/{id}/branch` | Branch / save-as — `{ "name": "...", "target_project_folder": "optional" }` |
| `GET` | `/projects/{id}/export` | Download the portable project ZIP |
| `POST` | `/projects/import` | Import a project ZIP (multipart: `project_name`, `project_zip`) |

`GET /projects/{id}` returns the normalized project:

```json
{
  "ok": true,
  "data": {
    "id": "my-video",
    "name": "my-video",
    "project_folder": "D:\\...\\output\\my-video",
    "revision": 14,
    "audio": { "path": "..." },
    "story": { "layer": { }, "defaults": { } },
    "references": { "subjects": [], "locations": [] },
    "scenes": [ ],
    "overlays": [ ],
    "render": { "logs": [], "active_log_id": "" },
    "updated_at": 1737000000
  },
  "revision": 14
}
```

## Scenes

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/projects/{id}/scenes` | List normalized scenes |
| `POST` | `/projects/{id}/scenes` | Add scene — `{ "base_revision": N, "scene": { } }` |
| `GET` | `/projects/{id}/scenes/{scene_id}` | Inspect one scene |
| `PATCH` | `/projects/{id}/scenes/{scene_id}` | Modify scene — `{ "base_revision": N, "patch": { } }` |
| `DELETE` | `/projects/{id}/scenes/{scene_id}` | Remove scene — `{ "base_revision": N }` |

A normalized scene (agent-facing shape):

```json
{
  "id": "scene_003",
  "number": 3,
  "label": "Neon Street",
  "timing": { "start": 12.5, "end": 18.0, "duration": 5.5 },
  "content": {
    "concept": null, "concept_prompt": null, "scene_summary": null,
    "director_note": "fast", "scene_note": "energetic",
    "timeline_note": null, "timeline_notes": [], "notes": null
  },
  "lyrics": { "text": "hello", "singers": ["Alice"], "instrumental": false, "no_lip_sync": false, "section": null },
  "subjects": { "mapped": ["Alice"], "character_present": true },
  "references": { "subject_mode": "reference", "location_mode": "reference" },
  "prompts": { "concept": null, "image": "a woman", "video": "she walks" },
  "media": { "image_path": null, "video_path": null, "video_status": null, "video_folder": null },
  "metadata": { }
}
```

`metadata` is the **lossless remainder**: any Builder field the API does not
explicitly normalize is preserved here and written back on save, so a newer Builder
field is never silently erased by an older API client.

### Scene patch example

```json
{
  "base_revision": 14,
  "patch": {
    "timing": { "start": 12.5, "end": 18.25 },
    "content": { "concept_prompt": "Woman walks through neon rain" },
    "prompts": { "image": "..." }
  }
}
```

The patch merges onto the existing segment (objects merge, arrays replace, `null`
clears); everything else survives.

## Status

`GET /projects/{id}/status`

```json
{
  "ok": true,
  "data": {
    "project_id": "my-video",
    "status": "ready",
    "scenes": { "total": 10, "complete": 5, "pending": 5 },
    "active_job": { "id": "render_20260919_120000" } ,
    "final_video": null,
    "revision": 16
  }
}
```

`status` is `ready` or `rendering` (when the active render log is `running`).

## Media / audio / references (thin wrappers)

| Method | Path | Backing Builder function |
|---|---|---|
| `POST` | `/projects/{id}/media` | `_scan_builder_scene_videos` |
| `POST` | `/projects/{id}/scenes/{scene_id}/media/video/restore` | `_restore_scene_video` (duration-mismatch confirm) |
| `POST` | `/projects/{id}/scenes/{scene_id}/image` | `_save_scene_image` |
| `POST` | `/projects/{id}/scenes/{scene_id}/media/archive` | `_archive_scene_image` |
| `POST` | `/projects/{id}/scenes/{scene_id}/media/frame` | `_extract_video_final_frame_as_scene_image` |
| `POST` | `/projects/{id}/references` | `_save_flux_reference_image` (`reference_type`: subject / location / ingredients_sheet) |
| `POST` | `/projects/{id}/audio` | `_save_project_audio` |
| `POST` | `/projects/{id}/scenes/{scene_id}/audio` | `_save_scene_audio` |
| `POST` | `/projects/{id}/scenes/{scene_id}/audio/trim` | `_trim_scene_audio` |
| `POST` | `/projects/{id}/audio/mix` | `_prepare_scene_audio_mix` |

These pass the request payload through to the existing Builder function (with
`project_folder` resolved server-side) and return its result under `data`.

---

## Phase 2 (in progress)

- **Job layer** — `POST .../image/generate`, `POST .../video/generate`,
  `GET /jobs/{job_id}`, `POST /jobs/{job_id}/cancel`; a job registry mapping
  VRGDG job IDs to ComfyUI prompt IDs.
- **Generation bridge** — `_run_generation_job`: scene → engine (LTX / MiniMax H3)
  → ComfyUI workflow template → queue → poll → collect → save media → update job
  and scene.
- **Final-render orchestration** — `POST /projects/{id}/render`: verify/generate
  missing scene media → stitch (ffmpeg concat + audio mux) → progress via job →
  `_save_builder_render_log()` → final artifact metadata.

## Phase 3 (planned)

- A thin **stdio MCP server** exposing the same operations as clean tools
  (`create_video_project`, `add_scene`, `generate_scene_image`,
  `generate_scene_video`, `render_project`, `get_job_status`, `export_project`, ...).
- A Hermes Desktop skill for the agentic workflow (staged approval:
  prompt → approve → generate → approve → next; revision-conflict retry).

---

## Development

- Unit tests for the import-safe logic: `tests/test_video_builder_api.py`.
  Run standalone (the repo root `__init__.py` imports ComfyUI's `folder_paths`,
  so `pytest` from the repo root fails to collect):

  ```
  python tests/test_video_builder_api.py
  ```

- The module registers routes at import time; it is a no-op when
  `PromptServer.instance` is not yet available and re-runs on the next call, so
  ComfyUI's deferred startup still picks the routes up.
