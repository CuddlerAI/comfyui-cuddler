import json
from pathlib import Path
import unittest

from cuddler_plugin.cuddler.catalog import parse
from cuddler_plugin.cuddler.errors import CuddlerError
from cuddler_plugin.cuddler.video import VIDEO_MODES, VideoReference, choices, defaults, prepare

ROOT = Path(__file__).resolve().parents[1]


class VideoRequestTest(unittest.TestCase):
    def setUp(self):
        self.catalog = parse(json.loads((ROOT / "data/catalog_snapshot.json").read_text()))
        self.choices = choices(self.catalog)

    def selection(self, mode="text", model_id="seedance-2.5"):
        return next(label for label, (model, item_mode, _) in self.choices.items()
                    if model["id"] == model_id and item_mode == mode)

    def parameters(self, selection):
        return defaults(*self.choices[selection])

    def media(self, mode):
        clip = VideoReference(b"synthetic-video", 4, 720, 720, 24)
        return ({"first_frame": b"first"} if mode == "image" else
                {"first_frame": b"first", "last_frame": b"last"} if mode == "first-last-frame" else
                {"reference_images": [b"reference"]} if mode in {"reference", "image-reference"} else
                {"reference_images": [b"reference"], "reference_videos": [clip]} if mode == "motion-control" else
                {"reference_videos": [clip]} if mode in {"video-reference", "edit", "extend"} else {})

    def test_each_advertised_model_mode_variant_has_valid_defaults(self):
        self.assertTrue(self.choices)
        modes = set()
        for selection, (model, mode, _) in self.choices.items():
            with self.subTest(selection=selection):
                spec = prepare(self.catalog, selection, self.parameters(selection), "a synthetic scene", 12, 2,
                               **self.media(mode))
                self.assertEqual(spec.body["model"], model["id"])
                self.assertEqual(spec.body["mode"], mode)
                self.assertEqual(spec.cap_usd, "2.000000")
                modes.add(mode)
        self.assertEqual(modes, set(VIDEO_MODES))

    def test_estimates_exclude_prompt_seed_and_media(self):
        selection = self.selection("image")
        spec = prepare(self.catalog, selection, self.parameters(selection), "private prompt", 12, 2,
                       first_frame=b"synthetic-png")
        self.assertEqual(set(spec.estimate_body) - {"model", "mode", "variant", "duration", "resolution",
                                                  "aspect_ratio", "generate_audio", "reference_image_count"}, set())
        self.assertNotIn("private prompt", json.dumps(spec.estimate_body))
        self.assertNotIn("first_frame", spec.body)
        self.assertEqual(spec.media[0].api_field, "first_frame")
        self.assertEqual(spec.media[0].data, b"synthetic-png")
        self.assertEqual(spec.body["aspect_ratio"], "adaptive")

    def test_invalid_parameters_stop_before_a_request_can_be_built(self):
        selection = self.selection()
        valid = self.parameters(selection)
        cases = [{**valid, "duration": True}, {**valid, "duration": 999},
                 {**valid, "resolution": "unknown"}, {**valid, "aspect_ratio": "adaptive"},
                 {**valid, "max_usd": "999"}, {**valid, "webhook_url": "https://example.com"}]
        for parameters in cases:
            with self.subTest(parameters=parameters), self.assertRaises(CuddlerError):
                prepare(self.catalog, selection, parameters, "a synthetic scene", 0, 2)
        for prompt, seed, cap in [(" ", 0, 2), ("x" * 4001, 0, 2), ("x", -1, 2),
                                  ("x", 2**31, 2), ("x", True, 2), ("x", 0, 0)]:
            with self.subTest(seed=seed, cap=cap), self.assertRaises(CuddlerError):
                prepare(self.catalog, selection, valid, prompt, seed, cap)

    def test_first_frame_is_required_only_for_image_mode(self):
        for mode, frame in [("image", None), ("image", b""), ("text", b"synthetic-png")]:
            selection = self.selection(mode)
            with self.subTest(mode=mode, frame=frame), self.assertRaises(CuddlerError):
                prepare(self.catalog, selection, self.parameters(selection), "synthetic scene", 0, 2,
                        first_frame=frame)

    def test_unknown_and_disabled_models_cannot_be_selected(self):
        selection = self.selection()
        parameters = self.parameters(selection)
        del self.catalog.models["seedance-2.5"]
        with self.assertRaises(CuddlerError):
            prepare(self.catalog, selection, parameters, "synthetic scene", 0, 2)

    def test_discrete_price_combinations_are_checked(self):
        selection = self.selection()
        model, mode, variant = self.choices[selection]
        model["pricing"] = [{"mode": mode, "variant": variant, "duration": 4, "resolution": "720p"}]
        parameters = self.parameters(selection)
        prepare(self.catalog, selection, parameters, "synthetic scene", 0, 2)
        with self.assertRaises(CuddlerError):
            prepare(self.catalog, selection, {**parameters, "duration": 5}, "synthetic scene", 0, 2)

    def test_silent_models_send_false_to_override_the_api_default(self):
        for model_id in ("minimax-hailuo-2.3", "kling-2.5-turbo"):
            selection = self.selection(model_id=model_id)
            model, _, _ = self.choices[selection]
            self.assertFalse(model["native_audio"])
            self.assertNotIn("generate_audio", model["request_fields"])
            spec = prepare(self.catalog, selection, self.parameters(selection), "synthetic scene", 0, 2)
            self.assertIs(spec.body["generate_audio"], False)
            self.assertIs(spec.estimate_body["generate_audio"], False)

    def test_edit_quote_uses_uploaded_duration_and_all_image_inputs(self):
        selection = self.selection("edit")
        clips = [VideoReference(b"one", 4.125, 720, 720, 24), VideoReference(b"two", 6.25, 720, 720, 24)]
        spec = prepare(self.catalog, selection, self.parameters(selection), "edit", 1, 2,
                       reference_videos=clips, reference_images=[b"image"])
        self.assertEqual(spec.body["task_type"], "edit")
        self.assertEqual(spec.estimate_body["duration"], 7)
        self.assertEqual(spec.estimate_body["input_video_seconds"], 10.375)
        self.assertEqual(spec.estimate_body["reference_image_count"], 1)
        self.assertFalse(set(spec.estimate_body) & {"camera_fixed", "return_last_frame", "task_type", "prompt", "seed"})

    def test_kling_discrete_durations_reject_invalid_run_before_upload(self):
        for model_id, mode in (("kling-2.6", "text"), ("kling-2.6", "image"),
                               ("kling-2.5-turbo", "text"), ("kling-o1", "text"),
                               ("kling-o1", "first-last-frame")):
            selection = self.selection(mode, model_id)
            parameters = self.parameters(selection)
            self.assertEqual(parameters["duration"], 5)
            for duration in (3, 6, 9):
                with self.subTest(model=model_id, duration=duration), self.assertRaises(CuddlerError):
                    prepare(self.catalog, selection, parameters | {"duration": duration}, "synthetic", 1, 2,
                            **self.media(mode))
            for duration in (5, 10):
                spec = prepare(self.catalog, selection, parameters | {"duration": duration}, "synthetic", 1, 2,
                               **self.media(mode))
                self.assertEqual(spec.body["duration"], duration)

    def test_kling_source_length_controls_both_quote_and_creation(self):
        for model_id, mode in (("kling-3.0", "motion-control"), ("kling-2.6", "motion-control"),
                               ("kling-o1", "edit"), ("kling-o3", "edit")):
            selection = self.selection(mode, model_id)
            media = self.media(mode) | {"reference_videos": [VideoReference(b"clip", 4.0417, 720, 720, 24)]}
            spec = prepare(self.catalog, selection, self.parameters(selection), "synthetic", 1, 2, **media)
            self.assertEqual(spec.body["duration"], 5)
            self.assertEqual(spec.estimate_body["duration"], 5)
            self.assertEqual(spec.estimate_body["input_video_seconds"], 4.0417)

    def test_mode_and_reference_limits_fail_before_any_upload(self):
        cases = [("text", "seedance-2.5", {"reference_images": [b"one"]}),
                 ("image", "seedance-2.5", {"first_frame": b"one", "reference_images": [b"two"]}),
                 ("first-last-frame", "kling-o1", {"first_frame": b"one"}),
                 ("reference", "seedance-2.0", {"reference_audios": [b"audio"]}),
                 ("reference", "seedance-2.5", {"reference_images": [b"same", b"same"]}),
                 ("reference", "seedance-2.5", {"reference_images": [str(i).encode() for i in range(31)]}),
                 ("edit", "seedance-2.5", {"reference_videos": [VideoReference(b"x", 3, 720, 720, 24)]}),
                 ("motion-control", "kling-3.0", {"reference_videos": [VideoReference(b"x", 4, 720, 720, 24)]})]
        for mode, model_id, media in cases:
            selection = self.selection(mode, model_id)
            with self.subTest(mode=mode, model=model_id), self.assertRaises(CuddlerError):
                prepare(self.catalog, selection, self.parameters(selection), "synthetic", 1, 2, **media)

    def test_legacy_fields_and_extra_controls_are_not_leaked_into_quotes(self):
        selection = self.selection("image")
        parameters = {**self.parameters(selection), "camera_fixed": True, "return_last_frame": True}
        spec = prepare(self.catalog, selection, parameters, "synthetic", 12, 2, first_frame=b"first", last_frame=b"last")
        self.assertTrue(spec.body["camera_fixed"])
        self.assertTrue(spec.body["return_last_frame"])
        self.assertEqual(spec.body["seed"], 12)
        self.assertEqual(spec.estimate_body["reference_image_count"], 2)
        for changes in ({"fps": 60}, {"camera_fixed": "false"}, {"character_orientation": "video"}):
            with self.subTest(changes=changes), self.assertRaises(CuddlerError):
                prepare(self.catalog, selection, parameters | changes, "synthetic", 1, 2, first_frame=b"first")


if __name__ == "__main__":
    unittest.main()
