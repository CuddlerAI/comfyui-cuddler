"""Bounded Platform media downloads converted to native ComfyUI IMAGE/AUDIO."""
import io

from ..cuddler.errors import CuddlerError

MAX_IMAGE_BYTES = 30 * 1024 * 1024
MAX_AUDIO_BYTES = 50 * 1024 * 1024
MAX_IMAGE_PIXELS = 32_000_000
MAX_AUDIO_SECONDS = 600


def image_tensor(source):
    import numpy as np
    import torch
    from PIL import Image, ImageOps

    with Image.open(source) as image:
        if image.width * image.height > MAX_IMAGE_PIXELS or image.width < 1 or image.height < 1:
            raise CuddlerError("media_too_large", detail={"limit": "32 million pixels"})
        array = np.asarray(ImageOps.exif_transpose(image).convert("RGB"), dtype=np.float32) / 255.0
    return torch.from_numpy(array.copy()).unsqueeze(0)


def audio_tensor(source):
    import av
    import numpy as np
    import torch

    parts, size = [], 0
    with av.open(source) as container:
        if not container.streams.audio:
            raise CuddlerError("invalid_response")
        stream = container.streams.audio[0]
        rate = stream.codec_context.sample_rate
        channels = stream.codec_context.channels
        if not rate or not 8_000 <= rate <= 192_000 or channels not in (1, 2):
            raise CuddlerError("invalid_response")
        resampler = av.AudioResampler(format="fltp", layout="mono" if channels == 1 else "stereo", rate=rate)

        def append(frame):
            nonlocal size
            array = frame.to_ndarray()
            size += array.size
            if size > rate * channels * MAX_AUDIO_SECONDS:
                raise CuddlerError("media_too_large", detail={"limit": "10 minutes of audio"})
            parts.append(array)

        for frame in container.decode(stream):
            for converted in resampler.resample(frame):
                append(converted)
        for converted in resampler.resample(None):
            append(converted)
    if not parts:
        raise CuddlerError("invalid_response")
    waveform = np.concatenate(parts, axis=1)
    if not np.isfinite(waveform).all():
        raise CuddlerError("invalid_response")
    return {"waveform": torch.from_numpy(waveform.copy()).unsqueeze(0), "sample_rate": rate}


def download(client, kind, job):
    limit = MAX_IMAGE_BYTES if kind == "image" else MAX_AUDIO_BYTES
    for attempt in range(2):
        source = io.BytesIO()
        try:
            client.download(job["output"]["url"], source, limit=limit)
        except CuddlerError:
            if attempt:
                raise
            job = client.get(kind, job["id"])
            continue
        source.seek(0)
        try:
            return image_tensor(source) if kind == "image" else audio_tensor(source)
        except CuddlerError:
            raise
        except Exception:
            raise CuddlerError("invalid_response") from None
