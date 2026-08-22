"""
Installs card art from the game's own ripped textures into LooseArt/.

These rips are organised as "<setcode>/<collectornumber>.png" - exactly the two
things the client identifies a card by - so a card's art is located without any
name matching at all. That matters: the one previous attempt to fill gaps by
matching card *names* put Crimson Invasion's Alolan Raichu (31/111, Psychic)
into a Trainer Kit slot that wanted a different card of the same name (17/30,
Quick Attack). Set plus number cannot make that mistake.

Two further guards:

  - A file is only ever written under a name the client actually asked for, or
    one that already exists in LooseArt. A generated name matching neither is
    reported and skipped, never invented.
  - By default only *missing* art is filled. Existing art is left alone and
    merely counted, so a run cannot quietly rewrite the collection. Pass
    --upgrade to also replace existing art with the authentic game texture.

Usage:
    python tools/install_card_art.py <dir-of-zips> [--apply] [--upgrade]
"""

import collections
import hashlib
import io
import json
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join(HERE, "tools", "missing_assets.txt")
LOOSE_ART = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)

# A card-set folder is bare lowercase letters and digits ("sm4", "tk10b"), or a
# "promo_xy" style promo set. Everything else in these rips is UI art (sleeves,
# coins, deck boxes) and is not card faces.
SET_RE = re.compile(r"^[a-z0-9]+$")
PROMO_RE = re.compile(r"^promo_([a-z0-9]+)$")

# Collector numbers as the client writes them: "017", "001xy", "65a".
NUM_RE = re.compile(r"^[a-z0-9]+$")


_set_keys = None


def canonical_set_keys():
    """lowercased set key -> the exact casing the client uses.

    Taken from carddata, because the client's casing is not derivable: most
    keys are all-caps but TwentiethAnn is not. Windows filesystems are
    case-insensitive, so an upper()-derived TWENTIETHANN_001.png silently
    overwrites TwentiethAnn_001.png while an `in os.listdir()` check happily
    reports the file as absent.
    """
    global _set_keys
    if _set_keys is None:
        _set_keys = {}
        card_dir = os.path.join(HERE, "carddata")
        for fn in os.listdir(card_dir):
            if not fn.endswith(".json"):
                continue
            with io.open(os.path.join(card_dir, fn), encoding="utf-8") as fh:
                key = (json.load(fh).get("set") or fn[:-5])
            _set_keys[key.lower()] = key
    return _set_keys


def set_prefix(folder):
    """Rip folder name -> the prefix the client uses, or None if not a set."""
    m = PROMO_RE.match(folder)
    guess = ("Promo_" + m.group(1).upper()) if m else (
        folder.upper() if SET_RE.match(folder) else None)
    if guess is None:
        return None
    # Prefer the client's own spelling; fall back to the guess for rip folders
    # that carddata has no set for (those get filtered out downstream anyway).
    return canonical_set_keys().get(guess.lower(), guess)


def load_wanted():
    """Filenames the client asked for and we could not supply."""
    wanted = set()
    with io.open(MANIFEST, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                wanted.add(parts[1])
    return wanted


def index_rips(zip_dir):
    """target filename -> [(zip path, entry)] for every card face in the rips."""
    index = collections.defaultdict(list)
    zips = sorted(
        os.path.join(zip_dir, n) for n in os.listdir(zip_dir) if n.lower().endswith(".zip")
    )
    for path in zips:
        try:
            zf = zipfile.ZipFile(path)
        except Exception as exc:                      # a bad download must not
            print("  skipping unreadable %s (%s)" % (os.path.basename(path), exc))
            continue                                  # take the whole run down
        for entry in zf.namelist():
            if entry.endswith("/") or not entry.lower().endswith(".png"):
                continue
            parts = entry.split("/")
            if len(parts) != 2:
                continue
            prefix = set_prefix(parts[0])
            num = os.path.splitext(parts[1])[0]
            if prefix is None or not NUM_RE.match(num):
                continue
            index["%s_%s.png" % (prefix, num)].append((path, entry))
    return index, len(zips)


def pick(sources):
    """One entry per target. Byte-identical duplicates are fine; others aren't.

    Several sets were downloaded twice ("Primal Clash" and "Primal Clash (1)").
    Identical copies collapse silently; genuinely different images under one
    name are a conflict this script refuses to guess at.
    """
    if len(sources) == 1:
        return sources[0], None
    digests = {}
    for path, entry in sources:
        digests.setdefault(
            hashlib.sha1(zipfile.ZipFile(path).read(entry)).hexdigest(), (path, entry)
        )
    if len(digests) == 1:
        return sources[0], None
    return None, [os.path.basename(p) for p, _ in sources]


def main(argv):
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        sys.exit(__doc__.strip())
    zip_dir = args[0]
    apply_changes = "--apply" in argv
    upgrade = "--upgrade" in argv

    wanted = load_wanted()
    have = set(os.listdir(LOOSE_ART)) if os.path.isdir(LOOSE_ART) else set()
    index, nzips = index_rips(zip_dir)

    fills, upgrades, conflicts, unknown = [], [], [], []
    for target in sorted(index):
        chosen, clash = pick(index[target])
        if chosen is None:
            conflicts.append((target, clash))
            continue
        if target in wanted and target not in have:
            fills.append((target, chosen))
        elif target in have:
            upgrades.append((target, chosen))
        else:
            unknown.append(target)

    print("%d zips, %d card faces indexed" % (nzips, len(index)))
    print("  %d fill gaps the client asked about" % len(fills))
    print("  %d match art already installed" % len(upgrades))
    print("  %d name a slot the client never requested (skipped)" % len(unknown))
    print("  %d conflict between zips (skipped)" % len(conflicts))
    for target, clash in conflicts[:5]:
        print("      conflict: %s in %s" % (target, clash))

    by_set = collections.Counter(t.rsplit("_", 1)[0] for t, _ in fills)
    print("\nfilling, by set:")
    for name, count in sorted(by_set.items()):
        print("  %-12s %d" % (name, count))

    todo = list(fills) + (upgrades if upgrade else [])
    if not apply_changes:
        print("\n(dry run - pass --apply to install %d files)" % len(todo))
        return

    os.makedirs(LOOSE_ART, exist_ok=True)
    written = 0
    for target, (path, entry) in todo:
        out = os.path.join(LOOSE_ART, target)
        tmp = out + ".part"
        with open(tmp, "wb") as fh:
            fh.write(zipfile.ZipFile(path).read(entry))
        os.replace(tmp, out)                          # never a half-written face
        written += 1
    print("\ninstalled %d card faces into %s" % (written, LOOSE_ART))


if __name__ == "__main__":
    main(sys.argv[1:])
