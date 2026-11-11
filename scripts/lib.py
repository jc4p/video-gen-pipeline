"""Shared helpers: story files, run dirs, and thin clients for Gemini, fal and ElevenLabs."""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import requests

IMAGE_MODEL = "gemini-3.1-flash-image"
TEXT_MODEL = "gemini-4-argon"

SIZES = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}


# ---------- env, story, run dir ----------

def load_env(path: Path = Path(".env")) -> None:
    if not path.exists():
        return
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def need_key(*names: str) -> str:
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    raise RuntimeError(f"{' or '.join(names)} is missing (set it in .env)")


def load_story(path: Path) -> dict[str, Any]:
    story = json.loads(path.read_text())
    assert story.get("style"), "story needs a global `style` string"
    assert story.get("shots"), "story needs `shots`"
    aspect = story.setdefault("aspect", "9:16")
    assert aspect in SIZES, f"aspect must be one of {sorted(SIZES)}"
    for i, shot in enumerate(story["shots"], start=1):
        shot.setdefault("id", f"{i:02d}")
        for field in ("slug", "duration", "frame_prompt", "video_prompt"):
            assert field in shot, f"shot {shot['id']} is missing `{field}`"
        for key in shot.get("characters", []):
            assert key in story.get("characters", {}), f"shot {shot['id']} uses unknown character {key!r}"
    assert not story["shots"][0].get("continues"), "the first shot cannot have `continues`"
    return story


def run_dir(story_path: Path, override: Path | None) -> Path:
    """runs/<story name>/ — every stage reads and writes fixed subfolders in here."""
    name = story_path.name.removesuffix(".json").removesuffix(".story")
    return override or Path("runs") / name


def parse_ids(spec: str) -> set[str]:
    return {item.strip().zfill(2) for item in spec.split(",") if item.strip()}


def shot_name(shot: dict[str, Any]) -> str:
    return f"shot_{shot['id']}_{shot['slug']}"


def is_narrated(story: dict[str, Any]) -> bool:
    """A story is narrated when every shot has a `narration` line, silent-action when none do."""
    flags = [bool(shot.get("narration")) for shot in story["shots"]]
    assert all(flags) or not any(flags), "give every shot a `narration` line, or none of them"
    return all(flags)


# ---------- ffmpeg ----------

def run(cmd: list[str]) -> str:
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{result.stderr[-2000:]}")
    return result.stderr


def media_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout)


# ---------- Gemini ----------

def _gemini_client():
    from google import genai

    return genai.Client(api_key=need_key("GEMINI_API_KEY", "GOOGLE_API_KEY"))


