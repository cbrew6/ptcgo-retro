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
}

PAGE = "https://limitlesstcg.com/cards/{set}/{num}"
IMAGE = ("https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com"
         "/tpci/{set}/{set}_{num:03d}_R_EN_LG.png")

UA = "Mozilla/5.0 (compatible; ptcgo-local personal archive)"
DELAY = 2.0          # be polite to a community-run site

ATTR_SET, ATTR_NAME, ATTR_NUM = 200580, 200630, 200780


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
    if remote.lower() != expected.lower():
        print("  %s/%03d  MISMATCH - we have %r, %s #%d is %r; not saving"
              % (ptcgo_set, number, expected, code, number, remote))
        return False

    try:
        img = get(IMAGE.format(set=code, num=number), binary=True)
    except Exception as exc:
        print("  %s/%03d  image failed: %s" % (ptcgo_set, number, exc))
        return False

    os.makedirs(LOOSE_ART, exist_ok=True)
    out = os.path.join(LOOSE_ART, "%s_%03d.png" % (ptcgo_set, number))
    with open(out, "wb") as fh:
        fh.write(img)
    print("  %s/%03d  %-22s -> %s (%d KB)"
          % (ptcgo_set, number, expected, os.path.basename(out), len(img) // 1024))
    return True


def main(args):
    if not args:
        sys.exit(__doc__.strip().splitlines()[-1])
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
