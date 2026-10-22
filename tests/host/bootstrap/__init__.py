"""Host-test bootstrap (tests only, never shipped): points the Cuddler plugin at a fake Platform.

Install it next to the plugin as custom_nodes/zz_cuddler_host_test. ComfyUI does
not load custom nodes in a fixed order, so the fake is installed when the web
app starts, after every custom node has loaded.
"""
import io
import json
import os
from pathlib import Path
import sys

from server import PromptServer
from aiohttp import web

NODE_CLASS_MAPPINGS = {}
API = "https://api.cuddler.ai/v1"
BALANCE = {"object": "balance", "currency": "usd", "total_usd": "12.340000", "held_usd": "0.000000",
           "available_usd": "12.340000", "daily_limit_usd": "300.000000", "spent_today_usd": "0.000000",
           "expires_at": None}
ESTIMATE = {"object": "estimate", "usd": "0.653877", "hold_usd": "0.666955", "price_version": "fake"}
OUTPUT = "https://host-test.r2.cloudflarestorage.com/cuddler-media-private/video.mp4"
UPLOAD = "https://host-test.r2.cloudflarestorage.com/cuddler-media-private/input/"
_fake = None


def _video_bytes(*, size=16, seconds=1, rate=4, metadata=False):
    """A real tiny MP4, encoded only by the host CI's existing PyAV dependency."""
    import av
    import numpy as np

    target = io.BytesIO()
    with av.open(target, mode="w", format="mp4") as container:
        if metadata:
            container.metadata["comment"] = "PRIVATE_WORKFLOW_MARKER"
        stream = container.add_stream("mpeg4", rate=rate)
        stream.width = stream.height = size
        stream.pix_fmt = "yuv420p"
        for index in range(seconds * rate):
            frame = av.VideoFrame.from_ndarray(np.full((size, size, 3), 32 + index % 100, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    return target.getvalue()


class NativeMediaFixture:
    """Real native inputs; only present in the host-test bootstrap, never the package."""
    RETURN_TYPES = ("IMAGE", "IMAGE", "VIDEO", "AUDIO", "VIDEO")
    FUNCTION = "execute"
    CATEGORY = "test"
    _clip = None

    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    def execute(self):
        import torch
        from comfy_api.latest import InputImpl
        if self._clip is None:
            type(self)._clip = _video_bytes(size=720, seconds=6, rate=24, metadata=True)
        return (torch.full((1, 720, 720, 3), 0.2), torch.full((1, 720, 720, 3), 0.6),
                InputImpl.VideoFromFile(io.BytesIO(self._clip), start_time=1, duration=4),
                {"waveform": torch.zeros(1, 1, 64000), "sample_rate": 16000},
                InputImpl.VideoFromFile(io.BytesIO(self._clip)))


NODE_CLASS_MAPPINGS["CuddlerHostMediaFixture"] = NativeMediaFixture


def _image_bytes():
    from PIL import Image
    target = io.BytesIO()
    Image.new("RGB", (16, 16), (32, 96, 160)).save(target, format="PNG")
    return target.getvalue()


def _audio_bytes():
    import wave
    target = io.BytesIO()
    with wave.open(target, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(b"\x00\x00" * 1600)
    return target.getvalue()


def _plugin():
    suffix = os.path.join("custom_nodes", "cuddler", "__init__.py")
    for name, module in list(sys.modules.items()):
        if (getattr(module, "__file__", None) or "").endswith(suffix):
            return name, module
    raise RuntimeError("the cuddler plugin is not loaded")


class FakePlatform:
    def __init__(self, response_class, catalog):
        self._response = response_class
        self._catalog = catalog
        self._media = _video_bytes()
        self._creative_media = {"image": _image_bytes(), "speech": _audio_bytes(), "music": _audio_bytes()}
        self.jobs = {}
        self.uploads, self.upload_keys, self.uploaded = {}, {}, set()
        self.creates = self.downloads = self.unexpected = 0
        self.create_requests = self.first_frame_creates = self.video_reads = 0
        self.upload_requests = self.upload_creates = self.upload_puts = 0
        self.upload_completes = self.upload_reads = 0
        self.video_modes = set()
        self.native_uploads = set()
        self.long_dialogues = self.precise_video_quotes = self.last_frames = 0

    def _json(self, status, body):
        return self._response(status, {"content-type": "application/json"}, json.dumps(body).encode())

    def _refuse(self):
        self.unexpected += 1
        return self._json(400, {"code": "invalid_request", "message": "not faked", "request_id": "req_fake"})

    @staticmethod
    def _media_headers_safe(request):
        return not any(name.lower() in {"authorization", "cookie", "proxy-authorization"}
                       for name in request.headers)

    def send(self, request):
        if request.method == "PUT" and request.url.startswith(UPLOAD):
            upload_id = request.url[len(UPLOAD):]
            upload = self.uploads.get(upload_id)
            if not upload or not self._media_headers_safe(request) or len(request.body or b"") != upload["bytes"]:
                return self._refuse()
            try:
                if b"PRIVATE_WORKFLOW_MARKER" in request.body:
                    return self._refuse()
                if upload["kind"] == "image":
                    from PIL import Image
                    with Image.open(io.BytesIO(request.body)) as image:
                        image.load()
                        if image.format != "PNG" or image.size not in {(16, 16), (720, 720)} or image.info:
                            return self._refuse()
                else:
                    import av
                    with av.open(io.BytesIO(request.body)) as container:
                        if upload["kind"] == "video":
                            stream = container.streams.video[0]
                            duration = float(stream.duration * stream.time_base)
                            if not any(abs(duration - expected) < 0.05 for expected in (4, 6)) or "comment" in container.metadata:
                                return self._refuse()
                        else:
                            frame = next(container.decode(audio=0))
                            if frame.sample_rate != 16000:
                                return self._refuse()
                if upload["kind"] not in {"image", "video", "audio"}:
                        return self._refuse()
                self.native_uploads.add(upload["kind"])
            except (OSError, ValueError, IndexError):
                return self._refuse()
            self.upload_puts += 1
            self.uploaded.add(upload_id)
            return self._response(200, {}, b"")
        path = request.url[len(API):] if request.url.startswith(API) else request.url
        if (request.method, path) == ("GET", "/catalog"):
            return self._json(200, self._catalog)
        if not request.headers.get("Authorization", "").startswith("Bearer cud_sk_"):
            return self._json(401, {"code": "unauthorized", "message": "bad key", "request_id": "req_fake"})
        if (request.method, path) == ("GET", "/balance"):
            return self._json(200, BALANCE)
        if (request.method, path) == ("POST", "/videos/estimate"):
            body = json.loads(request.body)
            if body.get("model") == "minimax-hailuo-2.3" and body.get("generate_audio", True):
                return self._refuse()
            if set(body) & {"prompt", "seed", "task_type", "return_last_frame", "camera_fixed", "reference_videos"}:
                return self._refuse()
            if "input_video_seconds" in body:
                if not any(abs(body["input_video_seconds"] - expected) < 0.05 for expected in (4, 6)):
                    return self._refuse()
                self.precise_video_quotes += 1
            return self._json(200, ESTIMATE)
        for kind, endpoint, prefix in (("image", "images", "img"), ("speech", "speech", "aud"),
                                       ("music", "music", "mus")):
            estimate = {**ESTIMATE, "object": kind + "_estimate"}
            if (request.method, path) == ("POST", f"/{endpoint}/estimate"):
                return self._json(200, estimate)
            if (request.method, path) == ("POST", f"/{endpoint}"):
                self.create_requests += 1
                key = request.headers.get("Idempotency-Key")
                if not key:
                    return self._refuse()
                body = json.loads(request.body)
                if kind == "speech" and len(body.get("dialogue", [])) == 50:
                    self.long_dialogues += 1
                for upload_id in body.get("reference_images", []):
                    if self.uploads.get(upload_id, {}).get("status") != "ready":
                        return self._refuse()
                if key not in self.jobs:
                    self.creates += 1
                    self.jobs[key] = {"object": kind, "id": prefix + "_" + f"{self.creates:020d}",
                        "model": body["model"], "status": "succeeded", "estimate": estimate,
                        "output": {"url": OUTPUT + "/" + kind, "retained_until": "2099-01-01T00:00:00Z"},
                        "error": None}
                return self._json(200, self.jobs[key])
            if request.method == "GET" and path.startswith(f"/{endpoint}/"):
                job = next((job for job in self.jobs.values() if path == f"/{endpoint}/" + job["id"]), None)
                if job:
                    return self._json(200, job)
        if (request.method, path) == ("POST", "/uploads"):
            self.upload_requests += 1
            key = request.headers.get("Idempotency-Key")
            if not key:
                return self._refuse()
            if key not in self.upload_keys:
                body = json.loads(request.body)
                if body.get("content_type") != {"image": "image/png", "video": "video/mp4", "audio": "audio/wav"}.get(body.get("kind")):
                    return self._refuse()
                self.upload_creates += 1
                upload_id = "upl_" + f"{self.upload_creates:020d}"
                self.upload_keys[key] = upload_id
                self.uploads[upload_id] = {"object": "upload", "id": upload_id, "kind": body["kind"],
                    "status": "pending", "content_type": body["content_type"], "bytes": body["bytes"],
                    "expires_at": "2099-01-01T00:00:00Z", "upload_url": UPLOAD + upload_id,
                    "upload_headers": {"Content-Type": body["content_type"]}}
            return self._json(201, self.uploads[self.upload_keys[key]])
        if path.startswith("/uploads/"):
            upload_id = path.split("/")[2]
            upload = self.uploads.get(upload_id)
            if upload and request.method == "POST" and path == f"/uploads/{upload_id}/complete":
                if upload_id not in self.uploaded:
                    return self._refuse()
                self.upload_completes += 1
                upload["status"] = "ready"
                return self._json(200, upload)
            if upload and request.method == "GET" and path == f"/uploads/{upload_id}":
                self.upload_reads += 1
                return self._json(200, upload)
        if (request.method, path) == ("POST", "/videos"):
            self.create_requests += 1
            key = request.headers.get("Idempotency-Key")
            if not key:
                return self._json(400, {"code": "invalid_request"})
            body = json.loads(request.body)
            for name in ("first_frame", "last_frame", "reference_images", "reference_videos", "reference_audios"):
                ids = body.get(name, [])
                for upload_id in ([ids] if isinstance(ids, str) else ids):
                    if self.uploads.get(upload_id, {}).get("status") != "ready":
                        return self._refuse()
            self.video_modes.add(body.get("mode"))
            if body.get("model") == "minimax-hailuo-2.3" and body.get("generate_audio", True):
                return self._refuse()
            if body.get("mode") == "image":
                upload_id = body.get("first_frame")
                upload = self.uploads.get(upload_id)
                if not upload or upload["status"] != "ready" or upload_id not in self.uploaded:
                    return self._refuse()
            if key not in self.jobs:
                self.creates += 1
                self.first_frame_creates += body.get("mode") == "image"
                self.jobs[key] = {"object": "video", "id": "vid_" + f"{self.creates:020d}",
                    "model": body["model"], "status": "succeeded", "estimate": ESTIMATE,
                    "output": {"url": OUTPUT, "retained_until": "2099-01-01T00:00:00Z",
                               "last_frame_url": OUTPUT + "/image" if body.get("return_last_frame") else None}, "error": None}
                if body.get("return_last_frame"):
                    self.last_frames += 1
            return self._json(200, self.jobs[key])
        if request.method == "GET" and path.startswith("/videos/"):
            job = next((job for job in self.jobs.values() if path == "/videos/" + job["id"]), None)
            if job:
                self.video_reads += 1
                return self._json(200, job)
        self.unexpected += 1
        return self._json(404, {"code": "not_found", "message": "not faked", "request_id": "req_fake"})

    def stream(self, request, sink):
        for kind, media in self._creative_media.items():
            if request.method == "GET" and request.url == OUTPUT + "/" + kind:
                if not self._media_headers_safe(request):
                    return self._refuse()
                self.downloads += 1
                sink.write(media)
                return self._response(200, {}, b"")
        if request.method == "GET" and request.url == OUTPUT:
            if not self._media_headers_safe(request):
                return self._refuse()
            self.downloads += 1
            sink.write(self._media)
            return self._response(200, {"content-type": "video/mp4"}, b"")
        return self.send(request)


async def _install(app):
    global _fake
    name, module = _plugin()
    root = Path(module.__file__).resolve().parent
    catalog = json.loads((root / "data" / "catalog_snapshot.json").read_text(encoding="utf-8"))
    fake = FakePlatform(sys.modules[name + ".cuddler.transport"].Response, catalog)
    _fake = fake
    sys.modules[name + ".cuddler_host.runtime"].configure_transport(fake)
    print("cuddler host test: fake Platform installed", flush=True)


PromptServer.instance.app.on_startup.append(_install)


@PromptServer.instance.routes.get("/cuddler-host-test/stats")
async def _stats(_request):
    # Test-only route; no keys, request bodies, prompts or media URLs are exposed.
    return web.json_response({"creates": _fake.creates, "downloads": _fake.downloads,
                              "create_requests": _fake.create_requests, "first_frame_creates": _fake.first_frame_creates,
                              "video_reads": _fake.video_reads,
                              "upload_requests": _fake.upload_requests, "upload_creates": _fake.upload_creates,
                              "upload_puts": _fake.upload_puts, "upload_completes": _fake.upload_completes,
                              "upload_reads": _fake.upload_reads,
                              "video_modes": sorted(_fake.video_modes), "native_uploads": sorted(_fake.native_uploads),
                              "long_dialogues": _fake.long_dialogues, "precise_video_quotes": _fake.precise_video_quotes,
                              "last_frames": _fake.last_frames,
                              "unexpected": _fake.unexpected})
