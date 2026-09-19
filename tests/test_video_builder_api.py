"""Unit tests for the VRGDG Video Builder Agent API pure-logic layer.

These tests exercise the import-safe, Builder-free functions in
``VRGDG_VideoBuilderAPI`` (normalization, merge engine, revision check, scene-id
resolution). They do NOT require a live ComfyUI or the Builder module.

Run with:  python -m pytest tests/test_video_builder_api.py -v
"""

import importlib.util
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_api():
    """Load VRGDG_VideoBuilderAPI as a standalone module (no package context).

    The module's route-registration block imports the Builder lazily and is
    guarded, so importing it standalone only executes the pure-logic definitions.
    """
    path = os.path.join(ROOT, "VRGDG_VideoBuilderAPI.py")
    spec = importlib.util.spec_from_file_location("vrgdg_video_builder_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


api = _load_api()


class ProjectIdTests(unittest.TestCase):
    def test_id_derived_from_folder_basename(self):
        self.assertEqual(api.project_id_from_folder("C:\\x\\output\\my-video"), "my-video")
        self.assertEqual(api.project_id_from_folder("D:\\AI\\out\\Neon Dreams"), "Neon Dreams")

    def test_id_strips_quotes_and_whitespace(self):
        self.assertEqual(api.project_id_from_folder('  "my-video"  '), "my-video")

    def test_empty_folder_gives_empty_id(self):
        self.assertEqual(api.project_id_from_folder(""), "")


class SceneIdTests(unittest.TestCase):
    def test_explicit_id_wins(self):
        self.assertEqual(api.scene_public_id({"id": "recovered_scene_1"}, 0), "recovered_scene_1")

    def test_derives_zero_padded_number(self):
        self.assertEqual(api.scene_public_id({"scene_number": 3}, 0), "scene_003")

    def test_falls_back_to_index(self):
        self.assertEqual(api.scene_public_id({}, 4), "scene_005")


class MergeEngineTests(unittest.TestCase):
    def test_object_recursive_merge_preserves_siblings(self):
        base = {"a": 1, "nested": {"x": 1, "y": 2}}
        patch = {"nested": {"y": 99}}
        api.apply_patch(base, patch)
        self.assertEqual(base["a"], 1)
        self.assertEqual(base["nested"], {"x": 1, "y": 99})

    def test_null_clears_field(self):
        base = {"a": 1, "b": "keep"}
        api.apply_patch(base, {"a": None})
        self.assertIsNone(base["a"])
        self.assertEqual(base["b"], "keep")

    def test_array_replaces_by_default(self):
        base = {"arr": [1, 2, 3]}
        api.apply_patch(base, {"arr": [9]})
        self.assertEqual(base["arr"], [9])

    def test_catalog_merges_by_id(self):
        base = [{"id": "s1", "name": "A"}, {"id": "s2", "name": "B"}]
        patch = [{"id": "s1", "name": "A2"}, {"id": "s3", "name": "C"}]
        result = api.merge_catalog_by_id(base, patch)
        self.assertEqual(len(result), 3)
        by_id = {e["id"]: e for e in result}
        self.assertEqual(by_id["s1"]["name"], "A2")
        self.assertEqual(by_id["s2"]["name"], "B")  # untouched survives
        self.assertEqual(by_id["s3"]["name"], "C")

    def test_catalog_replace_wrapper(self):
        base = [{"id": "s1"}]
        result = api.merge_catalog_by_id(base, {"replace": [{"id": "z"}]})
        self.assertEqual(result, [{"id": "z"}])


class RevisionTests(unittest.TestCase):
    def test_matching_revision_passes(self):
        self.assertIsNone(api.check_revision(14, 14))

    def test_mismatched_revision_conflicts(self):
        err = api.check_revision(14, 16)
        self.assertEqual(err["code"], "REVISION_CONFLICT")
        self.assertEqual(err["details"]["current_revision"], 16)

    def test_none_base_skips_check(self):
        self.assertIsNone(api.check_revision(None, 16))


class SceneNormalizationTests(unittest.TestCase):
    def test_round_trip_is_lossless(self):
        segment = {
            "id": "scene_001",
            "scene_number": 1,
            "label": "Opening",
            "start": 0.0,
            "end": 5.2,
            "lyric_text": "hello",
            "lyric_singers": ["Alice"],
            "lyric_instrumental": False,
            "lyric_no_lip_sync": False,
            "no_character_present": False,
            "mapped_subjects": ["Alice"],
            "director_note": "fast",
            "scene_note": "energetic",
            "timeline_notes": ["beat"],
            "subject_reference_mode": "reference",
            "location_reference_mode": "reference",
            "t2i_prompt": "a woman",
            "i2v_prompt": "she walks",
            "concept_prompt": "a concept",
            "minimax_stage": "some_engine_specific_field",  # must survive in metadata
            "video_status": "done",
        }
        scene = api._scene_to_api(segment, 0)
        self.assertEqual(scene["id"], "scene_001")
        self.assertEqual(scene["timing"]["duration"], 5.2)
        self.assertEqual(scene["lyrics"]["text"], "hello")
        self.assertEqual(scene["subjects"]["character_present"], True)
        # Lossless remainder captured.
        self.assertIn("metadata", scene)
        self.assertEqual(scene["metadata"]["minimax_stage"], "some_engine_specific_field")

        # Round-trip back to a segment and confirm the engine field survives.
        rebuilt = api._scene_to_segment(scene, None)
        self.assertEqual(rebuilt.get("minimax_stage"), "some_engine_specific_field")
        self.assertEqual(rebuilt.get("lyric_text"), "hello")
        self.assertEqual(rebuilt.get("t2i_prompt"), "a woman")
        self.assertEqual(rebuilt.get("start"), 0.0)
        self.assertEqual(rebuilt.get("end"), 5.2)

    def test_scene_to_segment_preserves_unknown_existing_fields(self):
        existing = {"start": 1.0, "end": 2.0, "future_field": "keep-me"}
        patch = {"label": "New Label"}
        result = api._scene_to_segment(patch, existing)
        self.assertEqual(result["future_field"], "keep-me")
        self.assertEqual(result["label"], "New Label")
        self.assertEqual(result["start"], 1.0)

    def test_scene_to_segment_in_place_target(self):
        seg = {"start": 1.0, "end": 2.0, "director_note": "old", "keep": "x"}
        patch = {"content": {"director_note": "new"}}
        returned = api._scene_to_segment(patch, None, target=seg)
        self.assertIs(returned, seg)
        self.assertEqual(seg["director_note"], "new")
        self.assertEqual(seg["keep"], "x")

    def test_find_scene_by_id_and_number(self):
        session = {
            "segments": [
                {"id": "scene_001", "scene_number": 1},
                {"scene_number": 2},
            ]
        }
        seg, idx = api.find_scene(session, scene_id="scene_001")
        self.assertEqual(idx, 0)
        seg, idx = api.find_scene(session, scene_number=2)
        self.assertEqual(idx, 1)
        seg, idx = api.find_scene(session, scene_id="scene_999")
        self.assertIsNone(seg)


class SessionNormalizationTests(unittest.TestCase):
    def test_session_to_project_shape(self):
        session = {
            "project_folder": "C:\\x\\output\\demo",
            "audio_path": "C:\\x\\output\\demo\\project_audio\\a.wav",
            "builder_save_revision": 7,
            "segments": [{"scene_number": 1, "label": "S1"}],
            "overlay_segments": [],
            "builder_story_layer": {"overall_story_idea": "x"},
            "builder_storyboard_defaults": {"image_aesthetic": "y"},
            "flux_reference_builder": {"subjects": [], "locations": []},
            "render_logs": [],
            "active_render_log_id": "",
        }
        project = api.session_to_project(session, "C:\\x\\output\\demo")
        self.assertEqual(project["id"], "demo")
        self.assertEqual(project["revision"], 7)
        self.assertEqual(len(project["scenes"]), 1)
        self.assertEqual(project["story"]["layer"]["overall_story_idea"], "x")
        self.assertEqual(project["references"], {"subjects": [], "locations": []})


class JobRegistryTests(unittest.TestCase):
    def test_new_job_defaults(self):
        job = api._new_job("demo", "generate", "scene_0001", {"builder": "i2v"})
        self.assertEqual(job["status"], "queued")
        self.assertEqual(job["progress"], 0.0)
        self.assertFalse(job["cancel_requested"])
        self.assertIsNotNone(job["id"])
        self.assertIsNone(job["error"])

    def test_update_job_lifecycle_timestamps(self):
        job = api._new_job("demo", "render")
        jid = job["id"]
        api._update_job(jid, status="running")
        self.assertIsNotNone(api._get_job(jid)["started_at"])
        self.assertIsNone(api._get_job(jid)["completed_at"])
        api._update_job(jid, status="complete", progress=1.0)
        self.assertIsNotNone(api._get_job(jid)["completed_at"])

    def test_list_jobs_filters_by_project(self):
        a = api._new_job("projA", "generate")["id"]
        b = api._new_job("projB", "render")["id"]
        api._update_job(a, status="complete")
        api._update_job(b, status="failed", error="boom")
        only_a = api._list_jobs("projA")
        self.assertTrue(any(j["id"] == a for j in only_a))
        self.assertFalse(any(j["id"] == b for j in only_a))
        failed = api._get_job(b)
        self.assertEqual(failed["error"], "boom")

    def test_get_job_missing_returns_none(self):
        self.assertIsNone(api._get_job("does_not_exist"))


class FindSegmentTests(unittest.TestCase):
    def _session(self):
        return {"segments": [
            {"id": "scene_0001", "scene_number": 1},
            {"scene_number": 2},  # no id -> ordinal
        ]}

    def test_by_id(self):
        seg = api._find_segment(self._session(), "scene_0001")
        self.assertEqual(seg["scene_number"], 1)

    def test_by_ordinal(self):
        seg = api._find_segment(self._session(), "2")
        self.assertEqual(seg["scene_number"], 2)

    def test_missing(self):
        self.assertIsNone(api._find_segment(self._session(), "scene_9999"))
        self.assertIsNone(api._find_segment({"segments": []}, "1"))


class ExtractSavedPathTests(unittest.TestCase):
    def test_dict_various_keys(self):
        self.assertEqual(api._extract_saved_path({"saved_path": "/x/a.png"}), "/x/a.png")
        self.assertEqual(api._extract_saved_path({"video_path": "/x/a.mp4"}), "/x/a.mp4")
        self.assertEqual(api._extract_saved_path({"path": "/x/a.png"}), "/x/a.png")

    def test_plain_string(self):
        self.assertEqual(api._extract_saved_path("/x/a.png"), "/x/a.png")

    def test_nothing_found(self):
        self.assertIsNone(api._extract_saved_path({"foo": "bar"}))
        self.assertIsNone(api._extract_saved_path(None))


class FindNewestVideoTests(unittest.TestCase):
    def test_picks_newest(self):
        import tempfile, time as _t
        d = tempfile.mkdtemp()
        old = os.path.join(d, "old.mp4")
        new = os.path.join(d, "new.mp4")
        open(old, "w").close()
        open(new, "w").close()
        _t.sleep(0.05)
        os.utime(old, (1000, 1000))
        self.assertEqual(api._find_newest_video(d), new)

    def test_missing_folder(self):
        self.assertIsNone(api._find_newest_video("/no/such/folder"))
        self.assertIsNone(api._find_newest_video(None))


if __name__ == "__main__":
    unittest.main()
