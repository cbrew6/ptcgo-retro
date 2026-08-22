"""
Fills the gaps fetch_all_art.py leaves behind.

fetch_all_art.py assumes one picture per set + card number. That is true for
most of the game and wrong in two ways that leave visible holes:

  Variant printings.  An archetype can carry attribute 10020, an asset-name
                      override, and the client then asks for "XY4/065xy"
                      rather than "XY4/065". These are alternate FOIL
                      TREATMENTS of the same illustration - Charizard in
                      Evolutions has three, differing only in foil pattern
                      (AngledPillars / Galaxy / Rainbow) - so the base card's
                      art is the correct art for them. Worse, keying on card
                      number made these archetypes look like duplicates, so
                      they were skipped entirely.

  Trainer Kits.       TK5A..TK10B are reprints, and no public database
                      carries them as sets. Their cards do exist elsewhere
                      though, so they are resolved by name against art
                      already on disk, and only failing that by asking the
                      API for the name directly.

Everything it writes is a copy of art already verified, or a download checked
against the same name rules, so nothing here can invent a card.

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
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))

CARD_DIR = os.path.join(HERE, "carddata")
INDEX_PATH = os.path.join(HERE, "bundle_index.json")
NAME_CACHE = os.path.join(HERE, "tools", "namecache")
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

ATTR_ASSET, ATTR_NAME, ATTR_NUM, ATTR_SET = 10020, 200630, 200780, 200580

API_NAME = ('https://api.pokemontcg.io/v2/cards?q=name:"{name}"'
            '&pageSize=250&select=id,name,number,set,images')

# Which era a Trainer Kit belongs to. A reprint usually keeps its original
# illustration, so preferring the printing from the same era makes the copied
# art more likely to be the one that kit actually used.
KIT_ERA = {"TK5": "BW", "TK6": "XY", "TK7": "XY", "TK8": "XY", "TK9": "XY",
           "TK10": "SM"}


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


def art_by_name(reqs, on_disk):
    """norm(card name) -> [stem, ...] for art we already hold."""
    index = {}
    for s, asset, name, num in reqs:
        if not name or num is None:
            continue
        stem = "%s_%03d" % (s, num)
        if stem in on_disk:
            index.setdefault(faa.norm(name), []).append(stem)
    return index


def pick(stems, prefer):
    """Choose which existing printing to copy, preferring the same era."""
    if prefer:
        for st in stems:
            if st.startswith(prefer):
                return st
    return stems[0]


def api_by_name(name):
    """Cached name search. One request per distinct name, at most."""
    os.makedirs(NAME_CACHE, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9]", "_", name)[:80]
    path = os.path.join(NAME_CACHE, safe + ".json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    try:
        body = json.loads(faa.get(API_NAME.format(
            name=urllib.request.quote(name)), retries=4))
    except Exception as exc:
        log("      name search failed for %r: %s" % (name, str(exc)[:70]))
        return []
    data = body.get("data", [])
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh)
    time.sleep(1.0)
    return data


def save(stem, data):
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
    for s, asset, name, num in reqs:
        stem = stem_of(s, asset)
        if stem in on_disk or stem in bundled or (s, asset) in seen:
            continue
        seen.add((s, asset))
        missing.append((s, asset, name, num))

    log("%d asset requests, %d without art\n" % (len(reqs), len(missing)))

    by_name = art_by_name(reqs, on_disk)
    made = {"variant": 0, "reprint": 0, "downloaded": 0}
    unresolved = []

    for s, asset, name, num in missing:
        stem = stem_of(s, asset)

        # 1. A variant printing: same illustration, different foil treatment.
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

        if not name:
            unresolved.append((stem, "no card name to search on"))
            continue

        # 2. The same card printed in another set, art already verified.
        stems = by_name.get(faa.norm(name))
        if stems:
            era = KIT_ERA.get(re.match(r"(TK\d+)", s).group(1)) if \
                s.startswith("TK") else None
            src = pick(stems, era)
            if not dry:
                shutil.copyfile(os.path.join(LOOSE_ART, src + ".png"),
                                os.path.join(LOOSE_ART, stem + ".png"))
                foil_for(s, asset)
                on_disk.add(stem)
            made["reprint"] += 1
            continue

        # 3. Ask upstream for the name directly.
        if dry:
            unresolved.append((stem, "would search upstream for %r" % name))
            continue
        cards = api_by_name(name)
        chosen = None
        for c in cards:
            if faa.names_agree(name, c.get("name")):
                imgs = c.get("images") or {}
                if imgs.get("large") or imgs.get("small"):
                    chosen = imgs.get("large") or imgs.get("small")
                    break
        if not chosen:
            unresolved.append((stem, "not found upstream: %r" % name))
            continue
        try:
            data = faa.to_card_texture(faa.get(chosen, binary=True))
        except Exception as exc:
            unresolved.append((stem, "download failed: %s" % str(exc)[:60]))
            continue
        save(stem, data)
        foil_for(s, asset)
        on_disk.add(stem)
        by_name.setdefault(faa.norm(name), []).append(stem)
        made["downloaded"] += 1
        time.sleep(faa.IMAGE_DELAY)

    log("%s%d variant printings, %d reprints matched by name, %d downloaded"
        % ("would create: " if dry else "created: ",
           made["variant"], made["reprint"], made["downloaded"]))

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
