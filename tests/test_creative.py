import json
import importlib
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

from cuddler_plugin.cuddler import audio, image
from cuddler_plugin.cuddler.catalog import parse
from cuddler_plugin.cuddler.errors import CuddlerError
from cuddler_plugin.cuddler.jobs import JobRunner
from tests import fakes
from tests.test_entrypoint import comfyui_process, load_like_comfyui


class CreativeRequestsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = parse(json.loads((Path(__file__).parents[1] / "data/catalog_snapshot.json").read_text()))

    def test_every_image_choice_has_an_estimate_matching_its_reference_count(self):
        for label, (model, mode, variant) in image.choices(self.catalog).items():
            references = [] if mode == "text" else [b"offline-reference"]
            with self.subTest(model=model["id"], mode=mode, variant=variant):
                spec = image.prepare(self.catalog, label,
                    {"resolution": model["resolutions"][0], "aspect_ratio": model["aspect_ratios"][0]},
                    "An orange ball", 1, 2, references=references)
                self.assertEqual(spec.kind, "image")
                self.assertEqual(spec.estimate_body["reference_image_count"], len(spec.media))
                self.assertNotIn("mode", spec.estimate_body)
                self.assertNotIn("prompt", spec.estimate_body)
                self.assertEqual(spec.cap_usd, "2.000000")

    def test_image_refusals_happen_before_network_or_upload(self):
        label, (model, _, _) = next((key, val) for key, val in image.choices(self.catalog).items() if val[1] == "text")
        params = {"resolution": model["resolutions"][0], "aspect_ratio": model["aspect_ratios"][0]}
        for changes in [{"references": [b"image"]}, {"seed": True}, {"prompt": " "}, {"cap": float("nan")},
                        {"parameters": params | {"unexpected": True}}]:
            args = dict(catalog=self.catalog, selection=label, parameters=params, prompt="orange", seed=1, cap=2)
            with self.subTest(changes=list(changes)), self.assertRaises(CuddlerError):
                image.prepare(**(args | changes))

    def test_each_speech_model_uses_a_catalog_voice_and_real_unicode_text(self):
        for label, model in audio.choices(self.catalog, "speech").items():
            voice = self.catalog.voices[model["id"]][0]["id"]
            line = {"voice_id": voice, "text": "你好🙂"}
            spec = audio.prepare_speech(self.catalog, label, [line], 9, 2)
            self.assertEqual(spec.body["text"], "你好🙂")
            self.assertEqual(spec.estimate_body, spec.body)
            self.assertNotIn("seed", spec.body)
            with self.assertRaises(CuddlerError):
                audio.prepare_speech(self.catalog, label, [line | {"voice_id": "voice_unknown"}], 9, 2)
            with self.assertRaises(CuddlerError):
                audio.prepare_speech(self.catalog, label, [line | {"text": "a" * (model["max_characters"] + 1)}], 9, 2)
            if model["dialogue"]:
                spec = audio.prepare_speech(self.catalog, label, [line, line], 9, 2)
                self.assertNotIn("text", spec.body)
                self.assertEqual(len(spec.body["dialogue"]), 2)
            else:
                with self.assertRaises(CuddlerError):
                    audio.prepare_speech(self.catalog, label, [line, line], 9, 2)

    def test_music_seeds_affect_recovery_without_sending_an_unsupported_seed(self):
        runner = JobRunner(None, None, None)
        for label in audio.choices(self.catalog, "music"):
            first = audio.prepare_music(self.catalog, label, "Original instrumental", 1, 2)
            second = audio.prepare_music(self.catalog, label, "Original instrumental", 2, 2)
            self.assertNotIn("seed", first.body)
            self.assertEqual(first.body, first.estimate_body)
            self.assertNotEqual(runner.fingerprint(first), runner.fingerprint(second))
            with self.assertRaises(CuddlerError):
                audio.prepare_music(self.catalog, label, "x" * 2001, 1, 2)

    def test_numbered_script_preserves_fifty_turns_and_ten_speakers(self):
        values = {"voice_id": "speaker-1", **{f"voice_id_{i}": f"speaker-{i}" for i in range(2, 11)},
                  "text": "", "dialogue_script": "\n".join(f"{i % 10 + 1}: 你好 {i}🙂" for i in range(50))}
        lines = audio.speech_lines(values)
        self.assertEqual(len(lines), 50)
        self.assertEqual(len({line["voice_id"] for line in lines}), 10)
        self.assertEqual(lines[-1], {"voice_id": "speaker-10", "text": "你好 49🙂"})
        for changes in ({"text": "conflicting text"}, {"second_speaker": True},
                        {"dialogue_script": "11: invalid"}, {"dialogue_script": "no speaker: hello"},
                        {"dialogue_script": values["dialogue_script"] + "\n1: too many"}):
            with self.subTest(changes=list(changes)), self.assertRaises(CuddlerError):
                audio.speech_lines(values | changes)


class CreativeNodesTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.process = comfyui_process(self.tmp.name)
        self.process.__enter__()
        self.addCleanup(self.process.__exit__, None, None, None)
        name, _ = load_like_comfyui()
        self.nodes = importlib.import_module(name + ".cuddler_host.creative")
        runtime = sys.modules[name + ".cuddler_host.runtime"]
        self.transport = fakes.FakeTransport()
        runtime.configure_transport(self.transport)
        self.runtime = runtime.get_runtime()
        self.runtime.keys.save(fakes.LIVE_KEY)
        self.boundary = mock.patch.object(self.nodes, "download", return_value="native-output")
        self.boundary.start()
        self.addCleanup(self.boundary.stop)

    def spec(self, kind):
        catalog = self.runtime.catalog.current()
        if kind == "image":
            label, (model, _, _) = next((k, v) for k, v in self.nodes.image.choices(catalog).items() if v[1] == "text")
            return self.nodes.CuddlerImage, dict(model={"model": label, "resolution": model["resolutions"][0],
                "aspect_ratio": model["aspect_ratios"][0]}, prompt="orange", max_usd=2, seed=1)
        label, model = next(iter(self.nodes.audio.choices(catalog, kind).items()))
        if kind == "music":
            return self.nodes.CuddlerMusic, dict(model=label, prompt="instrumental", max_usd=2, seed=1)
        voice = catalog.voices[model["id"]][0]["id"]
        return self.nodes.CuddlerSpeech, dict(model={"model": label, "text": "hello", "voice_id": voice}, max_usd=2, seed=1)

    async def test_all_creative_nodes_use_durable_recovery_before_new_submission(self):
        for kind, endpoint, prefix in (("image", "images", "img"), ("speech", "speech", "aud"), ("music", "music", "mus")):
            with self.subTest(kind=kind):
                job = {**fakes.video_job("succeeded"), "object": kind, "id": prefix + "_" + "A" * 20}
                quote = {**fakes.estimate(), "object": kind + "_estimate"}
                self.transport.add("POST", f"/{endpoint}/estimate", body=quote)
                self.transport.add("POST", f"/{endpoint}", body=job)
                self.transport.add("GET", f"/{endpoint}/" + job["id"], body=job)
                node, args = self.spec(kind)
                for _ in range(2):
                    self.assertEqual((await node.execute(**args))[0], "native-output")
                self.assertEqual(len(self.transport.calls("POST", f"/{endpoint}")), 1)
                self.assertEqual(len(self.transport.calls("GET", f"/{endpoint}/" + job["id"])), 1)
                sent = json.loads(self.transport.calls("POST", f"/{endpoint}")[0].body)
                self.assertEqual(sent["max_usd"], "2.000000")
                self.assertTrue(sent["metadata"]["client"].startswith("comfyui-cuddler/"))

    async def test_test_keys_and_invalid_inputs_never_spend(self):
        for kind in ("image", "speech", "music"):
            node, args = self.spec(kind)
            self.runtime.keys.save(fakes.TEST_KEY)
            with self.subTest(kind=kind), self.assertRaisesRegex(RuntimeError, "test key"):
                await node.execute(**args)
        self.assertEqual(self.transport.requests, [])
        self.runtime.keys.save(fakes.LIVE_KEY)
        with self.assertRaises(RuntimeError):
            await self.nodes.CuddlerImage.execute(None, "orange", 2, 1)
        with self.assertRaises(RuntimeError):
            await self.nodes.CuddlerSpeech.execute({"model": "missing"}, 2, 1)
        self.assertEqual(self.transport.requests, [])


if __name__ == "__main__":
    unittest.main()
