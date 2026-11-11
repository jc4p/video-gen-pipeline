# video-gen-pipeline

A shot-by-shot AI video pipeline that keeps characters consistent. You describe a short video as a list of shots; the pipeline draws a start frame for each shot, animates it, adds narration or music and sound effects, and cuts everything together with captions.

Characters stay recognizable because every frame they appear in is generated with their reference images attached. A character can come from a text description, or from photos of a real subject (a person, a pet, a toy, a mascot) turned into a stylized reference.

```
photos ──01──▶ character refs ─┐
topic  ──02──▶ story.json ─────┼─03─▶ frames ─04─▶ clips ─┐
                               └─05─▶ narration / music / sfx ─┴─06─▶ final.mp4
```

## Iteration is part of the pipeline

This is not a one-command generator. No stage gets everything right on the first pass, and the pipeline is built around looking at each stage's output and redoing the parts that are off before moving on. Plan for several rounds:

| Review this | Typical problems | Fix |
|---|---|---|
| Character references | Likeness is off, unwanted logo or outfit | `01 --refine-face`, `01 --edit "..."` |
| Story draft | Flat hook, unclear lines, repeated framings | Edit the JSON by hand, or re-run `02` |
| Start frames | Character drifts, duplicates, wrong scale or composition | Edit the shot's `frame_prompt`, then `03 --redo 04,09` |
| Clips | Stiff or wrong motion, character morphs mid-clip | Edit `video_prompt`, then `04 --redo 04`; try another `--model` |
| Narration | A line is rushed, slow or mispronounced | `05 --redo 06`, `--wps`, respell the word |
| Final cut | Captions cover a face, music too busy | `06 --voiceover-y`, `05 --redo music` |

Each stage keeps what already exists and regenerates only what you name with `--redo`, so a round of fixes costs a few shots, not the whole video. Go in order: fix stills before paying for clips, and settle the narration before the final cut, because each stage builds on the one before.

## Cost

In practice, an average 40-60 second video ends up costing around $6 across all providers, including the redo rounds. Most of that is the video clips, so the cheapest way to keep it down is to get the stills right first.

| Stage | Script | Service |
|---|---|---|
| Character references from photos | `01_make_character.py` | Gemini image |
| Draft a story file | `02_write_story.py` | Gemini |
| Start frame per shot | `03_frames.py` | Gemini image |
| Animate each frame | `04_videos.py` | fal (H3 Max by default; Seedance, Kling, Veo, Wan) |
| Narration, music, sound effects | `05_audio.py` | ElevenLabs |
| Cut, captions, mix | `06_assemble.py` | local ffmpeg |

## Setup

