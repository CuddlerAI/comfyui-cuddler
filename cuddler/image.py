"""Catalog-driven image requests; validate reference counts before uploading."""
from .errors import CuddlerError
from .jobs import JobSpec, MediaInput
from .money import cap_from_widget


def choices(catalog):
    return {f'{model["display_name"]} ({model["id"]}) / {choice["mode"]} / {choice["variant"]}':
            (model, choice["mode"], choice["variant"])
            for model in catalog.of_kind("image") for choice in model["choices"]}


def prepare(catalog, selection, parameters, prompt, seed, cap, *, references=()):
    selected = choices(catalog).get(selection)
    if selected is None or not isinstance(parameters, dict):
        raise CuddlerError("invalid_request")
    model, mode, variant = selected
    if (set(parameters) != {"resolution", "aspect_ratio"}
            or parameters["resolution"] not in model["resolutions"]
            or parameters["aspect_ratio"] not in model["aspect_ratios"]
            or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 4000
            or type(seed) is not int or not 0 <= seed <= 2_147_483_647
            or not isinstance(references, (tuple, list))):
        raise CuddlerError("invalid_request")
    maximum = min(10, model["max_reference_images"], model["max_references"])
    if (mode == "text" and references or mode != "text" and not references
            or len(references) > maximum):
        raise CuddlerError("invalid_request")
    media = []
    for index, data in enumerate(references):
        if not isinstance(data, bytes) or not data:
            raise CuddlerError("invalid_request")
        if len(data) > 30 * 1024 * 1024:
            raise CuddlerError("media_too_large", detail={"limit": "30 MiB"})
        media.append(MediaInput("reference_images", data, "image", "image/png",
                                f"reference-{index + 1}.png", many=True))
    body = {"model": model["id"], "mode": mode, "variant": variant,
            "prompt": prompt.strip(), **parameters}
    if "seed" in model["request_fields"]:
        body["seed"] = seed
    # The image estimate contract has no mode or content field.
    estimate = {"model": model["id"], "variant": variant, **parameters,
                "reference_image_count": len(media)}
    return JobSpec("image", body, estimate, cap_from_widget(cap), seed=seed, media=media)
