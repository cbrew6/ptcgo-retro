"""
Installs product art (packs, theme decks, blisters, bundles, tins, deck boxes)
from a sprite rip into LooseArt/.

The 240 product images the client asks for were CDN-hosted photography with no
card behind them, so nothing in a card database can supply them. A rip of the
game's own assets can.

Matching is on the last segment of the asset request, normalised to letters and
digits: the client asks for "AvatarItems/packs/AvatarCharizardPack" and the rip
calls it "avatarcharizardpack.png". A request is only filled when exactly one
file matches - an ambiguous name is reported and skipped rather than guessed
at, because a wrong pack picture is the same class of mistake as a wrong card
face.

Two habits of the rips shape the rest of this:

  * Several products ship a second image with a "1" suffix (a back or angled
    view). It normalises to a different key, so it simply never gets requested
    and the plain shot the client's single request wants is what wins.

  * The deck-box rips come in two flavours. "Deckbox Gen IV-VII" holds the
    rendered product shot - the box photographed at an angle, matching every
    other product image in this manifest. "DeckBox Text Gen IV-VII" holds the
    flat UV wrap that the game pastes onto its 3D box model. They share
    basenames, so passing both zips at once makes every deck box ambiguous.
    Pass only the render zip; the flats are not product art.

Rows whose file already exists in LooseArt are left alone. Those were placed by
an earlier, deliberate choice of source, and several basenames appear in more
than one rip with different content - re-running against a new zip must not
quietly redecide them.

Usage:
    python tools/install_product_art.py <zip> [<zip> ...] [--apply]
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


def index_zips(zip_paths):
    """normalised stem -> [(zipfile, entry)] across every source zip.

    Entries are matched on basename alone, so nested rips ("Collection General
    Gen IV-VII/Tins and Elite Boxes/xy10elitetrainerbox.png") need no special
    handling. Collisions across zips are kept, not merged, so they surface as
    ambiguities instead of an arbitrary winner.
    """
    index = {}
    for path in zip_paths:
        zf = zipfile.ZipFile(path)
        for entry in zf.namelist():
            if entry.endswith("/"):
                continue
            base = os.path.basename(entry)
            stem, ext = os.path.splitext(base)
            if ext.lower() not in (".png", ".jpg", ".jpeg"):
                continue
            index.setdefault(norm(stem), []).append((zf, entry))
    return index


def main(argv):
    zip_paths = [a for a in argv if not a.startswith("--")]
    if not zip_paths:
        sys.exit(__doc__.strip())
    apply_changes = "--apply" in argv

    index = index_zips(zip_paths)
    manifest = load_manifest()

    already, matched, ambiguous, unmatched = 0, [], [], []
    for request, filename in manifest:
        if os.path.exists(os.path.join(LOOSE_ART, filename)):
            already += 1
            continue
        # "AvatarItems/packs/AvatarCharizardPack" -> "AvatarCharizardPack"
        leaf = request.split("/")[-1]
        hits = index.get(norm(leaf))
        if not hits:
            unmatched.append(request)
        elif len(hits) > 1:
            ambiguous.append((request, hits))
        else:
            matched.append((request, filename, hits[0]))

    print("%d manifest rows: %d already in LooseArt, %d matched in the rip, "
          "%d ambiguous, %d not there"
          % (len(manifest), already, len(matched), len(ambiguous),
             len(unmatched)))
    for request, hits in ambiguous:
        print("  ambiguous: %s -> %s" % (request, [e for _, e in hits]))
    print()
    for request, filename, (_, entry) in matched:
        print("  %-46s <- %s" % (request, entry))

    if not apply_changes:
        print("\n(dry run - pass --apply to install)")
        return

    os.makedirs(LOOSE_ART, exist_ok=True)
    written = 0
    for request, filename, (zf, entry) in matched:
        out = os.path.join(LOOSE_ART, filename)
        tmp = out + ".part"
        with open(tmp, "wb") as fh:
            fh.write(zf.read(entry))
        os.replace(tmp, out)
        written += 1
    print("\ninstalled %d product images into %s" % (written, LOOSE_ART))


if __name__ == "__main__":
    main(sys.argv[1:])
