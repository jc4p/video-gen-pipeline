#!/usr/bin/env python3
"""Draft a story file (the shot list every later stage reads) with Gemini.

    # pitch a few directions first
    uv run scripts/02_write_story.py --topic "how volcanoes work" --ideas 3

    # then write the story
    uv run scripts/02_write_story.py --topic "how volcanoes work, hosted by Mia" \
        --style "1990s Saturday-morning 2D cel animation" --seconds 45 \
        --cast characters/mia --out stories/volcano.story.json

The result is a draft: read it, edit lines and prompts by hand, then run the next stages.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib import TEXT_MODEL, gemini_json, load_env, load_story

IDEAS_PROMPT = """You are pitching short-form video concepts.

TOPIC: {topic}

Give {n} genuinely different concepts, not variations of one idea. Return ONLY JSON:
{{"ideas": [{{"title": "...", "logline": "one sentence", "tone": "...",
"suggested_style": "a visual art direction", "beats": ["4-7 story beats in order"],
"ending": "how it lands"}}]}}
"""

NARRATED_RULES = """NARRATION (mode: narrated):
- Every shot has a `narration` line; the lines read in order are the whole voiceover. Aim for
  {lo}-{hi} words in total.
- Each line must make sense on one listen to someone who knows nothing about the topic. Say what
  a thing is before (or as) you name it. Plain cause and effect in full sentences.
- No slogans, no rhetorical-question pivots, no in-jokes, no "here's the twist" asides.
- Open on a concrete hook in the first 3 seconds and end on a clear closing line.
- Set each `duration` to roughly words / 2.8 seconds; real timing is taken from the audio later.
- `overlay_text` is a 1-4 word ALL-CAPS label for the shot."""

ACTION_RULES = """NARRATION (mode: action):
- No narration: leave `narration` out of every shot. The story is told by the pictures alone,
  with music and sound effects.
- Give shots with a clear sound (roar, splash, impact) an `sfx` object:
  {{"prompt": "what it sounds like", "seconds": 1.5, "at": 0.4}} where `at` is the offset into the shot.
- Add a top-level `music` string describing the background track.
- Durations of 2-4 seconds per shot, summing to about {seconds} seconds.
- If a shot continues the previous shot's action without a cut, add "continues": true; its clip
  then starts from the previous clip's last frame instead of a new still."""

STORY_PROMPT = """You are a short-form video director writing a shot list as JSON.

TOPIC: {topic}
TARGET LENGTH: about {seconds} seconds, aspect ratio {aspect}.

ART DIRECTION (goes into `style` and applies to every shot):
{style}

RECURRING CHARACTERS (reference images keep them identical across shots). When one appears in a
shot, list its key in that shot's `characters` and refer to it by name in the prompts:
{cast}

{rules}

SHOTS:
- `frame_prompt` describes one still image: the first frame of the shot. Concrete and visual:
  who, where, pose, camera angle. Do not write text that should appear in the image.
- `video_prompt` is one sentence of motion that starts from that still, ending with
  "no speech, no subtitles."
- Vary compositions: no two neighbouring shots with the same framing.
- Use only original characters and settings: no existing franchises, brands or logos.

Return ONLY JSON in exactly this shape:
{schema}
"""

SCHEMA = {
    "title": "string",
    "style": "string",
    "music": "string, optional",
    "shots": [{
        "slug": "kebab-case", "duration": 3.0, "overlay_text": "SHORT LABEL",
        "narration": "the line spoken during this shot (narrated mode only)",
        "frame_prompt": "...", "video_prompt": "...", "characters": ["key"],
    }],
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--topic", required=True)
    ap.add_argument("--ideas", type=int, help="print N concept pitches and stop")
    ap.add_argument("--style", help="art direction for the whole video")
    ap.add_argument("--seconds", type=int, default=45)
    ap.add_argument("--aspect", default="9:16", choices=["9:16", "16:9", "1:1"])
    ap.add_argument("--mode", default="narrated", choices=["narrated", "action"])
    ap.add_argument("--cast", nargs="*", type=Path, default=[], help="character folders from 01_make_character.py")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--model", default=TEXT_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="print the prompt, call nothing")
    args = ap.parse_args()

    if args.ideas:
        prompt = IDEAS_PROMPT.format(topic=args.topic, n=args.ideas)
    else:
        assert args.style and args.out, "--style and --out are required to write a story"
        characters = {}
        for folder in args.cast:
            meta = json.loads((folder / "character.json").read_text())
            characters[folder.name] = {"description": meta["description"], "ref_images": meta["ref_images"]}
        cast = "\n".join(f"- {k}: {c['description']}" for k, c in characters.items()) or "(none)"
        rules = (NARRATED_RULES.format(lo=int(args.seconds * 2.3), hi=int(args.seconds * 2.9))
                 if args.mode == "narrated" else ACTION_RULES.format(seconds=args.seconds))
        prompt = STORY_PROMPT.format(topic=args.topic, seconds=args.seconds, aspect=args.aspect,
                                     style=args.style, cast=cast, rules=rules,
                                     schema=json.dumps(SCHEMA, indent=2))
    if args.dry_run:
        print(prompt)
        return

    load_env()
    result = gemini_json(prompt, model=args.model)
    if args.ideas:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return

    story = {"title": result["title"], "aspect": args.aspect, "style": result["style"]}
    if result.get("music"):
        story["music"] = result["music"]
    if characters:
        story["characters"] = characters
    story["shots"] = [{"id": f"{i:02d}", **shot} for i, shot in enumerate(result["shots"], start=1)]
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(story, indent=2, ensure_ascii=False) + "\n")
    load_story(args.out)  # fail now if the draft is not a valid story
    total = sum(float(s["duration"]) for s in story["shots"])
    print(f"wrote {args.out}: {len(story['shots'])} shots, ~{total:.0f}s. Read and edit it before generating.")


if __name__ == "__main__":
    main()
