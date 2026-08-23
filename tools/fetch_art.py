"""
Fetches card images into LooseArt/ for the loose-art patch.

PTCGO uses its own set codes (BW10, XY6, SM2); public card databases use the
official ones (PLB, ROS, GRI). SETS below is that translation, and every
download is name-checked first: we look up the card's name in carddata/ and
compare it to the name on the source page. A mismatch means the set mapping is
wrong, so the file is skipped rather than saved as the wrong card.

Deliberately modest: it takes an explicit list of cards, sleeps between
requests, and is not built for bulk runs. Point it at whatever source you
prefer by editing PAGE/IMAGE.

Usage:
    python tools/fetch_art.py BW10/8 XY6/40 SM2/81
"""

import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARD_DIR = os.path.join(HERE, "carddata")
LOOSE_ART = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)

# PTCGO set code -> official set code. Extend as needed; a wrong entry is
# caught by the name check rather than silently producing wrong art.
SETS = {
    "BW1": "BLW", "BW2": "EPO", "BW3": "NVI", "BW4": "NXD", "BW5": "DEX",
    "BW6": "DRX", "BW7": "BCR", "BW8": "PLS", "BW9": "PLF", "BW10": "PLB",
    "BW11": "LTR",
    "XY0": "KSS", "XY1": "XY", "XY2": "FLF", "XY3": "FFI", "XY4": "PHF",
    "XY5": "PRC", "XY6": "ROS", "XY7": "AOR", "XY8": "BKT", "XY9": "BKP",
    "XY10": "FCO", "XY11": "STS", "XY12": "EVO",
    "SM1": "SUM", "SM2": "GRI", "SM3": "BUS", "SM4": "CIN",
    "HGSS1": "HS", "HGSS2": "UL", "HGSS3": "UD", "HGSS4": "TM",
    "COL": "CL", "DV": "DV",
    "TwentiethAnn": "GEN",
    "Promo_XY": "XYP", "Promo_BW": "BWP", "Promo_SM": "SMP",
    "Promo_HGSS": "HSP",
}

# Sets whose art already ships in StreamingAssets. LooseArt takes priority over
# bundles, so downloading these would REPLACE authentic local art with a
# third-party scan. Skip them.
LOCAL_ART_SETS = ("XY12", "BW_Energy", "HGSS_Energy", "XY_Energy",
                  "Free_Energy", "SM_Energy")

PAGE = "https://limitlesstcg.com/cards/{set}/{num}"
IMAGE = ("https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com"
         "/tpci/{set}/{set}_{num:03d}_R_EN_LG.png")

UA = "Mozilla/5.0 (compatible; ptcgo-local personal archive)"
DELAY = 2.0          # be polite to a community-run site

# Geometry, measured from the client's own shipped textures with
# tools/bundle_textures.py and cross-checked against the rips, which decode
# byte-identically to them. A card texture is 1024x1024 holding three things:
#
#     cols    0..109   white padding
#     cols  110..144   BLEED - a horizontal copy of the card's outermost column
#     cols  145..877   THE CARD, 733px wide, over the full 1024 height
#     cols  878..912   BLEED - a horizontal copy of the card's outermost column
#     cols  913..1023  white padding
#
# 733/1024 = 0.71582 against the 63:88 paper card's 0.71591, so the card sits at
# its TRUE ratio and is not stretched. Every one of the 36 shipped
# en_US_*_Energy_* bundles puts the card's left edge at column 145; 27 bleed the
# edge colour outwards and the 9 Free_Energy ones fill that band with black
# instead, which is what proves the band is not card.
#
# The 803-column figure (110..912) is the outer extent of card PLUS bleed.
# Reading it as the card is what once made fetched faces ~9% too wide: a round
# type symbol, 50x50 in the shipped textures, came out an ellipse. Do NOT
# stretch a 0.716 scan to fill it.
#
# So: fit to full height, keep the source's own aspect, centre it - then bleed
# the card's edge columns out to 110..912, because the display quad crops to
# about that column and would otherwise show a white sliver down each side.
CARD_TEXTURE = (1024, 1024)
PAD_COLOUR = (255, 255, 255, 255)
BLEED_BOX = (110, 912)

ATTR_SET, ATTR_NAME, ATTR_NUM = 200580, 200630, 200780

# Foil mask layers, requested as "{set}_{mask}/{number}" (and a _Foil2 variant
# for the second layer). These are masks, not artwork: with none bound the foil
# shader samples leftover reflection state and paints a stray sheen across the
# middle of the card. A fully transparent black mask means "no foil here",
# which removes the artefact.
FOIL_MASKS = ("wp_std", "wp_ph", "wp_pcd", "wp_secondary")
NEUTRAL_FOIL = (0, 0, 0, 0)


def write_foil_masks(ptcgo_set, number):
    """Blank out every foil layer for one card."""
    try:
        from PIL import Image
    except ImportError:
        return 0
    blank = Image.new("RGBA", CARD_TEXTURE, NEUTRAL_FOIL)
    written = 0
    for mask in FOIL_MASKS:
        for suffix in ("", "_Foil2"):
            name = "%s_%s%s_%03d.png" % (ptcgo_set, mask, suffix, number)
            path = os.path.join(LOOSE_ART, name)
            if not os.path.exists(path):
                blank.save(path, format="PNG")
                written += 1
    return written


def local_card(ptcgo_set, number):
    """Name of this card according to the client's own data."""
    path = os.path.join(CARD_DIR, ptcgo_set + ".json")
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    for a in data.get("archetypes", []):
        at = {x["n"]: (x.get("v") or {}) for x in a["attrs"]}
        if at.get(ATTR_NUM, {}).get("i") == number:
            return at.get(ATTR_NAME, {}).get("s")
    return None


