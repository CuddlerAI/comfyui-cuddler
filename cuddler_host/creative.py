"""Image, speech and music nodes; all spending goes through the shared durable runner."""
import asyncio

from comfy_api.latest import io
from comfy import model_management
from server import PromptServer

from ..cuddler import audio, image
from ..cuddler.errors import CuddlerError
from ..cuddler.i18n import describe
from .media import download
from .runtime import get_runtime
from .video import frame_png, schema_catalog


def limits():
    return [io.Float.Input("max_usd", display_name="Spending limit (USD)", default=2.0,
                           min=0.01, max=1000.0, step=0.01),
            io.Int.Input("seed", default=0, min=0, max=2_147_483_647,
                         control_after_generate=io.ControlAfterGenerate.fixed)]


async def run(node, runtime, spec):
    client = runtime.client(live=True)

    def progress(event, value=None):
        messages = ({"uploading": "正在上传参考图", "queued": "排队中", "running": "生成中"}
                    if runtime.language() == "zh" else
                    {"uploading": "Uploading reference", "queued": "Queued", "running": "Generating"})
        PromptServer.instance.send_progress_text(messages[event], node.hidden.unique_id,
                                                PromptServer.instance.client_id)

    job = await runtime.job_runner(client).run(spec,
        interrupted=model_management.processing_interrupted, progress=progress)
    result = await asyncio.to_thread(download, client, spec.kind, job)
    return io.NodeOutput(result)


class CuddlerImage(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        selections = image.choices(schema_catalog())
        options = []
        for label, (model, mode, variant) in selections.items():
            inputs = [io.Combo.Input("resolution", options=model["resolutions"]),
                      io.Combo.Input("aspect_ratio", options=model["aspect_ratios"])]
            if mode != "text":
                inputs.append(io.Autogrow.Input("references", template=io.Autogrow.TemplatePrefix(
                    io.Image.Input("reference"), prefix="image", min=1,
                    max=min(10, model["max_reference_images"], model["max_references"]))))
            options.append(io.DynamicCombo.Option(label, inputs))
        return io.Schema(node_id="CuddlerImage", display_name="Cuddler Image", category="Cuddler",
            inputs=[io.DynamicCombo.Input("model", options=options, extra_dict={"cuddler_choices": {
                label: {"model": model["id"], "variant": variant, "mode": mode}
                for label, (model, mode, variant) in selections.items()}}),
                io.String.Input("prompt", multiline=True, default=""), *limits()],
            outputs=[io.Image.Output()], hidden=[io.Hidden.unique_id], is_experimental=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    async def execute(cls, model, prompt, max_usd, seed) -> io.NodeOutput:
        runtime = None
        try:
            runtime = get_runtime()
            if not isinstance(model, dict):
                raise CuddlerError("invalid_request")
            selected = dict(model)
            selection = selected.pop("model", None)
            refs = selected.pop("references", {})
            if not isinstance(refs, dict) or len(refs) > 10:
                raise CuddlerError("invalid_request")
            # Slot order, not link completion order, determines reference order and the fingerprint.
            ordered = []
            for index in range(10):
                key = f"image{index}"
                if key in refs:
                    ordered.append(await asyncio.to_thread(frame_png, refs[key]))
            if len(ordered) != len(refs):
                raise CuddlerError("invalid_request")
            spec = image.prepare(runtime.catalog.current(), selection, selected, prompt, seed,
                                 max_usd, references=ordered)
            return await run(cls, runtime, spec)
        except CuddlerError as error:
            raise RuntimeError(describe(error, runtime.language() if runtime else "en")) from None


class CuddlerSpeech(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        catalog = schema_catalog()
        options = []
        for label, model in audio.choices(catalog, "speech").items():
            voices = [voice["id"] for voice in catalog.voices.get(model["id"], [])]
            if not voices:
                continue
            inputs = [io.Combo.Input("voice_id", options=voices),
                      io.String.Input("text", multiline=True, default="")]
            if model["dialogue"]:
                inputs.extend([io.Boolean.Input("second_speaker", default=False),
                               io.Combo.Input("voice_id_2", options=voices),
                               io.String.Input("text_2", multiline=True, default="")])
                inputs.append(io.String.Input("dialogue_script", multiline=True, default="",
                    tooltip="Up to 50 turns, one per line: 1: Hello / 2: Hi. Use speaker numbers 1–10. "
                            "Leave text empty and turn off second_speaker when using this script."))
                inputs.extend(io.Combo.Input(f"voice_id_{i}", options=voices) for i in range(3, 11))
            options.append(io.DynamicCombo.Option(label, inputs))
        return io.Schema(node_id="CuddlerSpeech", display_name="Cuddler Speech", category="Cuddler",
            inputs=[io.DynamicCombo.Input("model", options=options, extra_dict={"cuddler_choices": {
                label: {"model": model["id"]} for label, model in audio.choices(catalog, "speech").items()}}),
                *limits()], outputs=[io.Audio.Output()], hidden=[io.Hidden.unique_id], is_experimental=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    async def execute(cls, model, max_usd, seed) -> io.NodeOutput:
        runtime = None
        try:
            runtime = get_runtime()
            lines = audio.speech_lines(model)
            spec = audio.prepare_speech(runtime.catalog.current(), model.get("model"), lines, seed, max_usd)
            return await run(cls, runtime, spec)
        except CuddlerError as error:
            raise RuntimeError(describe(error, runtime.language() if runtime else "en")) from None


class CuddlerMusic(io.ComfyNode):
    @classmethod
    def define_schema(cls):
        selections = audio.choices(schema_catalog(), "music")
        return io.Schema(node_id="CuddlerMusic", display_name="Cuddler Music", category="Cuddler",
            inputs=[io.Combo.Input("model", options=list(selections), extra_dict={"cuddler_choices": {
                label: {"model": model["id"]} for label, model in selections.items()}}),
                io.String.Input("prompt", multiline=True, default=""), *limits()],
            outputs=[io.Audio.Output()], hidden=[io.Hidden.unique_id], is_experimental=True)

    @classmethod
    def fingerprint_inputs(cls, **kwargs):
        return float("nan")

    @classmethod
    async def execute(cls, model, prompt, max_usd, seed) -> io.NodeOutput:
        runtime = None
        try:
            runtime = get_runtime()
            spec = audio.prepare_music(runtime.catalog.current(), model, prompt, seed, max_usd)
            return await run(cls, runtime, spec)
        except CuddlerError as error:
            raise RuntimeError(describe(error, runtime.language() if runtime else "en")) from None
