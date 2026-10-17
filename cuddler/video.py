"""Catalog-driven text and first-frame video requests for the first usable node."""
from .errors import CuddlerError
from .jobs import JobSpec, MediaInput
from .money import cap_from_widget

PREVIEW_MODES = {"text": "Text to video", "image": "First frame to video"}
MAX_FRAME_BYTES = 30 * 1024 * 1024


def choices(catalog):
    """Stable, unambiguous selector labels; unsupported modes are not advertised."""
    result = {}
    for model in catalog.of_kind("video"):
        for choice in model["choices"]:
            mode, variant = choice["mode"], choice["variant"]
            if mode in PREVIEW_MODES:
                label = f'{model["display_name"]} ({model["id"]}) / {PREVIEW_MODES[mode]} / {variant}'
                result[label] = (model, mode, variant)
    return result


def aspect_ratios(model, mode):
    ratios = model["aspect_ratios"]
    if mode == "image" and "adaptive" in ratios:
        return ["adaptive"]
    return [ratio for ratio in ratios if ratio != "adaptive"]


def defaults(model, mode, variant):
    rows = [row for row in model.get("pricing", [])
            if row.get("mode", mode) == mode and row.get("variant", variant) == variant]
    row = rows[0] if rows else {}
    value = {"duration": row.get("duration", max(1, model["min_duration"])),
             "resolution": row.get("resolution", model["resolutions"][0]),
             "aspect_ratio": aspect_ratios(model, mode)[0]}
    if "generate_audio" in model["request_fields"]:
        value["generate_audio"] = row.get("generate_audio", model.get("native_audio", False))
    return value


def prepare(catalog, selection, parameters, prompt, seed, cap, *, first_frame=None):
    """Validate before any upload or charge. The server's estimate remains authoritative."""
    selected = choices(catalog).get(selection)
    if selected is None or not isinstance(parameters, dict):
        raise CuddlerError("invalid_request")
    model, mode, variant = selected
    fields = model["request_fields"]
    allowed = {"duration", "resolution", "aspect_ratio"}
    if "generate_audio" in fields:
        allowed.add("generate_audio")
    if parameters.keys() - allowed:
        raise CuddlerError("invalid_request")
    duration = parameters.get("duration")
    if (type(duration) is not int or not max(1, model["min_duration"]) <= duration <= min(30, model["max_duration"])
            or parameters.get("resolution") not in model["resolutions"]
            or parameters.get("aspect_ratio") not in aspect_ratios(model, mode)
            or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000
            or type(seed) is not int or not 0 <= seed <= 2_147_483_647):
        raise CuddlerError("invalid_request")
    if "generate_audio" in parameters and type(parameters["generate_audio"]) is not bool:
        raise CuddlerError("invalid_request")
    # A pricing row can constrain discrete durations/resolutions/audio by mode and variant.
    # Token-priced rows omit those dimensions and leave them to the model's range.
    dimensions = {"mode": mode, "variant": variant, **parameters}
    prices = model.get("pricing", [])
    if prices and not any(all(name not in row or row[name] == value for name, value in dimensions.items())
                          for row in prices):
        raise CuddlerError("invalid_request")
    body = {"model": model["id"], "mode": mode, "variant": variant,
            "prompt": prompt.strip(), **parameters}
    if "seed" in fields:
        body["seed"] = seed
    media = []
    if mode == "image":
        if not isinstance(first_frame, bytes) or not first_frame:
            raise CuddlerError("invalid_request")
        if len(first_frame) > MAX_FRAME_BYTES:
            raise CuddlerError("media_too_large", detail={"limit": "30 MiB"})
        media.append(MediaInput("first_frame", first_frame, "image", "image/png", "first-frame.png"))
    elif first_frame is not None:
        raise CuddlerError("invalid_request")
    estimate = {name: value for name, value in body.items() if name not in {"prompt", "seed"}}
    return JobSpec("video", body, estimate, cap_from_widget(cap), seed=seed, media=media)
