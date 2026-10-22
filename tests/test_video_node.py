"""Node-to-runner integration with real local records; native VIDEO decoding belongs to host CI."""
import importlib
import io
import json
import sys
import tempfile
import unittest
import wave
from unittest import mock

from PIL import Image
import numpy as np

from tests import fakes
from tests.test_entrypoint import comfyui_process, load_like_comfyui


class _HostImage:
    """Only the IMAGE tensor operations the boundary uses; no fake torch module is installed."""
    def __init__(self, array):
        self.array = array
        self.shape, self.ndim = array.shape, array.ndim

    def __getitem__(self, index):
        return _HostImage(self.array[index])

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.array


class _VideoBoundary:
    def __init__(self, source):
        self.bytes = source.read()
        self.checked = False

    def get_dimensions(self):
        self.checked = True
        return 16, 16


class VideoNodeTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.process = comfyui_process(self.tmp.name)
        self.process.__enter__()
        self.addCleanup(self.process.__exit__, None, None, None)
        self.name, _ = load_like_comfyui()
        self.video = importlib.import_module(self.name + ".cuddler_host.video")
        self.error = sys.modules[self.name + ".cuddler.errors"].CuddlerError
        runtime_module = sys.modules[self.name + ".cuddler_host.runtime"]
        self.transport = fakes.FakeTransport()
        runtime_module.configure_transport(self.transport)
        self.runtime = runtime_module.get_runtime()
        self.runtime.keys.save(fakes.LIVE_KEY)
        decoder = mock.patch.object(self.video.InputImpl, "VideoFromFile", _VideoBoundary)
        decoder.start()
        self.addCleanup(decoder.stop)

    def model(self, mode="text", **changes):
        label, (model, _, _) = next((key, value) for key, value in
            self.video.choices(self.runtime.catalog.current()).items()
            if value[0]["id"] == "seedance-2.5" and value[1] == mode)
        values = {"model": label, "duration": 4, "resolution": "720p",
                  "aspect_ratio": "adaptive" if mode == "image" else "16:9"}
        if "generate_audio" in model["request_fields"]:
            values["generate_audio"] = False
        return values | changes

    async def execute(self, model=None):
        return await self.video.CuddlerVideo.execute(
            model or self.model(), "an orange ball", 2.0, 123)

    def successful_video(self):
        self.transport.add("POST", "/videos/estimate", body=fakes.estimate())
        self.transport.add("POST", "/videos", body=fakes.video_job("succeeded"))
        self.transport.add("GET", "/videos/" + fakes.VIDEO_ID, body=fakes.video_job("succeeded"))
        self.transport.add("GET", fakes.OUTPUT_URL, body=b"offline-video-boundary")

    async def test_text_node_reuses_the_original_job_and_returns_the_native_video_boundary(self):
        self.successful_video()
        for _ in range(2):
            output = await self.execute()
            self.assertIsInstance(output[0], _VideoBoundary)
            self.assertTrue(output[0].checked)
            self.assertEqual(output[0].bytes, b"offline-video-boundary")
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)
        self.assertEqual(len(self.transport.calls("GET", fakes.OUTPUT_URL)), 2)

    async def test_first_frame_is_uploaded_as_a_metadata_free_png_before_video_creation(self):
        self.successful_video()
        self.transport.add("POST", "/uploads", status=201, body=fakes.upload())
        self.transport.add("PUT", fakes.PUT_URL, body=b"")
        self.transport.add("POST", "/uploads/" + fakes.UPLOAD_ID + "/complete", body=fakes.upload("ready"))
        image = _HostImage(np.ones((1, 4, 6, 3), dtype=np.float32))
        await self.execute(self.model("image", first_frame=image))
        sent = json.loads(self.transport.calls("POST", "/videos")[0].body)
        self.assertEqual((sent["mode"], sent["first_frame"]), ("image", fakes.UPLOAD_ID))
        with Image.open(io.BytesIO(self.transport.calls("PUT", fakes.PUT_URL)[0].body)) as png:
            self.assertEqual((png.size, png.info), ((6, 4), {}))

    async def test_a_failed_download_refreshes_only_the_original_job(self):
        self.successful_video()
        self.transport.routes[("GET", fakes.OUTPUT_URL)] = [self.error("media_transfer_failed"),
                                                          (200, b"refreshed-video", {})]
        output = await self.execute()
        self.assertEqual(output[0].bytes, b"refreshed-video")
        self.assertEqual(len(self.transport.calls("POST", "/videos")), 1)
        self.assertEqual(len(self.transport.calls("GET", "/videos/" + fakes.VIDEO_ID)), 1)

    async def test_bad_parameters_and_test_keys_cannot_create_or_upload(self):
        for model in (self.model(duration=-1), self.model("image")):
            with self.assertRaises(RuntimeError):
                await self.execute(model)
        self.runtime.keys.save(fakes.TEST_KEY)
        with self.assertRaisesRegex(RuntimeError, "test key"):
            await self.execute()
        self.assertEqual(self.transport.requests, [])

    def test_schema_advertises_all_modes_and_native_outputs(self):
        schema = self.video.CuddlerVideo.define_schema()
        self.assertEqual(schema.outputs, ["VIDEO", "IMAGE"])
        labels = [option.key for option in schema.inputs[0].options]
        self.assertTrue(labels)
        self.assertTrue(any("/ Motion control /" in label for label in labels))
        self.assertTrue(any("/ Extend video /" in label for label in labels))
        self.assertEqual([item.id for item in schema.inputs], ["model", "prompt", "max_usd", "seed"])

    def test_audio_reference_encoding_is_bounded_and_metadata_free(self):
        uploads = importlib.import_module(self.name + ".cuddler_host.uploads")
        native = {"waveform": _HostImage(np.zeros((1, 2, 32000), dtype=np.float32)), "sample_rate": 16000,
                  "workflow": "must not be uploaded"}
        data = uploads.audio_wav(native)
        self.assertNotIn(b"must not be uploaded", data)
        with wave.open(io.BytesIO(data)) as audio:
            self.assertEqual((audio.getnchannels(), audio.getnframes(), audio.getframerate()), (2, 32000, 16000))
        for shape in ((1, 1, 100), (2, 1, 32000), (1, 3, 32000)):
            with self.subTest(shape=shape), self.assertRaises(self.error):
                uploads.audio_wav({"waveform": _HostImage(np.zeros(shape)), "sample_rate": 16000})
        buffer = uploads.BoundedBuffer(4)
        buffer.write(b"1234")
        with self.assertRaises(self.error):
            buffer.write(b"5")
        with self.assertRaises(self.error):
            uploads.ordered_slots({"video-1": "invalid"}, "video", 10)


if __name__ == "__main__":
    unittest.main()