def gemini_image(prompt: str, out_path: Path, refs: list[Path] | None = None,
                 model: str = IMAGE_MODEL, aspect: str = "9:16", size: str = "1K") -> Path:
    """Generate or edit an image. Reference images go first, the instruction last."""
    from google.genai import types
    from PIL import Image

    contents: list[Any] = [Image.open(p) for p in refs or []] + [prompt]
    client = _gemini_client()  # keep a reference: the client closes its connection when collected
    response = client.models.generate_content(
        model=model,
        contents=contents,
        config=types.GenerateContentConfig(
            response_modalities=["TEXT", "IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect, image_size=size),
        ),
    )
    texts = []
    for part in response.parts or []:
        if part.text is not None:
            texts.append(part.text)
            continue
        image = part.as_image()
        if image:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            image.save(out_path)
            return out_path
    raise RuntimeError(f"Gemini returned no image for {out_path.name}: {texts!r}")


def gemini_json(prompt: str, model: str = TEXT_MODEL) -> Any:
    from google.genai import types

    client = _gemini_client()
    response = client.models.generate_content(
        model=model,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json", temperature=1.0),
    )
    return json.loads(response.text)


# ---------- Pixian (background removal) ----------

def remove_background(in_path: Path, out_path: Path) -> Path:
    api_id, api_secret = need_key("PIXIAN_API_AUTH").split(":", 1)
    with in_path.open("rb") as fh:
        response = requests.post("https://api.pixian.ai/api/v2/remove-background",
                                 auth=(api_id, api_secret), files={"image": fh}, timeout=180)
    response.raise_for_status()
    out_path.write_bytes(response.content)
    return out_path


# ---------- fal (image-to-video, queue API) ----------

FAL_QUEUE = "https://queue.fal.run"

# endpoint, the field the start image goes in, how the clip length is spelled, and fixed settings.
# `aspect` / `resolution` name the payload field to fill, when the model takes one.
VIDEO_MODELS: dict[str, dict[str, Any]] = {
    "seedance": {
        "endpoint": "bytedance/seedance-2.0/image-to-video", "image_field": "image_url",
        "duration": lambda s: str(round(s)), "aspect": "aspect_ratio", "resolution": "480p",
        "payload": {"generate_audio": False},
    },
    "h3-max": {  # follows the start image's aspect ratio; always returns audio (dropped at assembly)
        "endpoint": "minimax/h3-max/image-to-video", "image_field": "image_url",
        "duration": lambda s: s, "aspect": None, "resolution": "480P",
        "payload": {"prompt_expansion_mode": "disabled"},
    },
    "kling": {
        "endpoint": "fal-ai/kling-video/v3/pro/image-to-video", "image_field": "start_image_url",
        "duration": lambda s: str(round(s)), "aspect": None, "resolution": None,
        "payload": {"generate_audio": False, "cfg_scale": 0.5},
    },
    "veo-fast": {
        "endpoint": "fal-ai/veo3.1/fast/image-to-video", "image_field": "image_url",
        "duration": lambda s: f"{round(s)}s", "aspect": "aspect_ratio", "resolution": "720p",
        "payload": {"generate_audio": False},
    },
    "wan3": {
        "endpoint": "alibaba/wan-3.0/image-to-video", "image_field": "start_image_url",
        "duration": lambda s: round(s), "aspect": "aspect_ratio", "resolution": "480p",
        "payload": {"audio": False, "enable_prompt_expansion": False},
    },
}


def video_payload(model: str, prompt: str, image_uri: str, aspect: str, seconds: float,
                  resolution: str | None = None) -> tuple[str, dict[str, Any]]:
    cfg = VIDEO_MODELS[model]
    payload = dict(cfg["payload"])
    payload["prompt"] = prompt
    payload[cfg["image_field"]] = image_uri
    payload["duration"] = cfg["duration"](seconds)
    if cfg["aspect"]:
        payload[cfg["aspect"]] = aspect
    if cfg["resolution"]:
        payload["resolution"] = resolution or cfg["resolution"]
    return cfg["endpoint"], payload


def data_uri(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def fal_submit(endpoint: str, payload: dict[str, Any]) -> dict[str, str]:
    """Queue a job and return its ids/urls straight away, so the caller can save them before polling."""
    response = requests.post(
        f"{FAL_QUEUE}/{endpoint}",
        headers={"Authorization": f"Key {need_key('FAL_KEY', 'FAL_API_KEY')}"},
        json=payload, timeout=120,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"fal submit {endpoint} failed {response.status_code}: {response.text[:800]}")
    data = response.json()
    return {"endpoint": endpoint, "request_id": data["request_id"],
            "status_url": data["status_url"], "response_url": data["response_url"]}


def fal_wait(job: dict[str, str], out_path: Path, timeout: float = 1800.0) -> Path:
    """Poll a queued job to completion and download its video."""
    headers = {"Authorization": f"Key {need_key('FAL_KEY', 'FAL_API_KEY')}"}
    deadline = time.monotonic() + timeout
    while True:
        status = requests.get(job["status_url"], headers=headers, timeout=30).json().get("status")
        if status == "COMPLETED":
            break
        if status in ("FAILED", "ERROR"):
            raise RuntimeError(f"fal job {job['request_id']} ended {status}; regenerate the shot with --redo")
        if time.monotonic() > deadline:
            raise TimeoutError(f"fal job {job['request_id']} still {status}; re-run to resume polling")
        time.sleep(8)
    response = requests.get(job["response_url"], headers=headers, timeout=60)
    if response.status_code >= 400:
        raise RuntimeError(f"fal job {job['request_id']} failed: {response.text[:800]}")
    result = response.json()
    video = requests.get(result["video"]["url"], timeout=300)
    video.raise_for_status()
    out_path.write_bytes(video.content)
    return out_path


# ---------- ElevenLabs ----------

ELEVEN = "https://api.elevenlabs.io/v1"


def _eleven(path: str, body: dict[str, Any], timeout: int = 300) -> requests.Response:
    response = requests.post(
        f"{ELEVEN}/{path}",
        headers={"xi-api-key": need_key("ELEVENLABS_API_KEY", "ELEVEN_LABS_API_KEY")},
        json=body, timeout=timeout,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"ElevenLabs {path.split('?')[0]} failed {response.status_code}: {response.text[:600]}")
    return response


def tts_line(text: str, voice_id: str, model: str, speed: float) -> tuple[bytes, dict[str, Any]]:
    """One line of speech -> (mp3 bytes, per-character alignment)."""
    data = _eleven(
        f"text-to-speech/{voice_id}/with-timestamps?output_format=mp3_44100_128",
        {"text": text, "model_id": model,
         "voice_settings": {"stability": 0.35, "similarity_boost": 0.75, "style": 0.65,
                            "speed": speed, "use_speaker_boost": True}},
    ).json()
    return base64.b64decode(data["audio_base64"]), data["alignment"]


def make_music(prompt: str, seconds: float, out_path: Path) -> Path:
    response = _eleven("music?output_format=mp3_44100_128",
                       {"prompt": prompt, "music_length_ms": int(seconds * 1000)})
    out_path.write_bytes(response.content)
    return out_path


def make_sfx(prompt: str, seconds: float, out_path: Path) -> Path:
    response = _eleven("sound-generation?output_format=mp3_44100_128",
                       {"text": prompt, "duration_seconds": seconds, "prompt_influence": 0.4})
    out_path.write_bytes(response.content)
    return out_path
