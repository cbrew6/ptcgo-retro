"""
Fills the gaps fetch_all_art.py leaves behind.

fetch_all_art.py assumes one picture per set + card number. That is true for
most of the game and wrong in two ways that leave visible holes:

Variant printings. An archetype can carry attribute 10020, an asset-name
override, and the client then asks for "XY4/065xy" rather than "XY4/065".
Keying art on the card number makes these look like duplicates of the base
card, so they get dropped and render blank.

They are NOT all the same thing, and treating them as one class produced a
wrong card face. There are two kinds:

  Alternate art.  Attribute 200790 carries a second collector number -
                  "65a/119", "28a/83", "XY150a". These are separate printings
                  with their own illustration: XY4's Aegislash-EX 65a is the
                  full art, not the regular card. Public data indexes by
                  exactly that number, so the client hands us the mapping and
                  the name check confirms it. 19 of these; they are
                  DOWNLOADED, never copied from the base card.

  Stamp / foil.   No second collector number. Settled by extracting both
                  textures from the authentic XY12 bundles, which ship "011"
                  and "011xy" side by side: same card, same HP, ability,
                  attack and illustration, carrying a set-logo stamp in the
                  art box. Copying the base card states nothing false, so
                  these are filled that way.

The lesson: four samples of one kind do not describe a class. The XY12 pair
was real evidence about stamp variants and no evidence at all about alternate
arts, which had to be checked separately.

NO NAME MATCHING.  An earlier version of this filled the Trainer Kits
(TK5A-TK10B) by copying art from a same-named card in another set. That was
wrong and it shipped: TK10B's Alolan Raichu got Crimson Invasion's Alolan
Raichu, which has different attacks. Two cards sharing a name in different
sets are different cards, and carddata carries nothing that distinguishes
them - attribute 10190 looks like a card id but is a per-set constant (all 20
archetypes in TK10B share it). A wrong card face is worse than a blank one,
because it misstates the card while you are playing it.

So art is only ever COPIED between printings sharing a set and a card number
with no separate collector number. Everything else is downloaded against a
number the client itself supplies, or reported unresolved and left blank.

Usage:
    python tools/fix_missing_art.py            # fill what is missing
    python tools/fix_missing_art.py --dry-run  # just report the gaps
"""

import io
import json
import os
import re
import shutil
import sys
import time

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

ATTR_ASSET, ATTR_NAME, ATTR_NUM, ATTR_ALT = 10020, 200630, 200780, 200790

PROVENANCE = os.path.join(HERE, "tools", "art_provenance.json")

# Attribute 200790 carries a secondary collector number for alternate-art
# printings: "65a/119", "28a/83", "XY150a". Public data indexes those under
# exactly that number ("65a"), so it is a real mapping the client hands us -
# not a guess - and the name check still has to pass before anything is saved.
ALT_RE = re.compile(r"^([A-Za-z]*\d+[a-z])")


def alt_number(value):
    if not value:
        return None
    m = ALT_RE.match(value)
    return m.group(1) if m else None


def load_provenance():
    try:
        with open(PROVENANCE, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_provenance(p):
    try:
        with open(PROVENANCE, "w", encoding="utf-8") as fh:
            json.dump(p, fh, indent=0, sort_keys=True)
    except Exception:
        pass


def set_index_for(ptcgo_set):
    """Cached upstream index for a set, or None if we never fetched it."""
    sid = faa.SETS.get(ptcgo_set)
    if not sid:
        return None
    path = os.path.join(HERE, "tools", "setcache", sid + ".json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


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
            alt = alt_number(at.get(ATTR_ALT, {}).get("s")) if override else None
            out.append((s, asset, name, num, alt))
    return out


def save(stem, data):
    """Write atomically, so an interrupt cannot leave a half-written PNG."""
    out = os.path.join(LOOSE_ART, stem + ".png")
    tmp = out + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, out)


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
    for s, asset, name, num, alt in reqs:
        stem = stem_of(s, asset)
        if (s, asset) in seen:
            continue
        if alt:
            seen.add((s, asset))
            missing.append((s, asset, name, num, alt))
            continue
        if stem in on_disk or stem in bundled:
            continue
        seen.add((s, asset))
        missing.append((s, asset, name, num, None))

    log("%d asset requests, %d without art\n" % (len(reqs), len(missing)))

    made = {"alt": 0, "stamp": 0}
    unresolved = []
    prov = load_provenance()

    for s, asset, name, num, alt in missing:
        stem = stem_of(s, asset)

        # 1. An alternate-art printing. The client tells us its collector
        #    number (attribute 200790), public data indexes by exactly that,
        #    and the name still has to match - so this is verified, not
        #    guessed. It must also OVERRIDE any base-card copy already on
        #    disk, which would be the wrong illustration entirely.
        if alt:
            if prov.get(stem) == "alt":
                continue                       # already correctly sourced
            idx = set_index_for(s)
            entry = (idx or {}).get(alt)
            if not entry:
                unresolved.append((stem, "alt art %s not upstream" % alt))
                continue
            remote, url = entry[0], entry[1]
            if not faa.names_agree(name, remote):
                unresolved.append((stem, "alt art %s is %r, we have %r"
                                   % (alt, remote, name)))
                continue
            if not url:
                unresolved.append((stem, "alt art %s has no image" % alt))
                continue
            if dry:
                made["alt"] += 1
                continue
            try:
                data = faa.to_card_texture(faa.get(url, binary=True))
            except Exception as exc:
                unresolved.append((stem, "download failed: %s" % str(exc)[:60]))
                continue
            save(stem, data)
            foil_for(s, asset)
            on_disk.add(stem)
            prov[stem] = "alt"
            made["alt"] += 1
            time.sleep(faa.IMAGE_DELAY)
            continue

        if stem in on_disk or stem in bundled:
            continue

        # 2. A stamp or foil variant: same set, same card number, and the
        #    only safe substitution there is. Established by extracting both
        #    textures from the authentic XY12 bundles - see the header.
        if num is not None:
            base = "%s_%03d" % (s, num)
            if base in on_disk and base != stem:
                if not dry:
                    shutil.copyfile(os.path.join(LOOSE_ART, base + ".png"),
                                    os.path.join(LOOSE_ART, stem + ".png"))
                    foil_for(s, asset)
                    on_disk.add(stem)
                    prov[stem] = "base-copy"
                made["stamp"] += 1
                continue

        # There is deliberately no name-based fallback. See NO NAME MATCHING
        # at the top of this file: two cards sharing a name in different sets
        # are different cards, and nothing in carddata can tell them apart.
        unresolved.append(
            (stem, "no verifiable source" if name else "no card name"))

    if not dry:
        save_provenance(prov)
    log("%s%d alternate arts downloaded, %d stamp variants copied"
        % ("would create: " if dry else "created: ", made["alt"], made["stamp"]))

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
