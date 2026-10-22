"""HTTP checks against a running ComfyUI with the plugin and the test bootstrap installed.

    python tests/host/check_host.py --base http://127.0.0.1:8188 --user-dir ComfyUI/user
"""
import argparse
import io
import json
import os
from pathlib import Path
import stat
import sys
import time
import urllib.error
import urllib.request
import urllib.parse
import uuid

KEY = "cud_sk_live_" + "A" * 24


def call(base, method, path, body=None, *, origin=None, site=None, content_type="application/json"):
    target = urllib.parse.urlsplit(base)
    if target.scheme != "http" or target.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise AssertionError("host checks require loopback HTTP")
    headers = {"Content-Type": content_type} if body is not None else {}
    if origin:
        headers["Origin"] = origin
    if site:
        headers["Sec-Fetch-Site"] = site
    data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # always the local server
    try:
        with opener.open(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


def upload_first_frame(base):
    """Put a generated non-sensitive PNG into the host's real Load Image input directory."""
    from PIL import Image

    png = io.BytesIO()
    Image.new("RGB", (16, 16), (32, 96, 160)).save(png, format="PNG")
    name = "cuddler-host-test-" + uuid.uuid4().hex + ".png"
    boundary = "cuddler-host-test-boundary"
    body = (f'--{boundary}\r\nContent-Disposition: form-data; name="image"; filename="{name}"\r\n'
            'Content-Type: image/png\r\n\r\n').encode() + png.getvalue() + f"\r\n--{boundary}--\r\n".encode()
    status, raw = call(base, "POST", "/upload/image", body,
                       content_type="multipart/form-data; boundary=" + boundary)
    if status != 200:
        raise AssertionError("the host refused the synthetic first-frame PNG")
    image = json.loads(raw)
    if image.get("type") != "input" or image.get("name") != name:
        raise AssertionError("the host did not store the expected first-frame input")
    return "/".join(part for part in (image.get("subfolder"), image["name"]) if part)


def run_video(base, selection, *, audio=False, first_frame=None, seed=1729, parameters=None):
    """Submit the real host's flat DynamicCombo API inputs, then wait for SaveVideo."""
    inputs = {"model": selection, "model.duration": 4, "model.resolution": "720p",
              "model.aspect_ratio": "adaptive" if first_frame else "16:9",
              "prompt": "offline host integration", "seed": seed, "max_usd": 2.0}
    inputs.update(parameters or {})
    if audio:
        inputs["model.generate_audio"] = False
    if "(seedance-2.5)" in selection:
        inputs.update({"model.camera_fixed": False, "model.return_last_frame": False})
    workflow = {
        "1": {"class_type": "CuddlerVideo", "inputs": inputs},
        "2": {"class_type": "SaveVideo", "inputs": {"video": ["1", 0], "filename_prefix": "cuddler-host-test/video",
                                                   "format": "auto", "codec": "auto"}},
    }
    if first_frame:
        inputs["model.first_frame"] = ["3", 0]
        workflow["3"] = {"class_type": "LoadImage", "inputs": {"image": first_frame}}
    return run_workflow(base, workflow)["images"][0]


def run_workflow(base, workflow):
    status, raw = call(base, "POST", "/prompt", {"prompt": workflow, "client_id": "cuddler-host-test"})
    if status != 200 or "prompt_id" not in json.loads(raw):
        raise AssertionError(f"host rejected the workflow: {status} {raw[:500]!r}")
    prompt_id = json.loads(raw)["prompt_id"]
    end = time.monotonic() + 45
    while time.monotonic() < end:
        status, raw = call(base, "GET", "/history/" + prompt_id)
        history = json.loads(raw).get(prompt_id) if status == 200 else None
        if history:
            if history.get("status", {}).get("status_str") != "success":
                raise AssertionError(f"host execution failed: {str(history.get('status'))[:1000]}")
            return history["outputs"]["2"]
        time.sleep(0.2)
    raise AssertionError("the native-output workflow did not finish within 45 seconds")


def run_creative(base, node_id, *, reference=None):
    def values(schema):
        # V3 COMBO serializes its choices in options; V1 uses the type slot.
        return schema[0] if isinstance(schema[0], list) else schema[1]["options"]

    node = json.loads(call(base, "GET", "/object_info/" + node_id)[1])[node_id]
    inputs = {"seed": 2201, "max_usd": 2.0}
    model = node["input"]["required"]["model"]
    if node_id == "CuddlerMusic":
        inputs.update(model=values(model)[0], prompt="Original instrumental")
    else:
        options = model[1]["options"]
        option = (next(item for item in options if (" / reference / " if reference else " / text / ") in item["key"])
                  if node_id == "CuddlerImage" else options[0])
        inputs["model"] = option["key"]
        required = option["inputs"]["required"]
        if node_id == "CuddlerImage":
            inputs.update(prompt="An orange ball", **{
                "model.resolution": values(required["resolution"])[0],
                "model.aspect_ratio": values(required["aspect_ratio"])[0]})
        else:
            inputs.update({"model.voice_id": values(required["voice_id"])[0], "model.text": "Hello world."})
            if "second_speaker" in required:
                inputs.update({"model.second_speaker": True,
                               "model.voice_id_2": values(required["voice_id_2"])[-1],
                               "model.text_2": "Hello again."})
                inputs["model.dialogue_script"] = ""
                for index in range(3, 11):
                    inputs[f"model.voice_id_{index}"] = values(required[f"voice_id_{index}"])[0]
    image = node_id == "CuddlerImage"
    workflow = {
        "1": {"class_type": node_id, "inputs": inputs},
        "2": {"class_type": "SaveImage" if image else "SaveAudio", "inputs": {
            "images" if image else "audio": ["1", 0], "filename_prefix": "cuddler-host-test/" + node_id}},
    }
    if reference:
        inputs["model.references.image0"] = ["3", 0]
        workflow["3"] = {"class_type": "LoadImage", "inputs": {"image": reference}}
    output = run_workflow(base, workflow)
    saved = output["images" if image else "audio"][0]
    status, media = call(base, "GET", "/view?" + urllib.parse.urlencode(saved))
    if status != 200:
        return False
    if image:
        from PIL import Image
        with Image.open(io.BytesIO(media)) as decoded:
            return decoded.size == (16, 16) and decoded.mode == "RGB"
    import av
    with av.open(io.BytesIO(media)) as decoded:
        frame = next(decoded.decode(audio=0))
        return frame.sample_rate == 16000 and len(frame.layout.channels) == 1


def decoded_video(base, saved):
    """Decode the real SaveVideo file, shared by text and first-frame workflows."""
    import av

    status, media = call(base, "GET", "/view?" + urllib.parse.urlencode(saved))
    if status != 200:
        return False
    with av.open(io.BytesIO(media)) as container:
        frame = next(container.decode(video=0))
        return (frame.width, frame.height) == (16, 16)


def dynamic_defaults(option):
    result = {"model": option["key"]}
    for name, spec in option["inputs"].get("required", {}).items():
        kind, settings = spec[0], spec[1] if len(spec) > 1 else {}
        options = kind if isinstance(kind, list) else settings.get("options", [])
        if "default" in settings:
            result["model." + name] = settings["default"]
        elif options and kind != "AUTOGROW":
            result["model." + name] = options[0]
    return result


def run_advanced_video(base, options, model_id, mode_label, *, last=False, untrimmed=False):
    option = next(item for item in options if f"({model_id}) / {mode_label} /" in item["key"])
    inputs = {**dynamic_defaults(option), "prompt": "Offline native media test", "max_usd": 2.0, "seed": 3101}
    prefix = "model."
    if mode_label in {"First frame to video", "First and last frame"}:
        inputs[prefix + "first_frame"] = ["3", 0]
        inputs[prefix + "last_frame"] = ["3", 1]
    else:
        if mode_label not in {"Extend video", "Video reference"}:
            inputs[prefix + "reference_images.image0"] = ["3", 0]
        if mode_label != "Image reference":
            inputs[prefix + "reference_videos.video0"] = ["3", 4 if untrimmed else 2]
        if mode_label == "Reference to video":
            inputs[prefix + "reference_audios.audio0"] = ["3", 3]
    if last:
        inputs[prefix + "return_last_frame"] = True
    workflow = {
        "1": {"class_type": "CuddlerVideo", "inputs": inputs},
        "2": {"class_type": "SaveImage" if last else "SaveVideo", "inputs":
              {"images": ["1", 1], "filename_prefix": "cuddler-host-test/last-frame"} if last else
              {"video": ["1", 0], "filename_prefix": "cuddler-host-test/advanced", "format": "auto", "codec": "auto"}},
        "3": {"class_type": "CuddlerHostMediaFixture", "inputs": {}},
    }
    saved = run_workflow(base, workflow)["images"][0]
    if not last:
        return decoded_video(base, saved)
    from PIL import Image
    status, media = call(base, "GET", "/view?" + urllib.parse.urlencode(saved))
    with Image.open(io.BytesIO(media)) as image:
        return status == 200 and image.size == (16, 16)


def run_long_dialogue(base):
    info = json.loads(call(base, "GET", "/object_info/CuddlerSpeech")[1])["CuddlerSpeech"]
    option = next(item for item in info["input"]["required"]["model"][1]["options"]
                  if "dialogue_script" in item["inputs"].get("required", {}))
    inputs = {**dynamic_defaults(option), "seed": 3301, "max_usd": 2.0,
              "model.text": "", "model.second_speaker": False,
              "model.dialogue_script": "\n".join(f"{i % 10 + 1}: Hello {i}." for i in range(50))}
    run_workflow(base, {"1": {"class_type": "CuddlerSpeech", "inputs": inputs},
        "2": {"class_type": "SaveAudio", "inputs": {"audio": ["1", 0], "filename_prefix": "cuddler-host-test/dialogue"}}})


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--user-dir", required=True)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    page = {"origin": base, "site": "same-origin"}
    results = []

    def check(name, ok, detail=b""):
        results.append(ok)
        print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" -- {detail[:300]!r}"))

    status, body = call(base, "GET", "/extensions")
    check("the web extension is served",
          status == 200 and any(path.endswith("/cuddler/cuddler.js") for path in json.loads(body)), body)

    status, body = call(base, "GET", "/api/cuddler/status", **page)
    info = json.loads(body) if status == 200 else {}
    check("status answers, no key yet", info.get("key", {}).get("source") == "none", body)
    check("the model list is loaded", info.get("catalog", {}).get("models", 0) > 0, body)

    status, body = call(base, "POST", "/api/cuddler/key", {"key": KEY}, **page)
    check("a key saves and only its last four characters come back",
          status == 200 and KEY.encode() not in body and json.loads(body)["key"]["last4"] == "AAAA", body)
    key_file = Path(args.user_dir) / "__cuddler" / "credentials.json"
    check("the key file is in ComfyUI's system-user directory", key_file.is_file())
    if os.name == "posix":
        check("the key file is private (0600)", stat.S_IMODE(key_file.stat().st_mode) == 0o600)
    for path in ("/api/userdata/credentials.json", "/api/userdata/..%2F__cuddler%2Fcredentials.json",
                 "/api/userdata/%2E%2E%2F__cuddler%2Fcredentials.json", "/api/userdata?dir=..%2F__cuddler"):
        status, body = call(base, "GET", path)
        check(f"the key is not readable through {path}", status != 200 and KEY.encode() not in body,
              str(status).encode() + b" " + body)

    status, body = call(base, "GET", "/api/cuddler/status?balance=1", **page)
    check("the balance comes through",
          status == 200 and json.loads(body).get("balance", {}).get("available_display") == "$12.34", body)
    status, body = call(base, "POST", "/api/cuddler/estimate",
                        {"kind": "video", "body": {"model": "seedance-2.5", "duration": 4, "resolution": "720p"}},
                        **page)
    check("a price estimate comes through", status == 200 and json.loads(body).get("display") == "$0.66", body)

    status, body = call(base, "POST", "/api/cuddler/key", {"key": KEY}, origin="http://evil.example",
                        site="cross-site")
    check("another site cannot set the key", status == 403, str(status).encode())

    status, body = call(base, "GET", "/object_info/CuddlerVideo")
    node = json.loads(body).get("CuddlerVideo", {}) if status == 200 else {}
    check("CuddlerVideo is registered with native VIDEO and IMAGE outputs", node.get("output") == ["VIDEO", "IMAGE"], body)
    try:
        dynamic = node["input"]["required"]["model"][1]["options"]
        option = next(option for option in dynamic if
                      "(seedance-2.5) / Text to video / default" in option["key"])
        check("the actual host exposes the text-video DynamicCombo", True)
        nested = option["inputs"].get("required", {})
        before = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
        for _ in range(2):
            saved = run_video(base, option["key"], audio="generate_audio" in nested)
            check("CuddlerVideo → SaveVideo produces a downloadable decoded MP4", decoded_video(base, saved))
        after = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
        check("running the same inputs twice creates exactly one fake paid task",
              after["creates"] - before["creates"] == 1, json.dumps(after).encode())
        check("the second run executes recovery and downloads the retained result",
              after["downloads"] - before["downloads"] == 2, json.dumps(after).encode())
        check("all workflow requests used the offline Platform fixture", after["unexpected"] == 0,
              json.dumps(after).encode())
    except Exception as error:
        check("the real VIDEO workflow completes", False, str(error).encode())

    try:
        silent = next(option for option in dynamic if
                      "(minimax-hailuo-2.3) / Text to video / standard" in option["key"])
        metadata = node["input"]["required"]["model"][1]["cuddler_choices"][silent["key"]]
        check("silent-model price metadata explicitly disables audio", metadata.get("generate_audio") is False)
        saved = run_video(base, silent["key"], parameters={"model.duration": 6, "model.resolution": "768p"})
        check("a silent model runs without an audio toggle", decoded_video(base, saved))
    except Exception as error:
        check("the silent VIDEO workflow completes", False, str(error).encode())

    try:
        option = next(option for option in dynamic if
                      "(seedance-2.5) / First frame to video / default" in option["key"])
        nested = option["inputs"].get("required", {})
        first_frame = upload_first_frame(base)
        before = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
        for _ in range(2):
            saved = run_video(base, option["key"], audio="generate_audio" in nested, first_frame=first_frame)
            check("Load Image → CuddlerVideo → SaveVideo produces a decoded MP4", decoded_video(base, saved))
        after = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
        upload_counts = ("upload_requests", "upload_creates", "upload_puts", "upload_completes")
        check("the same first frame creates, sends and completes exactly one upload",
              all(after[name] - before[name] == 1 for name in upload_counts), json.dumps(after).encode())
        check("the same first-frame operation submits exactly one task and really recovers on the second run",
              all(after[name] - before[name] == 1 for name in ("creates", "create_requests", "first_frame_creates"))
              and after["video_reads"] - before["video_reads"] == 1
              and after["downloads"] - before["downloads"] == 2, json.dumps(after).encode())
        saved = run_video(base, option["key"], audio="generate_audio" in nested, first_frame=first_frame, seed=1730)
        check("a new seed still produces a decoded MP4", decoded_video(base, saved))
        reused = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
        check("a new seed reads the ready upload and creates one new task without uploading again",
              all(reused[name] == after[name] for name in upload_counts)
              and all(reused[name] - after[name] == 1 for name in ("upload_reads", "creates", "create_requests", "first_frame_creates", "downloads")),
              json.dumps(reused).encode())
        check("first-frame transfers used only the offline fixture and no media credential headers",
              reused["unexpected"] == 0, json.dumps(reused).encode())
    except Exception as error:
        check("the real first-frame VIDEO workflow completes", False, str(error).encode())

    for model_id, mode in (("seedance-2.5", "Reference to video"), ("seedance-2.5", "Edit video"),
            ("seedance-2.5", "Extend video"), ("kling-3.0", "Motion control"), ("kling-o1", "Image reference"),
            ("kling-o1", "Video reference"), ("kling-o1", "First and last frame")):
        try:
            check("native inputs → " + mode + " → SaveVideo", run_advanced_video(base, dynamic, model_id, mode))
        except Exception as error:
            check(mode + " workflow completes", False, str(error).encode())
    try:
        check("requested last frame → native IMAGE → SaveImage",
              run_advanced_video(base, dynamic, "seedance-2.5", "First frame to video", last=True))
        check("untrimmed video container metadata is stripped before upload",
              run_advanced_video(base, dynamic, "seedance-2.5", "Reference to video", untrimmed=True))
        run_long_dialogue(base)
        stats = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
        check("50-turn dialogue survives native host input packing", stats["long_dialogues"] == 1)
        check("image, trimmed video and audio uploads strip private metadata", stats["native_uploads"] == ["audio", "image", "video"])
        check("video quotes use the encoded four-second clip", stats["precise_video_quotes"] >= 5)
        check("all nine video modes reach the native host", len(stats["video_modes"]) == 9)
    except Exception as error:
        check("advanced media verification completes", False, str(error).encode())

    for node_id, native in (("CuddlerImage", "IMAGE"), ("CuddlerSpeech", "AUDIO"), ("CuddlerMusic", "AUDIO")):
        try:
            node = json.loads(call(base, "GET", "/object_info/" + node_id)[1]).get(node_id, {})
            check(node_id + " exposes native " + native, node.get("output") == [native])
            before = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
            for _ in range(2):
                check(node_id + " saves playable media", run_creative(base, node_id))
            after = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
            check(node_id + " recovers one task for two executions",
                  after["creates"] - before["creates"] == 1 and after["downloads"] - before["downloads"] == 2,
                  json.dumps(after).encode())
        except Exception as error:
            check(node_id + " native workflow completes", False, str(error).encode())
    try:
        check("LoadImage → CuddlerImage references → SaveImage", run_creative(base, "CuddlerImage", reference=upload_first_frame(base)))
        after = json.loads(call(base, "GET", "/cuddler-host-test/stats")[1])
        check("all four nodes stayed on the offline fixture", after["unexpected"] == 0, json.dumps(after).encode())
    except Exception as error:
        check("the image reference workflow completes", False, str(error).encode())

    status, body = call(base, "DELETE", "/api/cuddler/key", **page)
    check("the key can be removed",
          status == 200 and json.loads(body)["key"]["source"] == "none" and not key_file.exists(), body)

    print(f"{sum(results)}/{len(results)} host checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
