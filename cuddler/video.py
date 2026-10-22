"""Catalog-driven video requests, including bounded reference media and exact quotes."""
from dataclasses import dataclass, field
import math
from .errors import CuddlerError
from .jobs import JobSpec, MediaInput
from .money import cap_from_widget

VIDEO_MODES = {"text": "Text to video", "image": "First frame to video",
               "first-last-frame": "First and last frame", "reference": "Reference to video",
               "image-reference": "Image reference", "video-reference": "Video reference",
               "edit": "Edit video", "extend": "Extend video", "motion-control": "Motion control"}
MAX_FRAME_BYTES = 30 * 1024 * 1024
ESTIMATE_FIELDS = {"model", "mode", "variant", "duration", "resolution", "aspect_ratio", "generate_audio", "fps"}


@dataclass(frozen=True)
class VideoReference:
    data: bytes = field(repr=False)
    seconds: float
    width: int
    height: int
    fps: float


def request_fields(model):
    # These original token-priced entries deliberately predate request_fields.
    # Preserve that documented contract only for the named legacy models.
    if not model["request_fields"] and model["id"] in {"seedance-2.5", "seedance-2.0"}:
        fields = {"first_frame", "last_frame", "reference_images", "reference_videos", "reference_audios",
                  "generate_audio", "return_last_frame"}
        if model["id"] == "seedance-2.5":
            fields.update({"seed", "camera_fixed", "task_type"})
        return fields
    return set(model["request_fields"])


def choices(catalog):
    """Stable, unambiguous selector labels; unsupported modes are not advertised."""
    result = {}
    for model in catalog.of_kind("video"):
        for choice in model["choices"]:
            mode, variant = choice["mode"], choice["variant"]
            if mode in VIDEO_MODES:
                label = f'{model["display_name"]} ({model["id"]}) / {VIDEO_MODES[mode]} / {variant}'
                result[label] = (model, mode, variant)
    return result


def aspect_ratios(model, mode):
    ratios = model["aspect_ratios"]
    if mode in {"image", "first-last-frame", "edit", "extend", "motion-control"} and "adaptive" in ratios:
        return ["adaptive"]
    return [ratio for ratio in ratios if ratio != "adaptive"]


def discrete_durations(model, mode):
    # /v1/models has model-wide bounds; Kling's quote contract narrows these modes.
    # Keep aligned with the API's shared platformKlingDurations capability rules.
    if mode not in {"edit", "motion-control"} and (model["id"] in {"kling-2.5-turbo", "kling-2.6"}
            or model["id"] == "kling-o1" and mode in {"text", "first-last-frame"}):
        return (5, 10)
    return ()


def defaults(model, mode, variant):
    rows = [row for row in model.get("pricing", [])
            if row.get("mode", mode) == mode and row.get("variant", variant) == variant]
    row = rows[0] if rows else {}
    value = {"duration": row.get("duration", max(1, model["min_duration"])),
             "resolution": row.get("resolution", model["resolutions"][0]),
             "aspect_ratio": aspect_ratios(model, mode)[0]}
    durations = discrete_durations(model, mode)
    if durations and value["duration"] not in durations:
        value["duration"] = durations[0]
    fields = request_fields(model)
    if "generate_audio" in fields:
        value["generate_audio"] = row.get("generate_audio", model.get("native_audio", False))
    if "fps" in fields:
        value["fps"] = row.get("fps", 24)
    for name in ("camera_fixed", "return_last_frame"):
        if name in fields:
            value[name] = False
    if mode == "motion-control":
        value["character_orientation"] = "video"
    return value


def _bytes(data, limit):
    if not isinstance(data, bytes) or not data:
        raise CuddlerError("invalid_request")
    if len(data) > limit * 1024 * 1024:
        raise CuddlerError("media_too_large", detail={"limit": f"{limit} MiB"})


