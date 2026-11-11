#!/usr/bin/env python3
"""Make the soundtrack pieces with ElevenLabs: narration, music and sound effects.

    uv run scripts/05_audio.py --story examples/lighthouse.story.json --voice-id <id>
    uv run scripts/05_audio.py --story examples/lighthouse.story.json --voice-id <id> --redo 03 --wps 3.4

Narrated stories (every shot has a `narration` line):
  Each line is synthesized on its own, which paces far more evenly than one long take and lets
  you re-roll a single line with --redo. The lines are joined into audio/narration.mp3, and
  audio/timing.json records how long each shot must stay on screen for its line. Assembly uses
  that timing, so picture and voice cannot drift.

Action stories (no narration): shot durations come from the story file.

Either kind: a top-level `music` prompt makes audio/music.mp3, and a shot's
`sfx: {"prompt", "seconds", "at"}` makes audio/sfx/shot_NN.mp3, placed `at` seconds into the shot.
"""
from __future__ import annotations

import argparse
import json
import tempfile
from pathlib import Path

from lib import (is_narrated, load_env, load_story, make_music, make_sfx, media_duration, parse_ids,
                 run, run_dir, tts_line)

RATE = 44100


def synth_lines(story: dict, lines_dir: Path, redo: set[str], args: argparse.Namespace) -> None:
    """One trimmed mp3 + alignment per shot; cached unless listed in --redo."""
    for shot in story["shots"]:
        mp3, align_path = lines_dir / f"line_{shot['id']}.mp3", lines_dir / f"line_{shot['id']}.align.json"
        if mp3.exists() and align_path.exists() and shot["id"] not in redo:
            continue
        audio, align = tts_line(shot["narration"], args.voice_id, args.voice_model, args.speed)
        assert "".join(align["characters"]) == shot["narration"], (
            f"shot {shot['id']}: the voice model rewrote the text; spell out numbers and symbols")
        starts, ends = align["character_start_times_seconds"], align["character_end_times_seconds"]
        # Cut the dead air the model leaves around the speech.
        trim_start, trim_end = max(0.0, starts[0] - 0.05), ends[-1] + 0.05
        with tempfile.NamedTemporaryFile(suffix=".mp3") as raw:
            raw.write(audio)
            raw.flush()
            run(["ffmpeg", "-y", "-ss", f"{trim_start:.3f}", "-to", f"{trim_end:.3f}", "-i", raw.name,
                 "-ar", str(RATE), "-ac", "1", "-c:a", "libmp3lame", "-q:a", "2", str(mp3)])
        align_path.write_text(json.dumps({
            "characters": align["characters"],
            "starts": [round(t - trim_start, 4) for t in starts],
            "ends": [round(t - trim_start, 4) for t in ends],
        }))
        print(f"line {shot['id']}: {media_duration(mp3):.2f}s  {shot['narration'][:60]}")


