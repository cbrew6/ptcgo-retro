"""
Fills the gaps fetch_all_art.py leaves behind.

fetch_all_art.py assumes one picture per set + card number. That is true for
most of the game and wrong in two ways that leave visible holes:

  Variant printings.  An archetype can carry attribute 10020, an asset-name
                      override, and the client then asks for "XY4/065xy"
                      rather than "XY4/065". Keying art on the card number
                      makes these archetypes look like duplicates of the base
                      card, so they get dropped and render blank.

                      What they actually are was settled by extracting both
                      textures from the authentic XY12 bundles, which ship
                      "011" and "011xy" side by side: the variant is the SAME
                      card - same name, HP, ability, attack, illustration -
                      carrying a set-logo stamp in the art box. So the base
                      card's art is the right card, and differs only by that
                      cosmetic stamp. Substituting it states nothing false
                      about the card.

NO NAME MATCHING.  An earlier version of this filled the Trainer Kits
(TK5A-TK10B) by copying art from a same-named card in another set. That was
wrong and it shipped: TK10B's Alolan Raichu got Crimson Invasion's Alolan
Raichu, which has different attacks. Two cards sharing a name in different
sets are different cards, and carddata carries nothing that distinguishes
them - attribute 10190 looks like a card id but is a per-set constant (all 20
archetypes in TK10B share it). A wrong card face is worse than a blank one,
because it misstates the card while you are playing it.

So this tool only ever copies art between printings that share a set AND a
card number. Anything else is reported as unresolved and left blank.

Usage:
    python tools/fix_missing_art.py            # fill what is missing
    python tools/fix_missing_art.py --dry-run  # just report the gaps
"""

import io
import json
import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))

CARD_DIR = os.path.join(HERE, "carddata")
INDEX_PATH = os.path.join(HERE, "bundle_index.json")
LOOSE_ART = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)

os.environ.pop("SSLKEYLOGFILE", None)

# Reuse the pieces that are already proven rather than restating them.
import importlib.util                                        # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "fetch_all_art", os.path.join(HERE, "tools", "fetch_all_art.py"))
faa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(faa)

ATTR_ASSET, ATTR_NAME, ATTR_NUM = 10020, 200630, 200780


def log(msg):
    faa.log(msg)


def bundled_assets():
    """set_asset stems the shipped bundles already provide."""
    if not os.path.exists(INDEX_PATH):
        return set()
    with open(INDEX_PATH, encoding="utf-8") as fh:
        idx = json.load(fh)
    out = set()
    for bundle, names in idx.items():
        parts = bundle.split("_")
        start = 1
        for k, p in enumerate(parts):
            if p == "wp" and k + 1 < len(parts):
                start = k + 2
                break
        if start != 1:
            continue                      # foil bundle, not artwork
        for i in range(start, len(parts) + 1):
            pref = "_".join(parts[:i])
            for n in names:
                out.add("%s_%s" % (pref, n))
    return out


def requirements():
    """Every asset the client can ask for: (set, asset name, card name, number).

    The asset name is attribute 10020 when present - that override is the
    whole reason variant printings were being missed - and the zero-padded
    card number otherwise.
    """
    out = []
    for fn in sorted(os.listdir(CARD_DIR)):
        if not fn.endswith(".json"):
            continue
        s = fn[:-5]
        try:
            with open(os.path.join(CARD_DIR, fn), encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            continue
        for a in data.get("archetypes", []):
            at = {x["n"]: (x.get("v") or {}) for x in a["attrs"]}
            num = at.get(ATTR_NUM, {}).get("i")
            name = at.get(ATTR_NAME, {}).get("s")
            override = at.get(ATTR_ASSET, {}).get("s")
            asset = override or ("%03d" % num if num is not None else None)
            if not asset:
                continue
            out.append((s, asset, name, num))
    return out


def stem_of(s, asset):
    """File stem for an asset request. The loose-art patch maps '/' to '_',
    so "packs/BW1BlackWhite" becomes part of a filename, not a directory."""
    return ("%s_%s" % (s, asset)).replace("/", "_").replace("\\", "_")


def foil_for(s, asset):
    """Neutral foil masks for one asset.

    Takes the set and asset separately. A set name can itself contain an
    underscore (Promo_XY, BW_Energy), so splitting a joined stem guesses the
    boundary wrong and writes masks under a name the client never asks for.
    """
    asset = asset.replace("/", "_")
    if faa.BLANK_FOIL is None:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGBA", faa.CARD_TEXTURE, faa.NEUTRAL_FOIL).save(
            buf, format="PNG")
        faa.BLANK_FOIL = buf.getvalue()
    n = 0
    for mask in faa.FOIL_MASKS:
        for suffix in ("", "_Foil2"):
            p = os.path.join(LOOSE_ART, "%s_%s%s_%s.png"
                             % (s, mask, suffix, asset))
            if not os.path.exists(p):
                with open(p, "wb") as fh:
                    fh.write(faa.BLANK_FOIL)
                n += 1
    return n


def main(argv):
    dry = "--dry-run" in argv
    os.makedirs(LOOSE_ART, exist_ok=True)

    reqs = requirements()
    on_disk = {f[:-4] for f in os.listdir(LOOSE_ART) if f.endswith(".png")}
    bundled = bundled_assets()

    missing, seen = [], set()
    for s, asset, name, num in reqs:
        stem = stem_of(s, asset)
        if stem in on_disk or stem in bundled or (s, asset) in seen:
            continue
        seen.add((s, asset))
        missing.append((s, asset, name, num))

    log("%d asset requests, %d without art\n" % (len(reqs), len(missing)))

    made = {"variant": 0}
    unresolved = []

    for s, asset, name, num in missing:
        stem = stem_of(s, asset)

        # The only safe substitution: same set, same card number.
        if num is not None:
            base = "%s_%03d" % (s, num)
            if base in on_disk and base != stem:
                if not dry:
                    shutil.copyfile(os.path.join(LOOSE_ART, base + ".png"),
                                    os.path.join(LOOSE_ART, stem + ".png"))
                    foil_for(s, asset)
                    on_disk.add(stem)
                made["variant"] += 1
                continue

        # There is deliberately no name-based fallback. See NO NAME MATCHING
        # at the top of this file: two cards sharing a name in different sets
        # are different cards, and nothing in carddata can tell them apart.
        unresolved.append(
            (stem, "no verifiable source" if name else "no card name"))

    log("%s%d variant printings"
        % ("would create: " if dry else "created: ", made["variant"]))

    if unresolved:
        log("\n%d still without art:" % len(unresolved))
        kinds = {}
        for stem, why in unresolved:
            kind = ("product art (packs, decks, boxes)"
                    if "no card name" in why else why.split(":")[0])
            kinds.setdefault(kind, []).append(stem)
        for kind, stems in sorted(kinds.items(), key=lambda kv: -len(kv[1])):
            log("  %-38s %4d   e.g. %s"
                % (kind, len(stems), ", ".join(stems[:3])))


if __name__ == "__main__":
    main(sys.argv[1:])
