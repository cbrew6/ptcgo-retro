"""
Re-composites card faces that were built at the true paper ratio instead of the
game's own.

An authentic PTCGO card texture is 1024x1024 with the card occupying exactly
803x1024, centred - i.e. the card is deliberately stretched horizontally away
from the 63:88 ratio of the physical card, with ~110px of white padding down
each side. That convention holds across every rip checked (bw1, xy1, xy12, sm8,
hgss1, rsp, tk6a), so the client clearly expects it: it maps the whole 1024
texture onto a card quad and lets the padding fall outside the visible face.

Some art was instead fetched from api.pokemontcg.io and composited at the true
paper ratio on a 1024 canvas - 734x1024 for most sets, putting the card in
columns 145..878. Those cards render ~9% too narrow in game. Most sets can
simply be replaced with the game's ripped texture; the sets handled here have
no rip available, so the only available fix is to re-stretch the existing
pixels. The source width is not the same everywhere, so it is a per-set
parameter (see SOURCE_SPAN) rather than one global constant.

WHY THE EDGE CHECK IS THE WHOLE OPERATION
This transform is NOT idempotent - stretching an already-correct 803px card
again would take it to 879px and wreck it. The guard is that a file is only
rewritten when its content is measured to start and end at exactly the columns
its set is expected to occupy. An already-corrected file spans 110..912 and so
fails that test; it is recognised by name and skipped. A file matching neither
is skipped rather than guessed at.

The check is never relaxed to raise the success count, and it demands that the
content REACH both edges rather than merely sit inside them. That strictness
earned itself: SM_Energy has white margins wide enough to pass the weaker
"outer columns are white" form of the test against the 145..878 span, and
would have been silently cropped 5px too wide on each side. Measuring instead
of assuming turned that into a known per-set difference.

Only base card faces are touched: "<SET>_<number>.png" and the stamp-variant
"<SET>_<number>xy.png". Files carrying "_wp_" are transparent stamp overlays
with entirely different geometry, and the deck/pack/promo images (e.g.
"BW2_packs_...", "XY6_stormriderxy6deck") are not card faces at all. Neither
is matched by the pattern, so neither can be reached.

Usage:
    python tools/fix_card_geometry.py [--apply]
"""

import collections
import os
import re
import sys

from PIL import Image, ImageChops

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOSE_ART = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)

# Sets with no ripped texture available, so re-compositing is the only fix,
# each with the source span (inclusive columns) its art was actually built at.
#
# Most api.pokemontcg.io art was composited at the true 63:88 paper ratio,
# 734x1024 centred, i.e. columns 145..878.
#
# SM_Energy is 724px wide, not 734, and that is measured rather than assumed:
# in all 9 files every one of the 1024 rows starts at exactly column 150 and
# ends at exactly column 873, symmetric about the same centre. These cards are
# square-cornered, so there are no rounded corners to explain the inset - the
# compositor simply used a slightly narrower card width for this set. It is a
# real difference, NOT a mistake in this table: cropping them at 145..878 like
# every other set would bake a ~5px white sliver into each side and leave only
# 792px of card inside the 803px box.
#
# Whole sets are listed even though most of their files are already correct.
# That is deliberate: an already-correct file is recognised and skipped, so
# naming the set costs nothing and avoids maintaining a per-file list.
#
# Sets not listed here are unreachable by this script. That matters for
# TK10A_005.png, which is a copy of SM_Energy_006 and so carries the identical
# 724-wide signature: it belongs to another set's namespace and is handled by
# hand elsewhere, so "TK10A" is deliberately absent below.
PAPER_SPAN = (145, 878)
SOURCE_SPAN = {
    "BW2": PAPER_SPAN,
    "BW5": PAPER_SPAN,
    "BW6": PAPER_SPAN,
    "BW7": PAPER_SPAN,
    "Promo_SM": PAPER_SPAN,
    "Promo_XY": PAPER_SPAN,
    "SM1": PAPER_SPAN,
    "XY10": PAPER_SPAN,
    "XY11": PAPER_SPAN,
    "XY2": PAPER_SPAN,
    "XY3": PAPER_SPAN,
    "XY4": PAPER_SPAN,
    "XY6": PAPER_SPAN,
    "XY7": PAPER_SPAN,
    "XY8": PAPER_SPAN,
    "XY9": PAPER_SPAN,
    "SM_Energy": (150, 873),
}
SETS = list(SOURCE_SPAN)

