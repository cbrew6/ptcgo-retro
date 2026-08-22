"""
Replaces LooseArt base card faces with the game's own ripped textures.

Art fetched from api.pokemontcg.io was composited at 734x1024 - the card's true
63:88 ratio. The client's own textures put the card at 803x1024 inside the same
1024x1024 canvas: horizontally stretched, centred, white padding either side.
The API art therefore renders about 9% too narrow. The rips fix that exactly.

Three rules this script does not bend:

  - A card is identified by set code plus collector number, never by name.
    Name matching once put Crimson Invasion's Alolan Raichu (31/111) into a
    Trainer Kit slot wanting a different card of the same name (17/30).
  - The target set key comes from carddata's "set" field, never from
    uppercasing the rip folder. "twentiethann" is "TwentiethAnn"; on a
    case-insensitive filesystem "TWENTIETHANN" would silently clobber it while
    an `in os.listdir()` check still reported success.
  - A file is only written under a name carddata asks for or that LooseArt
    already holds. Anything else is reported and skipped, never invented.

Slot names are "<SetKey>_<num>.png". <num> is attribute 10020 when that is an
art-variant suffix ("017a", "043xy"), otherwise attribute 200780 zero-padded to
three digits. A 10020 that names a product ("packs/...", "xy1destructionrush")
marks the archetype as not a card face at all.

Usage:
    python tools/upgrade_card_art_from_rips.py [--apply] [--sets=A,B,...]
"""

import collections
import hashlib
import io
import json
import os
import re
import struct
import sys
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAME = os.path.dirname(REPO)
CARDDATA = os.path.join(REPO, "carddata")
LOOSE_ART = os.path.join(
    GAME,
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)
ZIP_DIR = r"C:\Users\cbrew\Downloads\PTCGO"

# The 50 sets the rip covers and this run owns. Everything else in LooseArt -
# BW2, BW6, XY6, XY9, the energy sets, AvatarItems, NoSet, RewardItems - belongs
# to another agent and must not be touched.
SCOPE = """
BW1 BW10 BW11 BW3 BW4 BW5 BW7 BW8 BW9 COL DV HGSS1 HGSS2 HGSS3 HGSS4 Promo_BW
Promo_HGSS Promo_SM Promo_XY RSP SL SM1 SM2 SM3 SM4 TATM TK10A TK10B TK5A TK5B
TK6A TK6B TK7A TK7B TK8A TK8B TK9A TK9B TwentiethAnn XY0 XY1 XY10 XY11 XY12 XY2
XY3 XY4 XY5 XY7 XY8
""".split()

# A card-face suffix: bare digits, or digits plus a variant tag ("017a",
# "043xy", "078en"). Anything else under attribute 10020 is a product image.
FACE_SUFFIX = re.compile(r"^[0-9]+[a-z]*$")

# Overlay subfolders inside some rips (holo/, pip/, h/, t/) are not consumed by
# the LooseArt layer; only entries directly under the set folder are faces.
SET_FOLDER_DEPTH = 2

PNG_MAGIC = bytes([137, 80, 78, 71, 13, 10, 26, 10])


def png_size(blob):
    """(width, height) from a PNG's IHDR, without decoding the pixels."""
    if blob[:8] != PNG_MAGIC:
        return None
    return struct.unpack(">II", blob[16:24])


def load_carddata():
    """SetKey -> {slot filename}, and lowercased set key -> SetKey."""
    slots = {}
    folder_to_key = {}
    for name in sorted(os.listdir(CARDDATA)):
        if not name.endswith(".json"):
            continue
        with io.open(os.path.join(CARDDATA, name), encoding="utf-8") as fh:
            data = json.load(fh)
        key = data["set"]
        folder_to_key[key.lower()] = key
        found = set()
        for arch in data.get("archetypes", []):
            attrs = {a["n"]: a["v"] for a in arch.get("attrs", [])}
            override = attrs.get(10020, {}).get("s")
            if override is not None:
                if FACE_SUFFIX.match(override):
                    found.add("%s_%s.png" % (key, override))
                continue                     # a product image, not a card face
            num = attrs.get(200780, {}).get("i")
            if isinstance(num, int):
                found.add("%s_%03d.png" % (key, num))
        slots[key] = found
    return slots, folder_to_key


