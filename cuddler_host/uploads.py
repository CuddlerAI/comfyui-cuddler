"""Encode native reference media, stripping workflow metadata before upload."""
import io
import math
import wave

from ..cuddler.errors import CuddlerError
from ..cuddler.video import VideoReference


class BoundedBuffer(io.BytesIO):
    def __init__(self, limit):
        super().__init__()
        self.limit = limit

    def write(self, data):
        if self.tell() + len(data) > self.limit:
            raise CuddlerError("media_too_large", detail={"limit": f"{self.limit // 1024 // 1024} MiB"})
        return super().write(data)


def video_mp4(video):
    import av
    from comfy_api.latest import Types

    try:
        seconds = float(video.get_duration())
        width, height = video.get_dimensions()
        fps = float(video.get_frame_rate())
        if (not math.isfinite(seconds) or not 2 <= seconds <= 30 or min(width, height) < 1
                or width * height > 8_294_400 or not math.isfinite(fps) or not 1 <= fps <= 120):
            raise CuddlerError("invalid_request")
        source = BoundedBuffer(200 * 1024 * 1024)
        # save_to honours native trim ranges; get_stream_source does not.
        video.save_to(source, format=Types.VideoContainer.MP4)
        source.seek(0)
        output = BoundedBuffer(200 * 1024 * 1024)
        with av.open(source) as container, av.open(output, "w", format="mp4") as target:
            streams = {}
            for stream in container.streams:
                if stream.type in {"video", "audio"}:
                    copied = target.add_stream_from_template(stream)
                    copied.metadata.clear()
                    streams[stream.index] = copied
            # Neither container tags nor subtitle/data tracks travel to the Platform.
            for packet in container.demux():
                if packet.dts is not None and packet.stream.index in streams:
                    packet.stream = streams[packet.stream.index]
                    target.mux(packet)
        output.seek(0)
        with av.open(output) as verified:
            stream = verified.streams.video[0]
            if stream.duration is None or stream.time_base is None:
                raise CuddlerError("invalid_request")
            # Price the exact encoded clip that is uploaded, including a host trim.
            duration = float(stream.duration * stream.time_base)
            if not math.isfinite(duration) or not 2 <= duration <= 30:
                raise CuddlerError("invalid_request")
            result = VideoReference(output.getvalue(), duration, stream.width, stream.height,
                                    float(stream.average_rate or fps))
        return result
    except CuddlerError:
        raise
    except Exception:
        raise CuddlerError("invalid_request") from None


def audio_wav(audio):
    import numpy as np

    try:
        if not isinstance(audio, dict):
            raise CuddlerError("invalid_request")
        rate, tensor = audio.get("sample_rate"), audio.get("waveform")
        if (type(rate) is not int or not 8_000 <= rate <= 192_000 or getattr(tensor, "ndim", None) != 3
                or tensor.shape[0] != 1 or tensor.shape[1] not in (1, 2) or not 2 <= tensor.shape[2] / rate <= 15):
            raise CuddlerError("invalid_request")
        values = tensor[0].detach().cpu().numpy()
        if not np.isfinite(values).all():
            raise CuddlerError("invalid_request")
        samples = (np.clip(values, -1, 1).T * 32767).astype("<i2")
        output = BoundedBuffer(50 * 1024 * 1024)
        with wave.open(output, "wb") as container:
            container.setnchannels(values.shape[0])
            container.setsampwidth(2)
            container.setframerate(rate)
            container.writeframes(samples.tobytes())
        return output.getvalue()
    except CuddlerError:
        raise
    except Exception:
        raise CuddlerError("invalid_request") from None


def ordered_slots(group, prefix, maximum):
    if not isinstance(group, dict) or set(group) - {f"{prefix}{i}" for i in range(maximum)}:
        raise CuddlerError("invalid_request")
    return [group[f"{prefix}{i}"] for i in range(maximum) if group.get(f"{prefix}{i}") is not None]