def _references(model, mode, parameters, first_frame, last_frame, images, videos, audios):
    fields = request_fields(model)
    counts = (len(images), len(videos), len(audios))
    for name, count, maximum in zip(("reference_images", "reference_videos", "reference_audios"), counts,
                                    (model["max_reference_images"], model["max_reference_videos"], model["max_reference_audios"])):
        if count > maximum or count and name not in fields:
            raise CuddlerError("invalid_request")
    if sum(counts) > model["max_references"] or (audios and not images and not videos and not model.get("audio_only_reference", False)):
        raise CuddlerError("invalid_request")
    frames = first_frame is not None or last_frame is not None
    if ((frames and sum(counts)) or (last_frame is not None and (first_frame is None or "last_frame" not in fields))
            or (mode in {"image", "first-last-frame"} and first_frame is None)
            or (mode == "first-last-frame" and last_frame is None)
            or (mode not in {"image", "first-last-frame"} and frames)
            or (mode == "text" and sum(counts)) or (mode == "reference" and not sum(counts))
            or (mode == "image-reference" and (not images or videos or audios))
            or (mode in {"video-reference", "edit", "extend", "motion-control"} and not videos)):
        raise CuddlerError("invalid_request")
    kling = model["id"].startswith("kling-")
    if kling and ((mode == "motion-control" and counts != (1, 1, 0))
                  or (mode in {"video-reference", "edit"} and (len(videos) != 1 or len(images) > 4))
                  or (last_frame is not None and (parameters.get("variant") == "turbo" or
                      model["id"] in {"kling-2.5-turbo", "kling-2.6"} and parameters["resolution"] != "1080p"))):
        raise CuddlerError("invalid_request")
    media = []
    for name, data in (("first_frame", first_frame), ("last_frame", last_frame)):
        if data is not None:
            _bytes(data, 30)
            media.append(MediaInput(name, data, "image", "image/png", name + ".png"))
    for name, items, kind, mime, suffix, limit in (
            ("reference_images", images, "image", "image/png", "png", 30),
            ("reference_videos", videos, "video", "video/mp4", "mp4", 200),
            ("reference_audios", audios, "audio", "audio/wav", "wav", 50)):
        seen = set()
        for index, item in enumerate(items):
            if kind == "video":
                if not isinstance(item, VideoReference) or not math.isfinite(item.seconds) or not 2 <= item.seconds <= 30:
                    raise CuddlerError("invalid_request")
                if kling:
                    motion = mode == "motion-control"
                    maximum = (10 if parameters.get("character_orientation") == "image" else 30) if motion else (10 if model["id"] == "kling-o1" else 15.5)
                    if (not 3 <= item.seconds <= maximum or min(item.width, item.height) < (340 if motion else 700)
                            or max(item.width, item.height) > (3850 if motion else 2160 if model["id"] == "kling-o1" else 4553)
                            or not motion and not 24 <= item.fps <= 60):
                        raise CuddlerError("invalid_request")
                    limit = 100 if motion else 200
                elif item.seconds > (30 if model["id"] == "seedance-2.5" else 15):
                    raise CuddlerError("invalid_request")
                if model["id"] == "seedance-2.5" and mode == "edit" and item.seconds < 4:
                    raise CuddlerError("invalid_request")
                data = item.data
            else:
                data = item
            _bytes(data, limit)
            # Uploader deduplication would produce duplicate IDs, which the API refuses.
            if data in seen:
                raise CuddlerError("invalid_request")
            seen.add(data)
            media.append(MediaInput(name, data, kind, mime, f"reference-{index}.{suffix}", many=True))
    return media


def prepare(catalog, selection, parameters, prompt, seed, cap, *, first_frame=None, last_frame=None,
            reference_images=(), reference_videos=(), reference_audios=()):
    """Validate before any upload or charge. The server's estimate remains authoritative."""
    selected = choices(catalog).get(selection)
    if selected is None or not isinstance(parameters, dict):
        raise CuddlerError("invalid_request")
    model, mode, variant = selected
    fields = request_fields(model)
    allowed = {"duration", "resolution", "aspect_ratio"} | (fields & {"generate_audio", "fps", "camera_fixed", "return_last_frame"})
    if mode == "motion-control":
        allowed.add("character_orientation")
    if parameters.keys() - allowed:
        raise CuddlerError("invalid_request")
    duration = parameters.get("duration")
    if (type(duration) is not int or not max(1, model["min_duration"]) <= duration <= min(30, model["max_duration"])
            or discrete_durations(model, mode) and duration not in discrete_durations(model, mode)
            or parameters.get("resolution") not in model["resolutions"]
            or parameters.get("aspect_ratio") not in aspect_ratios(model, mode)
            or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= (2500 if model["id"].startswith("kling-") else 4000)
            or type(seed) is not int or not 0 <= seed <= 2_147_483_647):
        raise CuddlerError("invalid_request")
    if (any(name in parameters and type(parameters[name]) is not bool for name in ("generate_audio", "camera_fixed", "return_last_frame"))
            or "fps" in parameters and (type(parameters["fps"]) is not int or parameters["fps"] not in (24, 25, 48, 50))
            or "character_orientation" in parameters and parameters["character_orientation"] not in {"image", "video"}):
        raise CuddlerError("invalid_request")
    # A pricing row can constrain discrete durations/resolutions/audio by mode and variant.
    # Token-priced rows omit those dimensions and leave them to the model's range.
    dimensions = {"mode": mode, "variant": variant, **parameters}
    prices = model.get("pricing", [])
    if prices and not any(all(name not in row or row[name] == value for name, value in dimensions.items())
                          for row in prices):
        raise CuddlerError("invalid_request")
    # The API defaults generate_audio to true. Silent models do not advertise an audio
    # toggle, but still require an explicit false; omitting it would reject every run.
    body = {"model": model["id"], "mode": mode, "variant": variant,
            "prompt": prompt.strip(), "generate_audio": model.get("native_audio", True), **parameters}
    if "seed" in fields:
        body["seed"] = seed
    if mode in {"edit", "extend"} and "task_type" in fields:
        body["task_type"] = mode
    media = _references(model, mode, {**parameters, "variant": variant}, first_frame, last_frame,
                        reference_images, reference_videos, reference_audios)
    estimate = {name: value for name, value in body.items() if name in ESTIMATE_FIELDS}
    estimate["reference_image_count"] = len(reference_images) + (first_frame is not None) + (last_frame is not None)
    if reference_videos:
        estimate["input_video_seconds"] = sum(item.seconds for item in reference_videos)
        if model["id"].startswith("kling-") and mode in {"edit", "motion-control"}:
            # These modes render the source clip's duration, irrespective of the widget.
            body["duration"] = estimate["duration"] = math.ceil(reference_videos[0].seconds)
        if model["id"] == "seedance-2.5" and mode == "edit":
            estimate["duration"] = math.ceil(max(item.seconds for item in reference_videos))
    return JobSpec("video", body, estimate, cap_from_widget(cap), seed=seed, media=media)
