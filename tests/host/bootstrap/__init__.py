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


def _video_bytes():
    """A real tiny MP4, encoded only by the host CI's existing PyAV dependency."""
    import av
    import numpy as np

    target = io.BytesIO()
    with av.open(target, mode="w", format="mp4") as container:
        stream = container.add_stream("mpeg4", rate=4)
        stream.width = stream.height = 16
        stream.pix_fmt = "yuv420p"
        for value in (32, 64, 96, 128):
            frame = av.VideoFrame.from_ndarray(np.full((16, 16, 3), value, dtype=np.uint8), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
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
        self.jobs = {}
        self.uploads, self.upload_keys, self.uploaded = {}, {}, set()
        self.creates = self.downloads = self.unexpected = 0
        self.create_requests = self.first_frame_creates = self.video_reads = 0
        self.upload_requests = self.upload_creates = self.upload_puts = 0
        self.upload_completes = self.upload_reads = 0

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
            from PIL import Image
            try:
                with Image.open(io.BytesIO(request.body)) as image:
                    image.load()
                    if image.format != "PNG" or image.size != (16, 16) or image.info:
                        return self._refuse()
            except (OSError, ValueError):
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
            return self._json(200, ESTIMATE)
        if (request.method, path) == ("POST", "/uploads"):
            self.upload_requests += 1
            key = request.headers.get("Idempotency-Key")
            if not key:
                return self._refuse()
            if key not in self.upload_keys:
                body = json.loads(request.body)
                if body.get("kind") != "image" or body.get("content_type") != "image/png":
                    return self._refuse()
                self.upload_creates += 1
                upload_id = "upl_" + f"{self.upload_creates:020d}"
                self.upload_keys[key] = upload_id
                self.uploads[upload_id] = {"object": "upload", "id": upload_id, "kind": "image",
                    "status": "pending", "content_type": "image/png", "bytes": body["bytes"],
                    "expires_at": "2099-01-01T00:00:00Z", "upload_url": UPLOAD + upload_id,
                    "upload_headers": {"Content-Type": "image/png"}}
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
                    "output": {"url": OUTPUT, "retained_until": "2099-01-01T00:00:00Z"}, "error": None}
            return self._json(200, self.jobs[key])
        if request.method == "GET" and path.startswith("/videos/"):
            job = next((job for job in self.jobs.values() if path == "/videos/" + job["id"]), None)
            if job:
                self.video_reads += 1
                return self._json(200, job)
        self.unexpected += 1
        return self._json(404, {"code": "not_found", "message": "not faked", "request_id": "req_fake"})

    def stream(self, request, sink):
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
                              "unexpected": _fake.unexpected})