def index_rips(folder_to_key, wanted_keys):
    """SetKey -> {slot filename: [(zip path, entry)]} for in-scope card faces."""
    index = collections.defaultdict(lambda: collections.defaultdict(list))
    unmapped = collections.Counter()
    undersized = []
    zips = sorted(
        os.path.join(ZIP_DIR, n)
        for n in os.listdir(ZIP_DIR)
        if n.lower().endswith(".zip")
    )
    for path in zips:
        try:
            zf = zipfile.ZipFile(path)
        except Exception as exc:             # a bad download must not take the
            print("  unreadable %s (%s)" % (os.path.basename(path), exc))
            continue                         # whole run down
        for entry in zf.namelist():
            if entry.endswith("/") or not entry.lower().endswith(".png"):
                continue
            parts = entry.split("/")
            if len(parts) != SET_FOLDER_DEPTH:
                continue                     # holo/, pip/, h/, t/ overlays
            key = folder_to_key.get(parts[0])
            if key is None:
                unmapped[parts[0]] += 1
                continue
            if key not in wanted_keys:
                continue
            stem = os.path.splitext(parts[1])[0]
            if not FACE_SUFFIX.match(stem):
                continue
            if png_size(zf.read(entry)) != (1024, 1024):
                # bw5/111.png is a 512x512 dark-background preview, not a card
                # face. Installing it would be a downgrade, so leave the slot.
                undersized.append("%s_%s.png" % (key, stem))
                continue
            index[key]["%s_%s.png" % (key, stem)].append((path, entry))
    return index, len(zips), unmapped, undersized


def pick(sources):
    """One source per slot. Byte-identical duplicate zips collapse silently.

    Primal Clash and Dragon Vault were each downloaded twice. Identical copies
    are fine; two genuinely different images under one name are a conflict this
    script refuses to guess at.
    """
    if len(sources) == 1:
        return sources[0], None
    digests = {}
    for path, entry in sources:
        with zipfile.ZipFile(path) as zf:
            digest = hashlib.sha1(zf.read(entry)).hexdigest()
        digests.setdefault(digest, (path, entry))
    if len(digests) == 1:
        return sources[0], None
    return None, sorted(os.path.basename(p) for p, _ in sources)


def main(argv):
    apply_changes = "--apply" in argv
    keys = list(SCOPE)
    for arg in argv:
        if arg.startswith("--sets="):
            keys = arg.split("=", 1)[1].split(",")

    slots, folder_to_key = load_carddata()
    missing_carddata = [k for k in keys if k not in slots]
    if missing_carddata:
        sys.exit("no carddata for: %s" % ", ".join(missing_carddata))

    have = set(os.listdir(LOOSE_ART)) if os.path.isdir(LOOSE_ART) else set()
    index, nzips, unmapped, undersized = index_rips(folder_to_key, set(keys))

    plan = []                                # (target, source, kind)
    skipped = []                             # (target, reason)
    per_set = collections.defaultdict(collections.Counter)

    for key in keys:
        for target in sorted(index.get(key, {})):
            source, clash = pick(index[key][target])
            if source is None:
                skipped.append((target, "differs between %s" % " and ".join(clash)))
                per_set[key]["conflict"] += 1
                continue
            if target in have:
                plan.append((target, source, "upgrade"))
                per_set[key]["upgrade"] += 1
            elif target in slots[key]:
                plan.append((target, source, "install"))
                per_set[key]["install"] += 1
            else:
                skipped.append((target, "no carddata slot and no LooseArt file"))
                per_set[key]["unclaimed"] += 1

    for target in undersized:
        skipped.append((target, "rip texture is not 1024x1024"))
        per_set[target.rsplit("_", 1)[0]]["undersized"] += 1

    print("%d zips scanned, %d sets in scope" % (nzips, len(keys)))
    print("%d to upgrade, %d to install, %d skipped"
          % (sum(1 for _, _, k in plan if k == "upgrade"),
             sum(1 for _, _, k in plan if k == "install"),
             len(skipped)))
    print("\n%-14s %8s %8s %10s %9s %10s"
          % ("set", "upgrade", "install", "unclaimed", "conflict", "undersized"))
    for key in keys:
        c = per_set[key]
        print("%-14s %8d %8d %10d %9d %10d"
              % (key, c["upgrade"], c["install"], c["unclaimed"], c["conflict"],
                 c["undersized"]))
    if skipped:
        print("\nskipped:")
        for target, reason in sorted(skipped):
            print("  %-24s %s" % (target, reason))

    if not apply_changes:
        print("\n(dry run - pass --apply to write %d files)" % len(plan))
        return

    written = collections.Counter()
    for target, source, _kind in plan:
        path, entry = source
        out = os.path.join(LOOSE_ART, target)
        tmp = out + ".part"
        with zipfile.ZipFile(path) as zf:
            blob = zf.read(entry)
        with open(tmp, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, out)                 # never a half-written card face
        written[target.rsplit("_", 1)[0]] += 1
    print("\nwrote %d files into %s" % (sum(written.values()), LOOSE_ART))


if __name__ == "__main__":
    main(sys.argv[1:])
