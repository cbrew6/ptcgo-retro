"""
Repairs the horizontal stretch this script used to apply, and lays card faces
out the way the client's own textures actually are.

THE ACTUAL GEOMETRY (measured, not assumed)
An authentic PTCGO card texture is 1024x1024 and holds three things:

    cols    0..109   white padding
    cols  110..144   BLEED - a horizontal copy of the card's outermost column
    cols  145..877   THE CARD, 733px wide, over the full 1024 height
    cols  878..912   BLEED - a horizontal copy of the card's outermost column
    cols  913..1023  white padding

733/1024 = 0.71582 against the 63:88 paper card's 0.71591. The card sits at its
TRUE ratio and is not stretched at all; the 803-column figure everyone reaches
for is the outer extent of card PLUS bleed.

Verified with tools/bundle_textures.py over all 36 shipped en_US_*_Energy_*
bundles: every one puts the card's left edge at column 145. Twenty-seven bleed
the edge colour outwards to 110..912; the nine Free_Energy ones fill that band
with black instead, which is what proves the band is not card. Within a row the
band is flat and equal to column 145 (mean deviation 0.17-0.46), and on a card
whose edge colour varies down the side it tracks that colour row by row.

The extractor those measurements come from is trustworthy: XY12 bundle textures
decode byte-identically to the XY12 rip PNGs (MAE 0.00, max channel diff 0).

Inside those textures a round type symbol measures 50x50 - a circle - and the
psychic eye glyph 39x28, w/h 1.3929, against 1.3913 for untouched paper art. A
9% horizontal stretch would read 1.52.

WHAT THIS SCRIPT USED TO DO, AND WHY IT WAS WRONG
It read 110..912 as the card and resampled each 734-wide card up to fill it,
which made every face it rewrote ~9.4% too wide. That reading was reinforced by
a second bug elsewhere: most LooseArt faces were at the time the game's own rip
texture with the bleed columns painted white, so they measured 145..878 and
looked like under-wide paper composites. They were not - they were correct
cards missing only their bleed.

The distortion is nearly invisible on illustrated art, which is why the change
was reported as an improvement, and obvious on a basic energy, whose symbol is
a circle. That is how it was caught.

WHAT IT DOES NOW
Restores the file from _backup_20260822/LooseArt and bleeds the card box's
outermost columns out to 110..912. Card pixels are copied through UNRESAMPLED,
so the art keeps the exact proportions it was fetched at. Nothing is scaled,
cropped or padded.

WHY THE GUARD IS THE WHOLE OPERATION
A file is rewritten only when its current content is EXACTLY the old stretch
recomputed from its own backup. That signature cannot arise by accident, so
this can never touch a face it did not damage - in particular not the ~1,288
faces later replaced with the game's own rip textures, which are byte-identical
to the rip and already carry the real bleed. Idempotency comes free: a repaired
file no longer matches the signature and is skipped.

Rips are not consulted here. Replacing a face with its rip belongs to
upgrade_card_art_from_rips.py, which has already taken every face it can serve.
Of the faces left, the only one whose set and number name a rip entry
(BW5 #111) is a 512x512 full-bleed image on a coloured ground - a different
convention entirely - so it is refused rather than reshaped.

Usage:
    python tools/fix_card_geometry.py [--apply]
"""

import collections
import os
import re
import sys

from PIL import Image, ImageChops

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME = os.path.dirname(HERE)
LOOSE_ART = os.path.join(
    GAME,
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)
BACKUP = os.path.join(GAME, "_backup_20260822", "LooseArt")

CANVAS = 1024

# Outer extent of card plus bleed, measured from the shipped bundles. Identical
# for every set.
BLEED_BOX = (110, 912)

# Where each set's card actually sits. fetch_art.py's to_card_texture() centres
# the card over the full height, so the box follows from the source's aspect:
# a 734-wide paper composite lands at 145..878. This is the card, not a guess
# at one - content is verified to lie inside it before anything is written.
PAPER_BOX = (145, 878)

