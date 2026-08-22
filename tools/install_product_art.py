"""
Installs product art (packs, theme decks, blisters, bundles, tins) from a
sprite rip into LooseArt/.

The 240 product images the client asks for were CDN-hosted photography with no
card behind them, so nothing in a card database can supply them. A rip of the
game's own assets can.

Matching is on the last segment of the asset request, normalised to letters and
digits: the client asks for "AvatarItems/packs/AvatarCharizardPack" and the rip
calls it "avatarcharizardpack.png". A request is only filled when exactly one
file matches - an ambiguous name is reported and skipped rather than guessed
at, because a wrong pack picture is the same class of mistake as a wrong card
face.

Usage:
    python tools/install_product_art.py <zip> [--apply]
"""

import io
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


def norm(s):
    return re.sub(r"[^a-z0-9]", "", s.lower())


def load_manifest():
    """(request, filename) for every asset we cannot currently supply."""
    out = []
    with io.open(MANIFEST, encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) >= 2:
                out.append((parts[0], parts[1]))
    return out


def index_zip(zf):
    """normalised stem -> [zip entry], skipping the "1" alternate shots.

    Several products ship a second image with a "1" suffix (a back or angled
    view). The plain name is the one the client's single request wants.
    """
    index = {}
    for entry in zf.namelist():
        if entry.endswith("/"):
            continue
        base = os.path.basename(entry)
        stem, ext = os.path.splitext(base)
        if ext.lower() not in (".png", ".jpg", ".jpeg"):
            continue
        index.setdefault(norm(stem), []).append(entry)
    return index


def main(argv):
    if not argv:
        sys.exit(__doc__.strip())
    zip_path = argv[0]
    apply_changes = "--apply" in argv

    zf = zipfile.ZipFile(zip_path)
    index = index_zip(zf)
    manifest = load_manifest()

    matched, ambiguous, unmatched = [], [], []
    for request, filename in manifest:
        # "AvatarItems/packs/AvatarCharizardPack" -> "AvatarCharizardPack"
        leaf = request.split("/")[-1]
        hits = index.get(norm(leaf))
        if not hits:
            unmatched.append(request)
        elif len(hits) > 1:
            ambiguous.append((request, hits))
        else:
            matched.append((request, filename, hits[0]))

    print("%d missing assets, %d matched in the rip, %d ambiguous, %d not there"
          % (len(manifest), len(matched), len(ambiguous), len(unmatched)))
    for request, hits in ambiguous[:5]:
        print("  ambiguous: %s -> %s" % (request, [os.path.basename(h) for h in hits]))
    print()
    for request, filename, entry in matched[:8]:
        print("  %-46s <- %s" % (request, os.path.basename(entry)))
    if len(matched) > 8:
        print("  ... and %d more" % (len(matched) - 8))

    if not apply_changes:
        print("\n(dry run - pass --apply to install)")
        return

    os.makedirs(LOOSE_ART, exist_ok=True)
    written = 0
    for request, filename, entry in matched:
        out = os.path.join(LOOSE_ART, filename)
        tmp = out + ".part"
        with open(tmp, "wb") as fh:
            fh.write(zf.read(entry))
        os.replace(tmp, out)
        written += 1
    print("\ninstalled %d product images into %s" % (written, LOOSE_ART))


if __name__ == "__main__":
    main(sys.argv[1:])
