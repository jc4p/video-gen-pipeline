#!/usr/bin/env python3
"""Turn photos of a subject (a person, pet, toy, mascot...) into reusable character references.

Writes characters/<name>/front.png, side.png and character.json. Later stages pass those images
to the image model with every shot the character appears in, which is what keeps them
recognizable from shot to shot.

    # first pass: photos -> stylized front + side views
    uv run scripts/01_make_character.py --name mia --photos ~/photos/mia1.jpg ~/photos/mia2.jpg \
        --style "high-end stylized 3D cartoon" --description "Mia, a woman with short red hair"

    # fix-up passes on the existing references (each writes front-<tag>.png / side-<tag>.png)
    uv run scripts/01_make_character.py --name mia --tag v2 --refine-face --photos ~/photos/mia1.jpg
    uv run scripts/01_make_character.py --name mia --tag plain \
        --edit "Replace the printed shirt with a plain solid green t-shirt, no logo or text"

The folder characters/ is gitignored: source photos and the references made from them stay local.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from lib import IMAGE_MODEL, gemini_image, load_env, remove_background

# Short task-style prompts hold a likeness better than long feature-by-feature descriptions.
CREATE = (
    "Turn the subject of these photos into a character drawn in this style: {style}. "
    "Full body, front view, relaxed neutral standing pose, plain white background. "
    "Keep them clearly recognizable: same face, hair, skin tone, build and outfit."
)
SIDE = (
    "This is a character reference. Draw the exact same character from the side (profile view), "
    "full body, same style, same outfit and proportions, plain white background."
)
REFINE_FACE = (
    "Image 1 is a stylized character. The other images are photos of the real subject. Redraw "
    "image 1 so the face matches the real subject more closely and is recognizably them. Keep "
    "everything else in image 1 exactly as it is: style, hair, outfit, pose, framing, background."
)
EDIT = (
    "Edit this character reference: {instruction}. Keep everything else exactly the same: "
    "face, hair, proportions, pose, style, framing and background."
)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", required=True, help="character key, e.g. mia -> characters/mia/")
    ap.add_argument("--photos", nargs="+", type=Path, default=[], help="source photos of the subject")
    ap.add_argument("--style", default="high-end stylized 3D cartoon, soft studio lighting")
    ap.add_argument("--description", help="one line describing the character, used in shot prompts")
    ap.add_argument("--refine-face", action="store_true", help="redraw the face of the current refs against --photos")
    ap.add_argument("--edit", help="apply one edit instruction to the current refs (outfit swap, expression...)")
    ap.add_argument("--tag", help="suffix for fix-up outputs, e.g. v2 -> front-v2.png (required with --refine-face/--edit)")
    ap.add_argument("--cutout", action="store_true", help="remove the background of the outputs with Pixian")
    ap.add_argument("--model", default=IMAGE_MODEL)
    ap.add_argument("--dry-run", action="store_true", help="print prompts, call nothing")
    args = ap.parse_args()

    char_dir = Path("characters") / args.name
    meta_path = char_dir / "character.json"
    meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    fixup = args.refine_face or bool(args.edit)

    jobs: list[tuple[str, list[Path], Path]] = []  # (prompt, reference images, output)
    if fixup:
        assert args.tag, "--tag is required with --refine-face / --edit so earlier refs are kept"
        assert not (args.refine_face and args.edit), "run --refine-face and --edit as separate passes"
        assert meta.get("ref_images"), f"no existing references in {char_dir}; run the first pass"
        if args.refine_face:
            assert args.photos, "--refine-face needs --photos"
        for current in [Path(p) for p in meta["ref_images"]]:
            view = current.stem.split("-")[0]
            out = char_dir / f"{view}-{args.tag}.png"
            if args.refine_face:
                jobs.append((REFINE_FACE, [current, *args.photos], out))
            else:
                jobs.append((EDIT.format(instruction=args.edit.rstrip(".")), [current], out))
    else:
        assert args.photos, "--photos is required for the first pass"
        assert args.description, "--description is required for the first pass"
        front = char_dir / "front.png"
        jobs.append((CREATE.format(style=args.style), list(args.photos), front))
        jobs.append((SIDE, [front], char_dir / "side.png"))
        meta = {"description": args.description, "style": args.style}

    if args.dry_run:
        for prompt, refs, out in jobs:
            print(f"{out}\n  refs: {[str(r) for r in refs]}\n  prompt: {prompt}\n")
        return

    load_env()
    char_dir.mkdir(parents=True, exist_ok=True)
    outputs = []
    for prompt, refs, out in jobs:
        gemini_image(prompt, out, refs=refs, model=args.model, aspect="3:4")
        if args.cutout:
            remove_background(out, out)
        outputs.append(str(out))
        print(f"wrote {out}")

    meta["ref_images"] = outputs
    meta_path.write_text(json.dumps(meta, indent=2) + "\n")
    print(f"wrote {meta_path} — review the images before using them in a story")


if __name__ == "__main__":
    main()
