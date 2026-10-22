"""Speech and music requests using the same durable runner as video and image."""
import re
from .errors import CuddlerError
from .jobs import JobSpec
from .money import cap_from_widget


def choices(catalog, kind):
    return {f'{model["display_name"]} ({model["id"]})': model
            for model in catalog.of_kind(kind)}


def _seed(seed):
    if type(seed) is not int or not 0 <= seed <= 2_147_483_647:
        raise CuddlerError("invalid_request")


def speech_lines(values):
    """A numbered dialogue script uses the ten visible voice selectors, never raw API JSON."""
    allowed = {"model", "text", "voice_id", "second_speaker", "text_2", "dialogue_script"}
    allowed.update(f"voice_id_{i}" for i in range(2, 11))
    if not isinstance(values, dict) or values.keys() - allowed:
        raise CuddlerError("invalid_request")
    script = values.get("dialogue_script", "")
    second = values.get("second_speaker", False)
    if not isinstance(script, str) or type(second) is not bool:
        raise CuddlerError("invalid_request")
    if script.strip():
        text = values.get("text", "")
        if not isinstance(text, str) or text.strip() or second:
            raise CuddlerError("invalid_request")
        lines = []
        for turn in script.splitlines():
            if not turn.strip():
                continue
            match = re.fullmatch(r"\s*(10|[1-9])\s*:\s*(\S.*)", turn)
            if not match or len(lines) >= 50:
                raise CuddlerError("invalid_request")
            speaker = int(match[1])
            lines.append({"voice_id": values.get("voice_id" if speaker == 1 else f"voice_id_{speaker}"),
                          "text": match[2].strip()})
        return lines
    lines = [{"voice_id": values.get("voice_id"), "text": values.get("text")}]
    if second:
        lines.append({"voice_id": values.get("voice_id_2"), "text": values.get("text_2")})
    return lines


def prepare_speech(catalog, selection, lines, seed, cap):
    model = choices(catalog, "speech").get(selection)
    _seed(seed)
    if model is None or not isinstance(lines, list) or not 1 <= len(lines) <= 50:
        raise CuddlerError("invalid_request")
    voices = {voice["id"] for voice in catalog.voices.get(model["id"], [])}
    for line in lines:
        if (not isinstance(line, dict) or set(line) != {"voice_id", "text"}
                or not isinstance(line["voice_id"], str) or line["voice_id"] not in voices
                or not isinstance(line["text"], str) or not line["text"].strip()):
            raise CuddlerError("invalid_request")
    if (sum(len(line["text"]) for line in lines) > model["max_characters"]
            or len({line["voice_id"] for line in lines}) > 10
            or len(lines) > 1 and not model["dialogue"]):
        raise CuddlerError("invalid_request")
    content = dict(lines[0]) if len(lines) == 1 else {"dialogue": [dict(line) for line in lines]}
    body = {"model": model["id"], "output_format": "mp3", **content}
    # Speech estimation needs the actual text to meter Unicode/UTF-8 correctly.
    # It is sent only to the same Platform origin, never logged or persisted by this module.
    return JobSpec("speech", body, dict(body), cap_from_widget(cap), seed=seed)


def prepare_music(catalog, selection, prompt, seed, cap):
    model = choices(catalog, "music").get(selection)
    _seed(seed)
    if model is None or not isinstance(prompt, str) or not 1 <= len(prompt.strip()) <= 2000:
        raise CuddlerError("invalid_request")
    body = {"model": model["id"], "prompt": prompt.strip(), "output_format": "mp3"}
    return JobSpec("music", body, dict(body), cap_from_widget(cap), seed=seed)
