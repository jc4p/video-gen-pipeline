#!/usr/bin/env python3
"""Generate the start frame of each shot, grounded on the character reference images.

    uv run scripts/03_frames.py --story examples/lighthouse.story.json
    uv run scripts/03_frames.py --story examples/lighthouse.story.json --redo 03,07

Frames land in runs/<story>/frames/. Existing frames are kept; --redo regenerates the listed
shots. Look at every frame before paying for video: fixing a still is far cheaper than a clip.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from lib import IMAGE_MODEL, gemini_image, load_env, load_story, parse_ids, run_dir, shot_name

GROUNDING = (
    "Recreate these exact characters in the scene: same face, hair, build, outfit and colors, "
    "instantly recognizable. Draw characters and environment in one unified rendering style, "
    "exactly as the style describes. Each character appears exactly once. "
)


def character_refs(story: dict[str, Any], key: str, refs_dir: Path) -> list[Path]:
    spec = story["characters"][key]
    if "ref_images" in spec:
        return [Path(p) for p in spec["ref_images"]]
    return [refs_dir / f"{key}.png"]  # generated from `ref_prompt`


def frame_prompt(story: dict[str, Any], shot: dict[str, Any], refs_dir: Path) -> tuple[str, list[Path]]:
    style = shot.get("style") or story["style"]
    lines, refs = [], []
    for key in shot.get("characters", []):
        images = character_refs(story, key, refs_dir)
        first, last = len(refs) + 1, len(refs) + len(images)
        span = f"image {first}" if first == last else f"images {first}-{last}"
        lines.append(f"reference {span} show {key} ({story['characters'][key]['description']})")
        refs.extend(images)
    grounding = f"Character grounding: {'; '.join(lines)}. {GROUNDING}" if lines else ""
    return f"{style}. {grounding}{shot['frame_prompt']}", refs


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--story", required=True, type=Path)
    ap.add_argument("--run", type=Path, help="run folder (default runs/<story name>)")
    ap.add_argument("--redo", default="", help="comma-separated shot ids to regenerate")
    ap.add_argument("--model", default=IMAGE_MODEL)
    ap.add_argument("--concurrency", type=int, default=4)
    ap.add_argument("--dry-run", action="store_true", help="print prompts, call nothing")
    args = ap.parse_args()

    story = load_story(args.story)
    out = run_dir(args.story, args.run)
    refs_dir, frames_dir = out / "refs", out / "frames"
    redo = parse_ids(args.redo)

    # Characters described only by a prompt get one reference sheet, generated once.
    ref_jobs = [
        (f"{story['style']}. One single character, full body, front view, neutral standing pose, "
         f"plain white background, nothing else in the image. {spec['ref_prompt']}",
         refs_dir / f"{key}.png")
        for key, spec in story.get("characters", {}).items()
        if "ref_images" not in spec and not (refs_dir / f"{key}.png").exists()
    ]
    # Shots that continue the previous clip start from its last frame, so they need no still.
    shots = [s for s in story["shots"] if not s.get("continues")]
    todo = [s for s in shots if s["id"] in redo or not (frames_dir / f"{shot_name(s)}.png").exists()]

    if args.dry_run:
        for prompt, path in ref_jobs:
            print(f"{path}\n  {prompt}\n")
        for shot in todo:
            prompt, refs = frame_prompt(story, shot, refs_dir)
            print(f"{frames_dir / shot_name(shot)}.png\n  refs: {[str(r) for r in refs]}\n  {prompt}\n")
        return

    load_env()
    for prompt, path in ref_jobs:
        gemini_image(prompt, path, model=args.model, aspect="3:4")
        print(f"wrote {path}")

    def make(shot: dict[str, Any]) -> Path:
        prompt, refs = frame_prompt(story, shot, refs_dir)
        return gemini_image(prompt, frames_dir / f"{shot_name(shot)}.png", refs=refs,
                            model=args.model, aspect=story["aspect"])

    failed = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as pool:
        futures = {shot["id"]: pool.submit(make, shot) for shot in todo}
    for shot_id, future in futures.items():
        if future.exception():
            failed.append(shot_id)
            print(f"shot {shot_id} FAILED: {future.exception()!r}")
        else:
            print(f"wrote {future.result()}")
    print(f"{len(todo) - len(failed)} generated, {len(shots) - len(todo)} kept, {len(failed)} failed -> {frames_dir}")
    if failed:
        raise SystemExit(f"re-run to retry shots {','.join(failed)}")


if __name__ == "__main__":
    main()