# A base card face. Trailing "xy" is the stamp/foil printing of the same card,
# which is a card face too. Anything else after the set prefix - "packs_...",
# a deck name, a "_wp_" overlay - deliberately does not match.
FACE_RE = {s: re.compile(r"^%s_(\d+)(xy)?\.png$" % re.escape(s)) for s in SETS}

CANVAS = 1024

# The target geometry, identical for every set: the game's own convention.
DST_LEFT, DST_WIDTH = 110, 803

# A channel value above this counts as white. Padding written by a compositor
# is a flat 255, so the tolerance only absorbs PNG-level noise, not real ink.
WHITE = 245

# How far short of its nominal edge the detected content may stop and still
# count as reaching it. Some cards have a near-white border whose outermost
# column or two sits above WHITE and so goes undetected; without this they
# would be reported as unexplained rather than fixed. Kept at 2 because the
# under-detection actually observed is one column, and every pixel of slack
# here becomes a white sliver inside the rewritten card.
#
# This slack applies in ONE direction only - see span_matches().
REACH_TOL = 2

# Used ONLY to label a skip (see classify), never to admit a rewrite, so it can
# be generous where REACH_TOL cannot.
NEAR_TOL = 8


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


def span_matches(left, right, lo, hi):
    """Is content at [left,right] the geometry [lo,hi]?

    The two halves of this are deliberately asymmetric, and the asymmetry is
    the safety property:

      CONTAINMENT is absolute. Content outside lo..hi means the crop would cut
      the card, so it is never tolerated by any amount. Promo_SM_078 (content
      from column 144) and XY4_122 (from 142) are real files that would each
      lose a column of border to a symmetric tolerance. Losing card is worse
      than leaving a card slightly narrow, so those are skipped and reported.

      REACHING is tolerant, by REACH_TOL, because stopping a column short of
      the edge is a detection artifact of a near-white border rather than
      different geometry, and costs at most a hairline of white.

    An earlier version used abs() on both ends, which silently permitted
    content up to 3 columns OUTSIDE the span. Nothing had yet hit that case,
    but XY4 does.
    """
    if left < lo or right > hi:
        return False
    return left <= lo + REACH_TOL and right >= hi - REACH_TOL


def classify(img, src_left, src_right):
    """(verdict, detail) for one already-opened RGB image.

    Verdict "fix" means this set's expected centred composite was positively
    identified. Every other verdict is a skip.

    Content must REACH src_left and src_right, not merely sit somewhere
    inside them. Testing only that the outer margins are white would have
    accepted SM_Energy against the 145..878 span every other set uses and
    quietly baked a white sliver down each side of all 9 files. The strict
    form is what caught that, so it is applied to every set.
    """
    span = content_span(img)
    if span is None:
        return "skip", "blank: no non-white pixel anywhere"
    left, right = span
    dst_right = DST_LEFT + DST_WIDTH - 1

    if span_matches(left, right, src_left, src_right):
        return "fix", "%dpx composite (cols %d..%d)" % (
            src_right - src_left + 1, left, right)

    # Explicit idempotency guard. Not needed for correctness - such a file has
    # ink in columns 110..144 and so already failed the test above - but named
    # so that a second run reports "already correct" instead of "unexpected".
    if span_matches(left, right, DST_LEFT, dst_right):
        return "skip", "already correct 803-wide geometry"

    # Reporting only, and deliberately loose. Plenty of authentic textures
    # measure a few columns off 110..912 - a dark border bleeds a pixel or two
    # past the card edge, or a full-art promo is drawn marginally narrow. They
    # are nowhere near the 734 signature and are not ours to touch, so they are
    # named for what they are instead of being lumped in with the clip warning
    # below. Widening this tolerance can only change a skip message; it can
    # never turn a skip into a rewrite.
    if abs(left - DST_LEFT) <= NEAR_TOL and abs(right - dst_right) <= NEAR_TOL:
        return "skip", "at or near 803-wide geometry (cols %d..%d) - not the "                        "734 signature" % (left, right)

    # Called out separately from a plain span mismatch because this is the
    # case where rewriting would destroy card, not merely mis-pad it.
    if left < src_left or right > src_right:
        return "skip", (
            "content reaches outside %d..%d (cols %d..%d, %dpx wide) - "
            "cropping would clip the card"
            % (src_left, src_right, left, right, right - left + 1))

    return "skip", "unexpected content span (cols %d..%d, %dpx wide)" % (
        left, right, right - left + 1)