# SM_Energy composited 724 wide rather than 734, and that is measured rather
# than assumed: in all 9 files every one of the 1024 rows starts at exactly
# column 150 and ends at exactly column 873. Treating it as 145..878 would
# bleed from a white column and leave a sliver of padding inside the card.
CARD_BOX = {
    "BW2": PAPER_BOX,
    "BW5": PAPER_BOX,
    "BW6": PAPER_BOX,
    "BW7": PAPER_BOX,
    "Promo_SM": PAPER_BOX,
    "Promo_XY": PAPER_BOX,
    "SM1": PAPER_BOX,
    "XY10": PAPER_BOX,
    "XY11": PAPER_BOX,
    "XY2": PAPER_BOX,
    "XY3": PAPER_BOX,
    "XY4": PAPER_BOX,
    "XY6": PAPER_BOX,
    "XY7": PAPER_BOX,
    "XY8": PAPER_BOX,
    "XY9": PAPER_BOX,
    "SM_Energy": (150, 873),
}
SETS = list(CARD_BOX)

# A base card face. Trailing "xy" is the stamp/foil printing of the same card,
# which is a card face too. Anything else after the set prefix - "packs_...",
# a deck name, a "_wp_" overlay - deliberately does not match.
FACE_RE = {s: re.compile(r"^%s_(\d+)(xy)?\.png$" % re.escape(s)) for s in SETS}

# A channel value above this counts as white. Padding written by a compositor
# is a flat 255, so the tolerance only absorbs PNG-level noise, not real ink.
WHITE = 245


def content_span(img):
    """Columns of the leftmost and rightmost non-white pixel, or None if blank.

    Non-white means *any* channel at or below WHITE, so a pale but coloured
    pixel still counts as content. Working on the per-channel minimum gets that
    in one pass instead of testing three bands separately.
    """
    r, g, b = img.split()
    darkest = ImageChops.darker(ImageChops.darker(r, g), b)
    box = darkest.point(lambda v: 255 if v <= WHITE else 0).getbbox()
    if box is None:
        return None
    return box[0], box[2] - 1


def old_stretch(img, lo, hi):
    """The damage this script used to do. Kept ONLY to recognise it.

    Must stay byte-for-byte what the old version produced, or the guard in
    classify() stops matching and a damaged file goes unrepaired.
    """
    crop = img.crop((lo, 0, hi + 1, CANVAS))
    out = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
    out.paste(crop.resize((BLEED_BOX[1] - BLEED_BOX[0] + 1, CANVAS),
                          Image.LANCZOS), (BLEED_BOX[0], 0))
    return out


def card_edges(img, lo, hi):
    """The card's real outermost columns, or None if content escapes lo..hi.

    CARD_BOX is where the compositor PASTED the card; it is not always where
    the ink starts. BW5 #111 composited 732 wide and so sits at 146..877, one
    column inside the 145..878 box every other face uses. Bleeding from the box
    corner there would replicate a white column - a no-op that leaves the face
    with no bleed at all - so the edges are measured, not assumed.
    """
    span = content_span(img)
    if span is None or span[0] < lo or span[1] > hi:
        return None
    return span


def bleed(img, lo, hi):
    """Extend the card's outermost columns sideways to fill BLEED_BOX.

    Fills outward from the card's real edge, so any near-white padding the
    compositor left between the box corner and the ink is covered by the bleed
    rather than surviving as a sliver inside it.

    Resizing a 1-column strip with NEAREST replicates that exact column, so no
    new colour is invented and the card itself is never read back or rewritten.
    """
    out = img.copy()
    left, right = BLEED_BOX
    out.paste(img.crop((lo, 0, lo + 1, CANVAS))
                 .resize((lo - left, CANVAS), Image.NEAREST), (left, 0))
    out.paste(img.crop((hi, 0, hi + 1, CANVAS))
                 .resize((right - hi, CANVAS), Image.NEAREST), (hi + 1, 0))
    return out


