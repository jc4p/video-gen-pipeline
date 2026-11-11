#!/usr/bin/env python3
"""Cut the clips together, burn captions, mix the soundtrack. No API calls.

    uv run scripts/06_assemble.py --story examples/lighthouse.story.json
    uv run scripts/06_assemble.py --story examples/lighthouse.story.json --captions voiceover --voiceover-y 0.3

Reads runs/<story>/clips and runs/<story>/audio, writes runs/<story>/final/<story>.mp4 plus a
smaller <story>.small.mp4 for sharing. Captions are burned here, after generation, so changing
their text, size or position never costs a regenerated clip.
"""
from __future__ import annotations

import argparse
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from lib import SIZES, is_narrated, load_story, media_duration, run, run_dir, shot_name

Caption = tuple[float, float, str]  # start, end, text


# ---------- captions ----------

def balanced_chunks(words: list[str], max_words: int) -> list[str]:
    """Split into the fewest chunks of <= max_words, evenly sized, so no chunk is a lone last word."""
    if not words:
        return []
    count = -(-len(words) // max_words)
    base, extra = divmod(len(words), count)
    chunks, cursor = [], 0
    for i in range(count):
        size = base + (1 if i < extra else 0)
        chunks.append(" ".join(words[cursor:cursor + size]))
        cursor += size
    return chunks


def caption_chunks(line: str, max_words: int) -> list[str]:
    """Break a narration line at sentence and clause boundaries, then into short balanced chunks."""
    chunks: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+", line.strip()):
        parts: list[str] = []
        for clause in re.split(r"(?<=[,;:])\s+", sentence):
            parts.extend(balanced_chunks(clause.split(), max_words))
        # Fold one-word leftovers (", too.") into the chunk before them.
        merged: list[str] = []
        for part in parts:
            if merged and len(part.split()) == 1:
                merged.extend(balanced_chunks((merged.pop() + " " + part).split(), max_words))
            else:
                merged.append(part)
        chunks.extend(merged)
    return chunks


def word_times(align: dict[str, Any]) -> list[tuple[float, float]]:
    words: list[tuple[float, float]] = []
    start = None
    end = 0.0
    for ch, s, e in zip(align["characters"], align["starts"], align["ends"]):
        if ch.isspace():
            if start is not None:
                words.append((start, end))
                start = None
        else:
            start = s if start is None else start
            end = e
    if start is not None:
        words.append((start, end))
    return words


def voiceover_captions(story: dict[str, Any], align: dict[str, Any], max_words: int) -> list[Caption]:
    times = word_times(align)
    captions: list[Caption] = []
    w = 0
    for shot in story["shots"]:
        for chunk in caption_chunks(shot["narration"], max_words):
            n = len(chunk.split())
            captions.append((times[w][0], times[w + n - 1][1], chunk))
            w += n
    assert w == len(times), "caption words do not line up with the narration audio; re-run 05_audio.py"
    return captions


def ass_time(seconds: float) -> str:
    cs = int(round(max(0.0, seconds) * 100))
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def wrap(text: str, max_chars: int) -> str:
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}".strip()
    return r"\N".join([*lines, current])


def build_ass(width: int, height: int, font: str, overlays: list[Caption], voiceover: list[Caption],
              overlay_y: float, voiceover_y: float) -> str:
    size_vo, size_ov = round(height * 0.044), round(height * 0.048)
    style = ("Style: {name},{font},{size},{color},&H000000FF,&H00000000,&H80000000,-1,0,0,0,100,100,0,0,1,"
             "{outline},3,{align},60,60,{margin},1")
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {width}", f"PlayResY: {height}",
        "WrapStyle: 2", "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, "
        "Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding",
        # Voiceover: white, measured up from the bottom. Overlay labels: yellow, down from the top.
        style.format(name="Voiceover", font=font, size=size_vo, color="&H00FFFFFF", outline=round(size_vo * 0.09),
                     align=2, margin=round(height * voiceover_y)),
        style.format(name="Overlay", font=font, size=size_ov, color="&H0000E6FF", outline=round(size_ov * 0.09),
                     align=8, margin=round(height * overlay_y)),
        "", "[Events]",
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for name, size, captions in (("Overlay", size_ov, overlays), ("Voiceover", size_vo, voiceover)):
        max_chars = int(0.8 * width / (0.55 * size))
        for start, end, text in captions:
            clean = text.upper().replace("\\", "").replace("{", "(").replace("}", ")")
            lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},{name},,0,0,0,,{wrap(clean, max_chars)}")
    return "\n".join(lines) + "\n"


# ---------- picture ----------

def fit_clip(source: Path, target: Path, seconds: float, width: int, height: int) -> None:
    """Retime a clip to exactly `seconds` and conform it to the output size; drops clip audio."""
    factor = seconds / media_duration(source)
    run(["ffmpeg", "-y", "-i", str(source), "-an",
         "-vf", f"setpts={factor:.8f}*PTS,scale={width}:{height}:force_original_aspect_ratio=increase,"
                f"crop={width}:{height},setsar=1,fps=30",
         "-t", f"{seconds:.3f}", "-c:v", "libx264", "-crf", "16", "-pix_fmt", "yuv420p", str(target)])


# ---------- sound ----------