def restretch(img, src_left, src_right):
    """This set's composite, re-laid out at the game's 803-wide convention."""
    crop = img.crop((src_left, 0, src_right + 1, CANVAS))
    out = Image.new("RGB", (CANVAS, CANVAS), (255, 255, 255))
    out.paste(crop.resize((DST_WIDTH, CANVAS), Image.LANCZOS), (DST_LEFT, 0))
    return out


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
    """Existing LooseArt files that are base card faces of the target sets."""
    for name in sorted(os.listdir(LOOSE_ART)):
        for setcode, pattern in FACE_RE.items():
            if pattern.match(name):
                yield setcode, name
                break


def main(argv):
    apply_changes = "--apply" in argv[1:]
    unknown = [a for a in argv[1:] if a != "--apply"]
    if unknown:
        sys.exit("unrecognised argument(s): %s" % " ".join(unknown))

    fixed = collections.Counter()
    seen = collections.Counter()
    skips = collections.defaultdict(collections.Counter)
    skip_files = collections.defaultdict(list)

    for setcode, name in targets():
        seen[setcode] += 1
        path = os.path.join(LOOSE_ART, name)
        with Image.open(path) as raw:
            size = raw.size
            if size != (CANVAS, CANVAS):
                reason = "not 1024x1024 (is %dx%d)" % size
                skips[setcode][reason] += 1
                skip_files[reason].append(name)
                continue
            # Flatten to RGB up front: a stray alpha channel or palette would
            # otherwise make "white" mean different things from file to file.
            img = raw.convert("RGB")

        src_left, src_right = SOURCE_SPAN[setcode]
        verdict, detail = classify(img, src_left, src_right)
        if verdict != "fix":
            skips[setcode][detail] += 1
            skip_files[detail].append(name)
            continue

        fixed[setcode] += 1
        if apply_changes:
            write_atomic(restretch(img, src_left, src_right), path)

    verb = "rewritten" if apply_changes else "would rewrite"
    print("mode: %s" % ("APPLY" if apply_changes else "dry run"))
    for setcode in SETS:
        print("%-10s %4d faces  %s %4d  skipped %4d"
              % (setcode, seen[setcode], verb, fixed[setcode],
                 seen[setcode] - fixed[setcode]))
    print("%-10s %4d faces  %s %4d  skipped %4d"
          % ("TOTAL", sum(seen.values()), verb, sum(fixed.values()),
             sum(seen.values()) - sum(fixed.values())))

    if any(skips.values()):
        print("\nskips by set and reason:")
        for setcode in SETS:
            for reason, count in sorted(skips[setcode].items()):
                print("  %-10s %4d  %s" % (setcode, count, reason))
        print("\nskipped files:")
        for reason, names in sorted(skip_files.items()):
            print("  %s:" % reason)
            for name in names:
                print("    %s" % name)

    strays = [n for n in os.listdir(LOOSE_ART) if n.endswith(".part")]
    print("\n.part files left in LooseArt: %d" % len(strays))
    for name in strays:
        print("  %s" % name)


if __name__ == "__main__":
    main(sys.argv)