def classify(current, backup, lo, hi):
    """(verdict, detail). "fix" means this face carries the old stretch."""
    if backup.size != (CANVAS, CANVAS):
        return "skip", "backup is %dx%d" % backup.size
    if current.size != (CANVAS, CANVAS):
        return "skip", "current is %dx%d" % current.size

    if content_span(backup) is None:
        return "skip", "backup is blank: no non-white pixel anywhere"
    edges = card_edges(backup, lo, hi)
    if edges is None:
        return "skip", ("backup content reaches outside %d..%d (cols %s) - "
                        "not this set's composite box"
                        % (lo, hi, content_span(backup)))

    repaired = bleed(backup, *edges)
    if ImageChops.difference(current, repaired).getbbox() is None:
        return "skip", "already repaired"

    if ImageChops.difference(current, old_stretch(backup, lo, hi)).getbbox() is None:
        return "fix", "carries the old %d-wide stretch" % (
            BLEED_BOX[1] - BLEED_BOX[0] + 1)

    if ImageChops.difference(current, backup).getbbox() is None:
        # Restored but never bled - the state a repair run leaves behind when
        # it bled from a white box corner. Finishing the job is in scope.
        return "fix", "at backup state but carries no bleed"

    return "skip", ("changed by something else (content cols %s) - left alone"
                    % (content_span(current),))


def write_atomic(img, path):
    """Write via <path>.part + replace, so a crash cannot leave a half PNG."""
    part = path + ".part"
    try:
        img.save(part, "PNG")
        os.replace(part, path)
    except BaseException:
        if os.path.exists(part):
            os.remove(part)
        raise


def targets():
    """Backed-up files that are base card faces of the sets this script owns."""
    for name in sorted(os.listdir(BACKUP)):
        for setcode, pattern in FACE_RE.items():
            if pattern.match(name):
                yield setcode, name
                break


def main(argv):
    apply_changes = "--apply" in argv[1:]
    unknown = [a for a in argv[1:] if a != "--apply"]
    if unknown:
        sys.exit("unrecognised argument(s): %s" % " ".join(unknown))
    if not os.path.isdir(BACKUP):
        sys.exit("no backup directory: %s" % BACKUP)

    fixed = collections.Counter()
    seen = collections.Counter()
    skips = collections.defaultdict(collections.Counter)

    for setcode, name in targets():
        seen[setcode] += 1
        path = os.path.join(LOOSE_ART, name)
        if not os.path.exists(path):
            skips[setcode]["absent from LooseArt"] += 1
            continue
        lo, hi = CARD_BOX[setcode]
        with Image.open(os.path.join(BACKUP, name)) as raw:
            backup = raw.convert("RGB")
        with Image.open(path) as raw:
            current = raw.convert("RGB")

        verdict, detail = classify(current, backup, lo, hi)
        if verdict != "fix":
            skips[setcode][detail.split(" (")[0]] += 1
            continue

        left, right = card_edges(backup, lo, hi)
        repaired = bleed(backup, left, right)
        # Never write a face whose card pixels are not the backup's, untouched.
        if ImageChops.difference(repaired.crop((left, 0, right + 1, CANVAS)),
                                 backup.crop((left, 0, right + 1, CANVAS))
                                 ).getbbox() is not None:
            skips[setcode]["card pixels would change - refused"] += 1
            continue

        fixed[setcode] += 1
        if apply_changes:
            write_atomic(repaired, path)

    verb = "repaired" if apply_changes else "would repair"
    print("mode: %s" % ("APPLY" if apply_changes else "dry run"))
    for setcode in sorted(SETS):
        if not seen[setcode]:
            continue
        print("%-10s %4d faces  %s %4d  skipped %4d"
              % (setcode, seen[setcode], verb, fixed[setcode],
                 seen[setcode] - fixed[setcode]))
    print("%-10s %4d faces  %s %4d  skipped %4d"
          % ("TOTAL", sum(seen.values()), verb, sum(fixed.values()),
             sum(seen.values()) - sum(fixed.values())))

    if any(skips.values()):
        print("\nskips by reason:")
        totals = collections.Counter()
        for setcode in skips:
            for reason, count in skips[setcode].items():
                totals[reason] += count
        for reason, count in totals.most_common():
            print("  %5d  %s" % (count, reason))

    strays = [n for n in os.listdir(LOOSE_ART) if n.endswith(".part")]
    print("\n.part files left in LooseArt: %d" % len(strays))
    for name in strays:
        print("  %s" % name)


if __name__ == "__main__":
    main(sys.argv)