def bleed_edges(canvas, lo, hi):
    """Extend the card's outermost columns sideways to fill BLEED_BOX.

    Resizing a 1-column strip with NEAREST replicates that exact column, so no
    colour is invented and the card's own pixels are never resampled.
    """
    from PIL import Image
    left, right = BLEED_BOX
    h = canvas.height
    if lo > left:
        canvas.paste(canvas.crop((lo, 0, lo + 1, h))
                           .resize((lo - left, h), Image.NEAREST), (left, 0))
    if hi < right:
        canvas.paste(canvas.crop((hi, 0, hi + 1, h))
                           .resize((right - hi, h), Image.NEAREST), (hi + 1, 0))
    return canvas


def to_card_texture(data):
    """Lay the card out the way the client's own textures are laid out.

    1024x1024 canvas: card at its own aspect over the full height, centred,
    then its edge columns bled out to 110..912. See the geometry note above.
    """
    try:
        from PIL import Image
    except ImportError:
        print("    (Pillow not installed - saving as-is; art will look wrong. "
              "pip install Pillow)")
        return data
    import io
    src = Image.open(io.BytesIO(data)).convert("RGBA")
    tw, th = CARD_TEXTURE
    w = max(1, min(tw, int(round(th * src.width / float(src.height)))))
    canvas = Image.new("RGBA", CARD_TEXTURE, PAD_COLOUR)
    x0 = (tw - w) // 2
    canvas.paste(src.resize((w, th), Image.LANCZOS), (x0, 0))
    bleed_edges(canvas, x0, x0 + w - 1)
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


def get(url, binary=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read()
    return body if binary else body.decode("utf-8", "replace")


def fetch(ptcgo_set, number):
    expected = local_card(ptcgo_set, number)
    if not expected:
        print("  %s/%03d  SKIP - not in carddata" % (ptcgo_set, number))
        return False
    code = SETS.get(ptcgo_set)
    if not code:
        print("  %s/%03d  SKIP - no set mapping for %s (add to SETS)"
              % (ptcgo_set, number, ptcgo_set))
        return False

    try:
        html = get(PAGE.format(set=code, num=number))
    except Exception as exc:
        print("  %s/%03d  page failed: %s" % (ptcgo_set, number, exc))
        return False

    m = re.search(r"<title>([^<]*)</title>", html)
    title = m.group(1) if m else ""
    remote = title.split(" - ")[0].strip()
    # PTCGO strips punctuation from names ("CharizardEX") where public
    # databases keep it ("Charizard-EX"). Compare on letters and digits only:
    # still catches a genuinely wrong set mapping, but stops rejecting the
    # same card over a hyphen, apostrophe or space.
    def norm(s):
        return re.sub(r"[^a-z0-9]", "", s.lower())
    if norm(remote) != norm(expected):
        print("  %s/%03d  MISMATCH - we have %r, %s #%d is %r; not saving"
              % (ptcgo_set, number, expected, code, number, remote))
        return False

    try:
        img = get(IMAGE.format(set=code, num=number), binary=True)
    except Exception as exc:
        print("  %s/%03d  image failed: %s" % (ptcgo_set, number, exc))
        return False

    img = to_card_texture(img)
    os.makedirs(LOOSE_ART, exist_ok=True)
    out = os.path.join(LOOSE_ART, "%s_%03d.png" % (ptcgo_set, number))
    with open(out, "wb") as fh:
        fh.write(img)
    n = write_foil_masks(ptcgo_set, number)
    print("  %s/%03d  %-22s -> %s (%d KB)%s"
          % (ptcgo_set, number, expected, os.path.basename(out), len(img) // 1024,
             ("  +%d foil masks" % n) if n else ""))
    return True


CLIENT_LOG = os.path.join(
    os.environ.get("USERPROFILE", ""), "AppData", "LocalLow",
    "The Pokémon Company International",
    "Pokemon Trading Card Game Online", "output_log.txt")

MISS_RE = re.compile(r"\[LooseArt\] miss: ([A-Za-z0-9_]+)/(\d+)\b")


def from_log(path=None):
    """Cards the client actually asked for and couldn't find.

    The patched client logs every unresolved asset, so this fetches exactly
    what you've encountered in game rather than crawling whole sets.
    """
    path = path or CLIENT_LOG
    if not os.path.exists(path):
        print("no client log at %s" % path)
        return []
    wanted = []
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = MISS_RE.search(line)
            if not m:
                continue
            s, n = m.group(1), int(m.group(2))
            if any(s == k or s.startswith(k + "_") for k in LOCAL_ART_SETS):
                continue                      # authentic art already on disk
            if "_wp_" in s:
                continue                      # foil mask, not card art
            spec = "%s/%d" % (s, n)
            if spec not in wanted:
                wanted.append(spec)
    return wanted


def main(args):
    if args and args[0] == "--from-log":
        args = from_log(args[1] if len(args) > 1 else None)
        if not args:
            sys.exit("nothing to fetch from the client log")
        print("%d card(s) requested by the client and missing:\n" % len(args))
    if not args:
        sys.exit("usage: fetch_art.py SET/NUM [SET/NUM ...] | --from-log")
    ok = 0
    for i, spec in enumerate(args):
        if "/" not in spec:
            print("  %s  SKIP - expected SET/NUMBER" % spec)
            continue
        s, n = spec.rsplit("/", 1)
        if i:
            time.sleep(DELAY)
        if fetch(s, int(n)):
            ok += 1
    print("\n%d/%d saved to %s" % (ok, len(args), LOOSE_ART))


if __name__ == "__main__":
    main(sys.argv[1:])
