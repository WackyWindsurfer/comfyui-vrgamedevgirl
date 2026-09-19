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
> export/import), Phase 2 (job layer, ComfyUI generation bridge, final-render
> orchestration), and Phase 3 (stdio MCP server + Hermes skill + end-to-end run)
> are complete and live-verified.

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

## Jobs (Phase 2 — generation + render bridge)

The job layer is **engine-agnostic**. The agent supplies a `builder` name and a
`builder_payload`; the bridge calls the existing `_build_*_api_prompt` function
(whitelisted), which loads the correct workflow template and patches it, then
queues the resulting ComfyUI API prompt, polls `/history`, collects the output,
and persists it into the session. This reuses the exact generation path the
Video Builder UI uses — no engine logic is duplicated.

**`POST /api/v1/video-builder/jobs/generate`**

```json
{
  "project_id": "my-video",
  "scene_id": "1",
  "builder": "i2v",
  "collect": "video",
  "timeout_s": 900,
  "builder_payload": { "i2v_prompt": "...", "width": 512, "height": 512, "length": 2 }
}
```

- `builder` is one of: `zimage`, `krea2`, `krea2_2pass`, `ernie_image`,
  `flux_klein`, `nb_image`, `i2v`, `t2v`, `rtv`, `minimax_h3`,
  `minimax_h3_2pass`, `minimax_h3_advanced_2pass`, `minimax_h3_3pass`,
  `ingredients`, `flf`, `id_lora`.
- `collect` is `video` (i2v/t2v/rtv → `_collect_scene_video`) or `image`
  (→ `_save_generated_image`).
- The produced media is written into the scene's `video_path` / `image_path`
  (lossless, revision-bumped). ComfyUI errors (missing node, missing model,
  validation) are surfaced in `job.error`.

**`POST /api/v1/video-builder/jobs/render`**

```json
{ "project_id": "my-video", "dry_run": true, "scene_ids": [], "audio_path": "" }
```

Gathers each scene's `video_path`, runs `_stitch_scene_videos` (ffmpeg concat +
audio mux), and writes a durable `_save_builder_render_log`. `dry_run` reports
the scene count without stitching.

**Job lifecycle**

- `GET /api/v1/video-builder/jobs?project_id=...` — list (newest first).
- `GET /api/v1/video-builder/jobs/{job_id}` — status
  (`queued` → `running` → `complete` / `failed` / `cancelled`), `message`,
  `comfyui_prompt_id`, `result`, `error`.
- `POST /api/v1/video-builder/jobs/{job_id}/cancel` — cooperative cancel (checked
  between poll ticks).

Jobs are transient; the durable record is the render log + the session's media
paths.

## Phase 3 — stdio MCP server + Hermes skill (DONE)

`VRGDG_VideoBuilderMCP.py` is a **thin stdio MCP server** over the REST API.
Every tool is a one-to-one wrapper around an `/api/v1/video-builder/...`
endpoint; it adds no generation logic (the ComfyUI instance does all the work)
and returns the raw JSON envelope verbatim — including `revision` and the
`409 REVISION_CONFLICT` error shape, which the agent uses to reconcile and retry.

**Run:** `python VRGDG_VideoBuilderMCP.py` (stdio). Configure via env:
`VRGDG_API_BASE` (e.g. `http://127.0.0.1:8188/api/v1/video-builder`) or
`VRGDG_BASE_URL` (e.g. `http://127.0.0.1:8188`).

**20 tools:** `list_projects`, `create_project`, `get_project`, `delete_project`,
`branch_project`, `get_project_status`, `list_scenes`, `get_scene`, `add_scene`,
`patch_scene`, `delete_scene`, `generate_scene`, `render_project`, `list_jobs`,
`get_job`, `cancel_job`, `scan_scene_videos`, `save_scene_image`,
`save_project_audio`, `export_project`.

> **Dependency:** the server uses the `mcp` package (FastMCP). Pin `mcp<2`
> (FastMCP was renamed to `MCPServer` in mcp 2.x). Install:
> `pip install "mcp<2"`.

