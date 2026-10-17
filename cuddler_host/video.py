"""The first Video node: text/first-frame input and a native ComfyUI VIDEO output."""
import asyncio
import io as streams
import json

from comfy_api.latest import InputImpl, io
from comfy import model_management
from server import PromptServer

from ..cuddler.errors import CuddlerError
from ..cuddler.i18n import describe
from ..cuddler.catalog import parse
from ..cuddler.video import aspect_ratios, choices, defaults, prepare
from .runtime import SNAPSHOT, get_runtime

MAX_VIDEO_BYTES = 200 * 1024 * 1024


def schema_catalog():
    try:
        return get_runtime().catalog.current()
    except CuddlerError:
        # A damaged private job database must stop charging, not remove the node or settings UI.
        return parse(json.loads(SNAPSHOT.read_text(encoding="utf-8")))


def frame_png(image):
    """Encode one host IMAGE without workflow or prompt metadata; reject batches."""
    import numpy as np
    from PIL import Image

    if (getattr(image, "ndim", None) != 4 or image.shape[0] != 1 or image.shape[3] not in (3, 4)
            or min(image.shape[1:3]) < 1 or image.shape[1] * image.shape[2] > 30_000_000):
        raise CuddlerError("invalid_request")
    array = image[0].detach().cpu().numpy()
    if not np.isfinite(array).all():
        raise CuddlerError("invalid_request")
    buffer = streams.BytesIO()
    Image.fromarray(np.clip(array * 255, 0, 255).astype(np.uint8)).save(buffer, format="PNG")
    return buffer.getvalue()


def download(client, job):
    """Refresh the same job after an expired/broken link, never submit another job."""
    for attempt in range(2):
        buffer = streams.BytesIO()
        try:
            client.download(job["output"]["url"], buffer, limit=MAX_VIDEO_BYTES)
            buffer.seek(0)
            video = InputImpl.VideoFromFile(buffer)
            video.get_dimensions()  # validate the file before passing it downstream
            return video
        except CuddlerError:
            if attempt:
                raise
            job = client.get("video", job["id"])
        except Exception:
            raise CuddlerError("invalid_response") from None


class CuddlerVideo(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        options = []
        selections = choices(schema_catalog())
        for label, (model, mode, variant) in selections.items():
            initial = defaults(model, mode, variant)
            inputs = [
                io.Int.Input("duration", default=initial["duration"],
                             min=max(1, model["min_duration"]), max=min(30, model["max_duration"])),
                io.Combo.Input("resolution", options=model["resolutions"], default=initial["resolution"]),
                io.Combo.Input("aspect_ratio", options=aspect_ratios(model, mode), default=initial["aspect_ratio"]),
            ]
            if "generate_audio" in model["request_fields"]:
                inputs.append(io.Boolean.Input("generate_audio", default=initial["generate_audio"]))
            if mode == "image":
                inputs.append(io.Image.Input("first_frame"))
            options.append(io.DynamicCombo.Option(label, inputs))
        return io.Schema(
            node_id="CuddlerVideo", display_name="Cuddler Video", category="Cuddler",
            description="Text or first-frame video. Same inputs and seed recover the original job. "
                        "Connect VIDEO to Save Video. Other video modes are still in development.",
            inputs=[io.DynamicCombo.Input("model", options=options, extra_dict={"cuddler_choices": {
                        label: {"model": model["id"], "mode": mode, "variant": variant}
                        for label, (model, mode, variant) in selections.items()}}),
                    io.String.Input("prompt", multiline=True, default=""),
                    io.Float.Input("max_usd", display_name="Spending limit (USD)", default=2.0,
                                   min=0.01, max=1000.0, step=0.01),
                    io.Int.Input("seed", default=0, min=0, max=2_147_483_647,
                                 control_after_generate=io.ControlAfterGenerate.fixed)],
            outputs=[io.Video.Output()], hidden=[io.Hidden.unique_id], is_experimental=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        # Always consult the durable job record and credential guard. Host tensor caching cannot
        # prove a retained output is still available or that a changed key can read it.
        return float("nan")

    @classmethod
    async def execute(cls, model, prompt, max_usd, seed) -> io.NodeOutput:
        runtime = None
        try:
            runtime = get_runtime()
            selected = dict(model)
            selection = selected.pop("model", None)
            image = selected.pop("first_frame", None)
            frame = await asyncio.to_thread(frame_png, image) if image is not None else None
            spec = prepare(runtime.catalog.current(), selection, selected, prompt, seed, max_usd, first_frame=frame)
            client = runtime.client(live=True)

            def progress(event, value=None):
                messages = ({"uploading": "正在上传参考图", "queued": "排队中", "running": "生成中"}
                            if runtime.language() == "zh" else
                            {"uploading": "Uploading reference", "queued": "Queued", "running": "Generating"})
                text = messages[event]
                PromptServer.instance.send_progress_text(text, cls.hidden.unique_id, PromptServer.instance.client_id)

            job = await runtime.job_runner(client).run(spec,
                interrupted=model_management.processing_interrupted, progress=progress)
            video = await asyncio.to_thread(download, client, job)
            return io.NodeOutput(video)
        except CuddlerError as error:
            raise RuntimeError(describe(error, runtime.language() if runtime else "en")) from None