def mix_graph(total: float, narration: int | None, music: int | None, sfx: list[tuple[int, float]]) -> str:
    """ffmpeg filter graph: narration on top, music ducked under it, effects at their cue times."""
    stereo = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    parts, tracks = [], []
    if narration is not None:
        parts.append(f"[{narration}:a]{stereo},asplit=2[vo][key]")
        tracks.append("[vo]")
    if music is not None:
        level = -27 if narration is not None else -18
        fade = max(0.0, total - 1.0)
        parts.append(f"[{music}:a]atrim=end={total:.3f},afade=t=out:st={fade:.3f}:d=1,"
                     f"loudnorm=I={level}:TP=-6:LRA=11,{stereo}[bed]")
        if narration is not None:
            parts.append("[bed][key]sidechaincompress=threshold=0.03:ratio=4:attack=20:release=350[duck]")
            tracks.append("[duck]")
        else:
            tracks.append("[bed]")
    elif narration is not None:
        parts.append("[key]anullsink")
    for index, at in sfx:
        parts.append(f"[{index}:a]{stereo},adelay={round(at * 1000)}:all=1[fx{index}]")
        tracks.append(f"[fx{index}]")
    parts.append(f"{''.join(tracks)}amix=inputs={len(tracks)}:duration=longest:normalize=0,"
                 f"loudnorm=I=-14:TP=-1.5:LRA=11,{stereo},apad[a]")
    return ";".join(parts)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--story", required=True, type=Path)
    ap.add_argument("--run", type=Path, help="run folder (default runs/<story name>)")
    ap.add_argument("--captions", default="both", choices=["both", "voiceover", "overlay", "none"])
    ap.add_argument("--voiceover-y", type=float, default=0.16, help="voiceover captions: distance from the bottom, 0-1")
    ap.add_argument("--overlay-y", type=float, default=0.14, help="overlay labels: distance from the top, 0-1")
    ap.add_argument("--max-words", type=int, default=3, help="words per voiceover caption")
    ap.add_argument("--font", default="Anton", help="any installed font; falls back to the system default")
    args = ap.parse_args()

    story = load_story(args.story)
    out = run_dir(args.story, args.run)
    clips_dir, audio_dir, final_dir = out / "clips", out / "audio", out / "final"
    final_dir.mkdir(parents=True, exist_ok=True)
    width, height = SIZES[story["aspect"]]
    narrated = is_narrated(story)
    shots = story["shots"]

    if narrated:
        assert (audio_dir / "timing.json").exists(), "narrated story: run 05_audio.py first"
        timing = json.loads((audio_dir / "timing.json").read_text())
        durations = [float(timing[s["id"]]) for s in shots]
    else:
        durations = [float(s["duration"]) for s in shots]
    starts = [sum(durations[:i]) for i in range(len(shots))]
    total = sum(durations)

    overlays: list[Caption] = []
    voiceover: list[Caption] = []
    if args.captions in ("both", "overlay"):
        overlays = [(start, start + dur, shot["overlay_text"])
                    for shot, start, dur in zip(shots, starts, durations) if shot.get("overlay_text")]
    if args.captions in ("both", "voiceover") and narrated:
        align = json.loads((audio_dir / "narration.align.json").read_text())
        voiceover = voiceover_captions(story, align, args.max_words)

    name = out.name
    final, small = final_dir / f"{name}.mp4", final_dir / f"{name}.small.mp4"
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        concat = tmp_dir / "concat.txt"
        entries = []
        for shot, seconds in zip(shots, durations):
            source = clips_dir / f"{shot_name(shot)}.mp4"
            assert source.exists(), f"missing clip {source}; run 04_videos.py"
            stretch = seconds / media_duration(source)
            if stretch > 1.6:
                print(f"shot {shot['id']}: clip slowed {stretch:.1f}x to cover {seconds:.1f}s; "
                      f"consider a longer `generation_seconds` or splitting the shot")
            fitted = tmp_dir / f"{shot['id']}.mp4"
            fit_clip(source, fitted, seconds, width, height)
            entries.append(f"file '{fitted}'\n")
        concat.write_text("".join(entries))

        cmd = ["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(concat)]
        picture = "[0:v]null[v]"
        if overlays or voiceover:
            ass = tmp_dir / "captions.ass"
            ass.write_text(build_ass(width, height, args.font, overlays, voiceover,
                                     args.overlay_y, args.voiceover_y))
            picture = f"[0:v]subtitles=filename={ass}[v]"

        inputs: list[Path] = []
        narration_i = music_i = None
        if narrated:
            inputs.append(audio_dir / "narration.mp3")
            narration_i = len(inputs)
        if (audio_dir / "music.mp3").exists():
            inputs.append(audio_dir / "music.mp3")
            music_i = len(inputs)
        sfx = []
        for shot, start in zip(shots, starts):
            path = audio_dir / "sfx" / f"shot_{shot['id']}.mp3"
            if shot.get("sfx") and path.exists():
                inputs.append(path)
                sfx.append((len(inputs), start + float(shot["sfx"].get("at", 0.0))))
        for path in inputs:
            cmd += ["-i", str(path)]

        if inputs:
            graph = f"{picture};{mix_graph(total, narration_i, music_i, sfx)}"
            cmd += ["-filter_complex", graph, "-map", "[v]", "-map", "[a]", "-c:a", "aac", "-b:a", "192k"]
        else:
            cmd += ["-filter_complex", picture, "-map", "[v]"]
        cmd += ["-t", f"{total:.3f}", "-c:v", "libx264", "-crf", "18", "-pix_fmt", "yuv420p",
                "-movflags", "+faststart", str(final)]
        run(cmd)

    run(["ffmpeg", "-y", "-i", str(final), "-vf", f"scale={width * 2 // 3 // 2 * 2}:-2", "-c:v", "libx264",
         "-crf", "28", "-preset", "slow", "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(small)])
    print(f"{final}  {media_duration(final):.2f}s  {final.stat().st_size / 1e6:.1f}MB")
    print(f"{small}  {small.stat().st_size / 1e6:.1f}MB")


if __name__ == "__main__":
    main()
