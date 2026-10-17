import json
from pathlib import Path
import unittest

from cuddler_plugin.cuddler.catalog import parse
from cuddler_plugin.cuddler.errors import CuddlerError
from cuddler_plugin.cuddler.video import choices, defaults, prepare

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

    def test_each_advertised_model_mode_variant_has_valid_defaults(self):
        self.assertTrue(self.choices)
        modes = set()
        for selection, (model, mode, _) in self.choices.items():
            with self.subTest(selection=selection):
                spec = prepare(self.catalog, selection, self.parameters(selection), "a synthetic scene", 12, 2,
                               first_frame=b"synthetic-png" if mode == "image" else None)
                self.assertEqual(spec.body["model"], model["id"])
                self.assertEqual(spec.body["mode"], mode)
                self.assertEqual(spec.cap_usd, "2.000000")
                modes.add(mode)
        self.assertEqual(modes, {"text", "image"})

    def test_estimates_exclude_prompt_seed_and_media(self):
        selection = self.selection("image")
        spec = prepare(self.catalog, selection, self.parameters(selection), "private prompt", 12, 2,
                       first_frame=b"synthetic-png")
        self.assertEqual(set(spec.estimate_body) - {"model", "mode", "variant", "duration", "resolution",
                                                  "aspect_ratio", "generate_audio"}, set())
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


if __name__ == "__main__":
    unittest.main()