def stitch(story: dict, audio_dir: Path, args: argparse.Namespace) -> float:
    """Join the lines into narration.mp3; write the joined alignment and per-shot timing."""
    lines_dir = audio_dir / "lines"
    chars: list[str] = []
    starts: list[float] = []
    ends: list[float] = []
    timing: dict[str, float] = {}
    shots = story["shots"]
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)

        def silence(name: str, seconds: float) -> Path:
            path = tmp_dir / name
            run(["ffmpeg", "-y", "-f", "lavfi", "-t", f"{seconds:.3f}",
                 "-i", f"anullsrc=r={RATE}:cl=mono", str(path)])
            return path

        # Players clip the first instant of audio, so start with a beat of silence.
        pieces = [silence("lead.wav", args.lead_in)]
        gap = silence("gap.wav", args.gap)
        offset = args.lead_in
        shot_start = 0.0
        print(f"\n{'id':>3} {'wps':>4} {'sec':>5}  narration")
        for i, shot in enumerate(shots):
            mp3 = lines_dir / f"line_{shot['id']}.mp3"
            align = json.loads((lines_dir / f"line_{shot['id']}.align.json").read_text())
            words = len(shot["narration"].split())
            natural = media_duration(mp3)
            # Optional pitch-preserving speed-up to an even words-per-second pace.
            tempo = min(2.0, max(0.7, args.wps * natural / words)) if args.wps else 1.0
            wav = tmp_dir / f"line_{shot['id']}.wav"
            run(["ffmpeg", "-y", "-i", str(mp3), "-filter:a", f"atempo={tempo:.5f}",
                 "-ar", str(RATE), "-ac", "1", str(wav)])
            pieces.append(wav)
            if i:
                chars.append(" ")
                starts.append(round(offset - args.gap, 4))
                ends.append(round(offset, 4))
            chars.extend(align["characters"])
            starts.extend(round(offset + t / tempo, 4) for t in align["starts"])
            ends.extend(round(offset + t / tempo, 4) for t in align["ends"])
            seconds = media_duration(wav)
            offset += seconds
            if i < len(shots) - 1:
                pieces.append(gap)
                offset += args.gap
            # The shot holds through its line and the breath after it.
            timing[shot["id"]] = round(offset - shot_start, 3)
            shot_start = offset
            print(f"{shot['id']:>3} {words / seconds:>4.1f} {seconds:>5.2f}  {shot['narration'][:60]}")

        concat = tmp_dir / "concat.txt"
        concat.write_text("".join(f"file '{p}'\n" for p in pieces))
        run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat),
             "-c:a", "libmp3lame", "-q:a", "2", str(audio_dir / "narration.mp3")])

    assert "".join(chars) == " ".join(s["narration"] for s in shots)
    (audio_dir / "narration.align.json").write_text(
        json.dumps({"characters": chars, "starts": starts, "ends": ends}, ensure_ascii=False))
    (audio_dir / "timing.json").write_text(json.dumps(timing, indent=2) + "\n")
    return offset


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--story", required=True, type=Path)
    ap.add_argument("--run", type=Path, help="run folder (default runs/<story name>)")
    ap.add_argument("--voice-id", help="ElevenLabs voice id (required for narrated stories)")
    ap.add_argument("--voice-model", default="eleven_turbo_v2_5")
    ap.add_argument("--speed", type=float, default=1.1, help="voice speed, 0.7-1.2")
    ap.add_argument("--wps", type=float, default=0.0,
                    help="time-compress every line to this many words/second (0 = leave as spoken)")
    ap.add_argument("--gap", type=float, default=0.15, help="breath between lines, seconds")
    ap.add_argument("--lead-in", type=float, default=0.3, help="silence before the first word")
    ap.add_argument("--redo", default="", help="shot ids whose line / sound effect to regenerate; `music` redoes the music")
    ap.add_argument("--dry-run", action="store_true", help="list what would be generated, call nothing")
    args = ap.parse_args()

    story = load_story(args.story)
    audio_dir = run_dir(args.story, args.run) / "audio"
    narrated = is_narrated(story)
    redo_music = "music" in args.redo.split(",")
    redo = parse_ids(",".join(x for x in args.redo.split(",") if x != "music"))
    sfx_shots = [s for s in story["shots"] if s.get("sfx")]

    if args.dry_run:
        print(f"mode: {'narrated' if narrated else 'action'}")
        if narrated:
            for shot in story["shots"]:
                print(f"line {shot['id']}: {shot['narration']}")
        for shot in sfx_shots:
            print(f"sfx {shot['id']}: {shot['sfx']}")
        print(f"music: {story.get('music', '(none)')}")
        return

    load_env()
    (audio_dir / "lines").mkdir(parents=True, exist_ok=True)
    (audio_dir / "sfx").mkdir(exist_ok=True)

    if narrated:
        assert args.voice_id, "--voice-id is required for a narrated story"
        synth_lines(story, audio_dir / "lines", redo, args)
        total = stitch(story, audio_dir, args)
    else:
        total = sum(float(s["duration"]) for s in story["shots"])
    print(f"\ntotal length {total:.2f}s")

    for shot in sfx_shots:
        path = audio_dir / "sfx" / f"shot_{shot['id']}.mp3"
        if not path.exists() or shot["id"] in redo:
            make_sfx(shot["sfx"]["prompt"], float(shot["sfx"]["seconds"]), path)
            print(f"sfx {shot['id']}: wrote {path}")

    music = audio_dir / "music.mp3"
    if story.get("music") and (redo_music or not music.exists()):
        make_music(story["music"], total, music)
        print(f"wrote {music}")


if __name__ == "__main__":
    main()