You need [uv](https://docs.astral.sh/uv/), plus `ffmpeg` and `ffprobe` on your `PATH`.

```bash
uv sync
cp .env.example .env   # then fill in the keys
```

Every stage that calls a paid API has a `--dry-run` flag that prints what it would send.

## Quick start

Two example stories are included. Both use characters defined by a text prompt, so they run without any photos.

```bash
S=examples/lighthouse.story.json
uv run scripts/03_frames.py --story $S
uv run scripts/04_videos.py --story $S
uv run scripts/05_audio.py  --story $S --voice-id <an ElevenLabs voice id>
uv run scripts/06_assemble.py --story $S
```

The result is `runs/lighthouse/final/lighthouse.mp4`, with a smaller `lighthouse.small.mp4` for sharing. Look at `runs/lighthouse/frames/` after the first command and redo any frame you don't like before running the second.

- `examples/lighthouse.story.json` is a narrated explainer (vertical).
- `examples/kite_chase.story.json` is an action piece with music and sound effects, no narration (landscape). It needs no `--voice-id`.

## The story file

One JSON file drives every stage.

```json
{
  "title": "How a Lighthouse Works",
  "aspect": "9:16",
  "style": "Polished 2D cel-animation cartoon, bold ink outlines, ...",
  "music": "Gentle, curious instrumental, no vocals",
  "characters": {
    "pip": {
      "ref_prompt": "Pip, a small round friendly robot with a teal body ...",
      "description": "Pip, a small round teal robot with one big amber eye-lens"
    }
  },
  "shots": [
    {
      "slug": "the-tower",
      "duration": 3.5,
      "overlay_text": "THE LIGHTHOUSE",
      "narration": "So people built a tall tower with a bright lamp at the top: a lighthouse.",
      "frame_prompt": "A tall red-and-white striped lighthouse on a rocky cliff at dusk ...",
      "video_prompt": "Pip points up at the top of the tower and the lamp flickers on, no speech, no subtitles.",
      "characters": ["pip"]
    }
  ]
}
```

Top level:

| Field | Meaning |
|---|---|
| `style` | Art direction, prepended to every frame prompt. Changing it restyles the whole video. |
| `aspect` | `9:16` (default), `16:9` or `1:1`. |
| `music` | Optional prompt for a background track. |
| `characters` | Each has a `description` and either `ref_images` (paths to reference images) or `ref_prompt` (generated once into `runs/<story>/refs/`). |

Per shot:

| Field | Meaning |
|---|---|
| `slug`, `duration` | Name and length in seconds. For narrated stories the real length comes from the audio. |
| `frame_prompt` | The still image the shot starts from. |
| `video_prompt` | The motion that starts from that still. |
| `characters` | Keys of the characters in the shot. Their reference images are attached when the frame is generated. |
| `narration` | The line spoken during this shot. Give every shot one, or none. |
| `overlay_text` | Optional short label burned at the top of the shot. |
| `sfx` | Optional `{"prompt", "seconds", "at"}`; `at` is the offset into the shot. |
| `continues` | `true` makes the shot one continuous action with the previous shot: its clip starts from the previous clip's last frame. |
| `touchup` | With `continues`: an edit applied to that last frame first, e.g. "a stone wall appears in front of her". |
| `style` | Optional per-shot override of the global style. |
| `generation_seconds` | Optional length of the generated clip for this shot (default 4). |

## Characters from photos

```bash
uv run scripts/01_make_character.py --name mia \
  --photos ~/photos/mia1.jpg ~/photos/mia2.jpg \
  --style "high-end stylized 3D cartoon" \
  --description "Mia, a woman with short red hair in a green jacket"
```

This writes `characters/mia/front.png`, `side.png` and `character.json`. Look at them. If the likeness is off, run a fix-up pass; each one writes new files and leaves the earlier ones in place:

```bash
# pull the face closer to the real photos
uv run scripts/01_make_character.py --name mia --tag v2 --refine-face --photos ~/photos/mia1.jpg

# any single edit: outfit, expression, removing a logo
uv run scripts/01_make_character.py --name mia --tag plain \
  --edit "Replace the printed shirt with a plain solid green t-shirt, no logo or text"
```

Add `--cutout` to remove the background with Pixian. Use the character by passing `--cast characters/mia` to `02_write_story.py`, or by listing the images under `ref_images` in a story file.

Things that help:

- Several photos from different angles, including one full-body shot.
- Front and side references together hold a likeness better than one image.
- Put distinguishing features in `description` ("always bare-headed", "plain blue t-shirt"). The description is repeated in every grounded frame.

### Privacy

`characters/` is gitignored, so source photos and the references made from them stay on your machine. The photos you pass in are uploaded to Google (Gemini) and, with `--cutout`, to Pixian; generated frames are uploaded to fal. Only use photos of people who have agreed to it, and check each provider's data terms before using photos of anyone.

## Writing a story

```bash
# pitch a few directions
uv run scripts/02_write_story.py --topic "how tides work" --ideas 3

# write the draft
uv run scripts/02_write_story.py --topic "how tides work, hosted by Mia" \
  --style "1990s Saturday-morning 2D cel animation" --seconds 45 \
  --cast characters/mia --out stories/tides.story.json
```

Use `--mode action` for a piece with no narration. The output is a draft: read it and edit lines and prompts by hand. Narration reads best when each line makes sense on its own to someone new to the topic.

## Redoing shots

Every stage writes into fixed folders under `runs/<story>/` and skips what already exists. Use `--redo` to regenerate specific shots after editing their prompts:

```bash
uv run scripts/03_frames.py --story $S --redo 03,07     # new stills for two shots
uv run scripts/04_videos.py --story $S --redo 03,07     # then new clips for them
uv run scripts/05_audio.py  --story $S --voice-id <id> --redo 05          # re-roll one narration line
uv run scripts/05_audio.py  --story $S --voice-id <id> --redo music
```

A still costs a fraction of a clip, and most problems (wrong likeness, duplicate characters, bad composition) are already visible in the still.

When a prompt change does not fix a frame, say what you want plainly instead of what you do not want: "arms relaxed at his sides" works better than "NOT a stiff pose".

`04_videos.py` saves each job's request id before waiting on it. If the script is interrupted, running it again picks the same jobs back up instead of submitting new ones.

### Narration timing

Each narration line is synthesized separately, then joined. `05_audio.py` writes `audio/timing.json` with how long each shot must last to cover its line, and `06_assemble.py` retimes each clip to that length, so picture and voice stay in sync. Use `--wps 3.4` to speed every line to an even words-per-second pace.

If a line is much longer than its clip, assembly slows the clip down and prints a warning. Split the shot or set `generation_seconds`.

### Captions

`06_assemble.py` burns two kinds of caption: word-timed voiceover captions near the bottom and each shot's `overlay_text` near the top.

```bash
uv run scripts/06_assemble.py --story $S --captions voiceover --voiceover-y 0.3
```

`--captions` takes `both`, `voiceover`, `overlay` or `none`. `--voiceover-y` and `--overlay-y` move them (as a fraction of the frame height) when they cover a face. Assembly makes no API calls, so caption changes are free.

## Video models

`--model` on `04_videos.py` selects the image-to-video model: `h3-max` (default), `seedance`, `kling`, `veo-fast` or `wan3`. Endpoints and per-model settings live in `VIDEO_MODELS` in `scripts/lib.py`.

The best image-to-video model changes every few months. To keep up, we recommend looking at the model charts on [fal.ai](https://fal.ai) and switching the default when something better tops them. Adding a model is one entry in `VIDEO_MODELS`: its endpoint, the field its start image goes in, and how it spells duration, aspect ratio and resolution.

Video models sometimes drift a character's design partway through a clip. Shorter clips, simpler motion prompts and a different model are the usual fixes.

## Content

The story writer is told to use original characters and settings. Keep it that way for anything you publish: generating recognizable franchise characters, brands or real people who have not agreed to it is on you, not the tool.

## License

MIT
