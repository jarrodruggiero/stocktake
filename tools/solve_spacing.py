#!/usr/bin/env python
"""Solve the wordmark's letter offsets by measuring, instead of by eye.

    uv run python tools/solve_spacing.py            # check stocktake
    uv run python tools/solve_spacing.py --word ledger

**Why this is in the repo.** Optical spacing is not something to eyeball, and
this has already had to be rebuilt from scratch twice — once for the wordmark
itself and once for a second one. Any change to a letter width needs
it again.

## What it does

A fixed advance per letter is wrong, because how much air a pair needs depends
on the shapes that face each other: two flat stems need a real gap, two bowls
curving away from one another already have a lens of white between them and
need less. So this measures each letter's ink profile row by row and finds the
advance that keeps the **white area between neighbours constant**, with a
**minimum-clearance floor** so two round letters cannot close up where the area
rule alone would allow it.

## The calibration, which is the part that makes it trustworthy

Two numbers govern it — the target area and the clearance floor — and they are
not guessed. `app/branding.py` already carries nine letters at offsets that
were solved and then approved, so the tool fits both parameters to reproduce
those. It currently lands within **one unit on every letter of `stocktake`**.
If a change to the letterforms makes that error grow, the model no longer
describes the type and the fit is telling you so.

Rendering is done with headless Chrome and read back with Pillow, both of which
`tools/screenshot.py` already needs.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import branding  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
SCALE = 2                 # px per design unit; 2 is plenty at a 21 stem
PAD = 20                  # design units of margin, so nothing is clipped
BOX_W, BOX_H = 140, 240   # a cell big enough for any glyph plus the padding

# `branding.WORDMARK_LETTERS` is (offset, fragment) in word order, so the
# alphabet is recovered by zipping it against the word it spells.
WORD = branding.NAME.lower()
ALPHABET = {letter: fragment
            for letter, (_x, fragment) in zip(WORD, branding.WORDMARK_LETTERS)}
OFFSETS = [x for x, _fragment in branding.WORDMARK_LETTERS]


def _render(fragment: str, out: Path) -> None:
    svg = (f'<svg xmlns="http://www.w3.org/2000/svg" '
           f'width="{BOX_W * SCALE}" height="{BOX_H * SCALE}" '
           f'viewBox="{-PAD} {-PAD} {BOX_W} {BOX_H}">'
           f'<rect x="{-PAD}" y="{-PAD}" width="{BOX_W}" height="{BOX_H}" '
           f'fill="white"/><g fill="black" color="black">{fragment}</g></svg>')
    page = out.with_suffix(".html")
    page.write_text(f'<!doctype html><html><body style="margin:0">{svg}'
                    "</body></html>")
    subprocess.run(
        [CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
         f"--window-size={BOX_W * SCALE},{BOX_H * SCALE}",
         "--virtual-time-budget=1200", f"--screenshot={out}", page.as_uri()],
        check=True, capture_output=True, timeout=60,
    )


def profile(fragment: str, tmp: Path, name: str) -> dict:
    """Leftmost and rightmost ink per scanline, in design units."""
    png = tmp / f"{name}.png"
    _render(fragment, png)
    image = Image.open(png).convert("L")
    width, height = image.size
    pixels = image.load()

    rows: dict[int, tuple[float, float]] = {}
    for y in range(height):
        xs = [x for x in range(width) if pixels[x, y] < 128]
        if xs:
            rows[y] = (min(xs) / SCALE - PAD, (max(xs) + 1) / SCALE - PAD)
    return {"rows": rows, "right": max(r for _l, r in rows.values())}


def _gaps(a: dict, b: dict, advance: float) -> list[float]:
    """The white between two neighbours, per row where BOTH carry ink."""
    return [advance + b["rows"][y][0] - a["rows"][y][1]
            for y in a["rows"] if y in b["rows"]]


def area(a: dict, b: dict, advance: float) -> float:
    gaps = _gaps(a, b, advance)
    return sum(gaps) / SCALE if gaps else 0.0      # rows are px; descale


def clearance(a: dict, b: dict, advance: float) -> float:
    gaps = _gaps(a, b, advance)
    return min(gaps) if gaps else advance


def solve(a: dict, b: dict, target: float, floor: float) -> float:
    low, high = 0.0, 260.0
    for _ in range(60):
        mid = (low + high) / 2
        low, high = (mid, high) if area(a, b, mid) < target else (low, mid)
    advance = (low + high) / 2
    while clearance(a, b, advance) < floor:
        advance += 0.5
    return advance


def fit(prof: dict) -> tuple[float, float, float]:
    """(target area, clearance floor, rms error) against the shipped offsets."""
    wanted = [b - a for a, b in zip(OFFSETS, OFFSETS[1:])]
    pairs = list(zip(WORD, WORD[1:]))
    best = None
    for candidate_area in range(1800, 4200, 25):
        for candidate_floor in range(5, 13):
            error = sum((solve(prof[a], prof[b], candidate_area,
                               candidate_floor) - advance) ** 2
                        for (a, b), advance in zip(pairs, wanted))
            if best is None or error < best[0]:
                best = (error, candidate_area, candidate_floor)
    return best[1], best[2], (best[0] / len(wanted)) ** 0.5


def offsets(word: str, prof: dict, target: float, floor: float) -> list[int]:
    out, x = [0], 0.0
    for left, right in zip(word, word[1:]):
        x += solve(prof[left], prof[right], target, floor)
        out.append(round(x))
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--word", default=WORD,
                        help=f"a word spelled from {''.join(sorted(ALPHABET))}")
    args = parser.parse_args()

    missing = set(args.word) - set(ALPHABET)
    if missing:
        print(f"no letterform for {sorted(missing)} — this alphabet holds "
              f"{''.join(sorted(ALPHABET))}")
        return 1

    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        prof = {letter: profile(fragment, tmp, f"{ord(letter):x}")
                for letter, fragment in ALPHABET.items()}

    target, floor, rms = fit(prof)
    print(f"fit: white area {target}, clearance floor {floor} "
          f"— reproduces {WORD} to {rms:.2f} units per pair")

    solved = offsets(args.word, prof, target, floor)
    print(f"\n{args.word}: {solved}")
    if args.word == WORD:
        print(f"shipped:   {OFFSETS}")
        drift = [s - p for s, p in zip(solved, OFFSETS)]
        print(f"drift:     {drift}")
        # A model that no longer describes the type is not a spacing answer,
        # it is a warning that the letterforms moved underneath it.
        if max(abs(d) for d in drift) > 2:
            print("\nThe fit no longer reproduces the shipped offsets. Either "
                  "the letterforms changed (re-space the word) or the model "
                  "stopped describing them (do not trust the numbers above).")
            return 1
    else:
        ink = solved[-1] + prof[args.word[-1]]["right"]
        print(f"viewBox:   -12 -12 {round(ink) + 24} <depth + 24>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
