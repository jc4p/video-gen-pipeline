#!/usr/bin/env python3
"""Animate each start frame into a short clip with an image-to-video model on fal.

    uv run scripts/04_videos.py --story examples/lighthouse.story.json
    uv run scripts/04_videos.py --story examples/lighthouse.story.json --redo 05 --model seedance

Clips land in runs/<story>/clips/. Each job's request id is saved before polling, so if this
script is interrupted, re-running it resumes the same jobs instead of paying for new ones.

A shot with "continues": true is one continuous action with the shot before it: its clip
starts from the previous clip's last frame. Give it a "touchup" instruction to have the image
model adjust that frame first (add a prop, move the character) while keeping everything else.
"""
from __future__ import annotations

import argparse
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from lib import (IMAGE_MODEL, VIDEO_MODELS, data_uri, fal_submit, fal_wait, gemini_image, load_env,
                 load_story, parse_ids, run, run_dir, shot_name, video_payload)

CONTINUITY = (
    "This image is the last frame of the previous video shot. Produce the very next instant of "
    "the same continuous action: same characters, outfits, world, lighting and art style. "
    "Make only this change: "
)


def chains(shots: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Group shots so each `continues` shot follows the shot it continues."""
    groups: list[list[dict[str, Any]]] = []
    for shot in shots:
        if shot.get("continues"):
            groups[-1].append(shot)
        else:
            groups.append([shot])
    return groups


def last_frame(clip: Path, out_png: Path) -> Path:
    run(["ffmpeg", "-y", "-sseof", "-0.1", "-i", str(clip), "-update", "1", "-q:v", "1", str(out_png)])
    return out_png


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--story", required=True, type=Path)
    ap.add_argument("--run", type=Path, help="run folder (default runs/<story name>)")
    ap.add_argument("--redo", default="", help="comma-separated shot ids to regenerate")
    ap.add_argument("--model", default="h3-max", choices=sorted(VIDEO_MODELS))
    ap.add_argument("--resolution", help="override the model's default resolution")
    ap.add_argument("--seconds", type=float, default=4.0,
                    help="generated clip length; a shot's `generation_seconds` overrides it")
    ap.add_argument("--concurrency", type=int, default=8, help="chains in flight at once")
    ap.add_argument("--dry-run", action="store_true", help="list what would be submitted, call nothing")
    args = ap.parse_args()

    story = load_story(args.story)
    out = run_dir(args.story, args.run)
    frames_dir, clips_dir = out / "frames", out / "clips"
    redo = parse_ids(args.redo)

    def render(shot: dict[str, Any], previous_clip: Path | None) -> Path:
        name = shot_name(shot)
        clip, job_file = clips_dir / f"{name}.mp4", clips_dir / f"{name}.job.json"
        if shot["id"] in redo:
            clip.unlink(missing_ok=True)
            job_file.unlink(missing_ok=True)
        if clip.exists():
            return clip
        if not job_file.exists():
            if shot.get("continues"):
                start = last_frame(previous_clip, clips_dir / f"{name}.start.png")
                if shot.get("touchup"):
                    prompt = f"{shot.get('style') or story['style']}. {CONTINUITY}{shot['touchup']}"
                    start = gemini_image(prompt, clips_dir / f"{name}.touchup.png", refs=[start],
                                         model=IMAGE_MODEL, aspect=story["aspect"])
            else:
                start = frames_dir / f"{name}.png"
                assert start.exists(), f"missing start frame {start}; run 03_frames.py"
            endpoint, payload = video_payload(
                args.model, shot["video_prompt"], data_uri(start), story["aspect"],
                float(shot.get("generation_seconds", args.seconds)), args.resolution)
            job = fal_submit(endpoint, payload)
            job_file.write_text(json.dumps({**job, "model": args.model}, indent=2) + "\n")
            print(f"shot {shot['id']}: submitted {job['request_id']}")
        fal_wait(json.loads(job_file.read_text()), clip)
        print(f"shot {shot['id']}: wrote {clip}")
        return clip

    def render_chain(chain: list[dict[str, Any]]) -> None:
        previous: Path | None = None
        for shot in chain:
            previous = render(shot, previous)

    groups = chains(story["shots"])
    if args.dry_run:
        for chain in groups:
            for shot in chain:
                clip = clips_dir / f"{shot_name(shot)}.mp4"
                state = "keep" if clip.exists() and shot["id"] not in redo else "generate"
                source = "previous clip's last frame" if shot.get("continues") else "start frame"
                print(f"shot {shot['id']}: {state} ({args.model}, from {source})  {shot['video_prompt']}")
        return

    load_env()
    clips_dir.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {chain[0]["id"]: pool.submit(render_chain, chain) for chain in groups}
    failed = [f"{shot_id}: {future.exception()!r}" for shot_id, future in futures.items() if future.exception()]
    for line in failed:
        print(f"chain starting at shot {line}")
    if failed:
        raise SystemExit("some clips failed; re-run to resume (submitted jobs are not resubmitted)")
    print(f"all clips in {clips_dir}")


if __name__ == "__main__":
    main()