**Agentic workflow (the Hermes skill drives this):**
1. `create_project` → `add_scene` (pass `base_revision` to avoid 409).
2. For each scene: `generate_scene` (returns a job) → poll `get_job` until
   `complete`/`failed` → the media is persisted into the session.
3. `render_project` (stitch + render log) → poll `get_job`.
4. On a `409 REVISION_CONFLICT`, re-read the current revision (from the error's
   `current.revision`) and retry the mutation.

**Verified end-to-end** two ways:
1. `tests/test_mcp_e2e.py` — official MCP client over stdio against the live
   8188 instance: server boots, lists 20 tools, full
   `create_project → add_scene → render_project (dry-run) → get_job →
   delete_project` round-trip succeeds; test project cleaned up.
2. **From Hermes Desktop** — the server is registered in Hermes
   (`hermes mcp add vrgdg_video_builder`, 20/20 tools enabled). A fresh
   `hermes` session natively called the tools: `list_projects` (ok, 0
   projects) → `create_project("HERMES-E2E-DESKTOP")` (ok, id returned) →
   `delete_project` (ok, cleaned up). Confirms the full agentic path:
   Hermes → MCP (stdio) → REST API → ComfyUI instance.

> **Note (environment):** a real GPU *completion* requires the diffusion/video
> models + custom nodes to be installed on the target ComfyUI instance. The
> bridge is verified end-to-end (build → queue → poll → collect → persist) against
> the live instance; builder-level validation errors (missing model / node /
> audio) surface cleanly in `job.error`.

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

## Running a real generation (environment requirements)

The API/bridge/MCP stack is engine-agnostic and needs no special setup to *run*
(jobs, validation, render orchestration all work with no models). But to actually
produce media on a given instance, that instance must be able to see the models
and the nodes the chosen builder's template uses. On the **HERMES (8188)**
instance, two things are required:

1. **Shared model folder.** The models live in a shared folder
   (`E:\AI\models`), not the instance's local `models/` dir. Start the instance
   with the flag pointing at the config that maps that folder:

   ```
   python main.py --port 8188 --listen 127.0.0.1 \
     --extra-model-paths-config "D:/AI/ComfyUI-Installs/HERMES/extra_model_paths.yaml"
   ```

   Without the flag, `main.py` only looks in the ComfyUI root and the instance
   sees almost no models (the config file lives in the *parent* dir, so it is
   not auto-discovered).

2. **Scheduler node pack.** The `krea2` and `zimage` image builders' templates
   use the `FlowMatch Euler Discrete Scheduler (Custom)` node, which is provided
   by the `erosdiffusion-eulerflowmatchingdiscretescheduler` custom-node pack
   (self-contained; only needs `diffusers`, already in the venv). It must be
   installed in the instance's `custom_nodes/` and the instance restarted.

3. **SageAttention + Triton (video builders, e.g. `minimax_h3`).** The video
   builders' `DiffusionModelLoaderKJ` node defaults to `sage_attention: "auto"`,
   which imports `sageattention` at runtime — so a video generation needs
   `sageattention` **and** `triton` installed in the instance's Python. The
   easy-install (8191) runs on an embedded Python that already has
   `sageattention 2.2.0` + `triton` + `sageattn3`; a `.venv`-based instance
   (8188) must add them. Note the Python-version coupling: the prebuilt
   `triton` wheel is per-CPython-version, so install the `triton-windows` build
   matching the venv's Python (e.g. `pip install triton-windows` for a 3.13 venv;
   copying a 3.12 build fails with a DLL-load error). `sageattention`'s "auto"
   mode also needs a supported CUDA arch (RTX 5090 = `sm120`, supported).

A real end-to-end generation is covered by `tests/test_real_generation_e2e.py`
(MCP client over stdio → REST → ComfyUI → GPU), which runs a real Krea2 image
and asserts the job completes and the session revision is bumped. Model names
must match **exactly** what the live instance enumerates (e.g. a model in a
subfolder is listed with a `subfolder\name` prefix).
