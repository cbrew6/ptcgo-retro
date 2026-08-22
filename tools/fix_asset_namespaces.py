"""
Re-files product art under the asset names the client actually requests.

The bug this fixes: attribute 10020 is an asset-name *override*, and when its
value contains a "/" it is already absolute - "packs/BW1BlackWhite" names the
`packs` bundle, not an asset inside the card's own set. The manifest was built
by gluing the set key onto the front of it regardless, producing

    BW1_packs_BW1BlackWhite.png     what we wrote
    packs_BW1BlackWhite.png         what the client asks for

so every pack, deck box and league bundle image sat on disk under a name
nothing would ever request. Same story for the pcdBoxes namespace, which was
written as NoSet_* and <SET>_<deckname>.

Ground truth here is `bundle_index.json`: the client gates every art request
behind DoesAssetExistInManifest(), which is a lookup over exactly those asset
lists. A name absent from the index is never requested, so the index is the
complete set of names worth writing - no guessing required.

Matching is on the leaf name only, case-insensitively (LooseArt sits on a
case-insensitive filesystem, and the index is lowercased while the client asks
in mixed case). A leaf claimed by two namespaces is reported, never guessed.

Existing files are copied, not moved: the old name is harmless dead weight, and
keeping it means a re-run is idempotent and nothing is lost if a mapping here
turns out to be wrong.

Usage:
    python tools/fix_asset_namespaces.py [--apply]
"""

import collections
import json
import os
import re
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(HERE, "bundle_index.json")
LOOSE_ART = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)

# Namespaces that live in their own bundle rather than under a set. These are
# the ones attribute 10020 can point at absolutely.
PRODUCT_NAMESPACES = ("packs", "pcdBoxes", "deckBoxes", "deckboxFlats",
                      "cardSleeves", "coins", "Logos", "currency")

# ...but only these two are ever written. The client's log requests pcdBoxes 34
# times and packs 3 times, and deckboxFlats / deckBoxes / cardSleeves not once.
# That matters beyond mere caution: every product image we hold is an *angled
# box render*, which is what pcdBoxes wants, whereas deckboxFlats wants the flat
# UV wrap the game pastes onto its 3D box model. The two share leaf names, so a
# name found in both is not a coin toss - writing our render as a flat would be
# a visibly wrong texture. Anything else stays unwritten until a real request
# for it is observed.
WRITABLE = ("packs", "pcdBoxes")

# A card face: <SET>_<number>.png. Never touched here.
CARD_RE = re.compile(r"^.+_\d+[a-z]*\.png$", re.I)


def index_namespaces():
    """namespace -> {lowercased asset name: canonical asset name}."""
    with open(INDEX, encoding="utf-8") as fh:
        bundles = json.load(fh)
    out = collections.defaultdict(dict)
    for bundle, assets in bundles.items():
        base = bundle.split("_CR")[0]
        if base not in PRODUCT_NAMESPACES:
            continue
        for asset in assets:
            leaf = asset.split("/")[-1]
            out[base].setdefault(leaf.lower(), leaf)
    return out


def candidate_leaves(stem):
    """Plausible asset names inside a mis-namespaced filename.

    "BW1_packs_BW1BlackWhite" could be leaf "BW1BlackWhite" (set glued on
    front of an absolute name) or "packs_BW1BlackWhite"; "XY6_aurorablastxy6deck"
    could be "aurorablastxy6deck". Trying progressively shorter tails covers all
    of them, and the index decides which is real.
    """
    parts = stem.split("_")
    return [ "_".join(parts[i:]) for i in range(len(parts)) ]


def main(argv):
    apply_changes = "--apply" in argv
    ns = index_namespaces()
    have = os.listdir(LOOSE_ART)
    have_lower = {f.lower() for f in have}

    plans, ambiguous, unwritable, already = [], [], [], 0
    for fn in sorted(have):
        if not fn.lower().endswith(".png") or CARD_RE.match(fn):
            continue
        if "_wp_" in fn:
            continue
        stem = fn[:-4]
        # Already correctly namespaced? Leave it.
        if any(stem.lower().startswith(n.lower() + "_") for n in ns):
            already += 1
            continue
        hits = []
        for leaf in candidate_leaves(stem):
            for namespace, names in ns.items():
                canon = names.get(leaf.lower())
                if canon is not None:
                    hits.append((namespace, canon))
        # Dedupe on the produced filename; the same leaf found twice is one plan.
        hits = list(dict.fromkeys(hits))
        if not hits:
            continue
        writable = [h for h in hits if h[0] in WRITABLE]
        if len(writable) > 1:
            # Genuinely undecidable: two namespaces we would both write to.
            ambiguous.append((fn, writable))
            continue
        if not writable:
            unwritable.append((fn, hits))
            continue
        hits = writable
        namespace, canon = hits[0]
        target = "%s_%s.png" % (namespace, canon)
        if target.lower() in have_lower:
            already += 1
            continue
        plans.append((fn, target))

    by_ns = collections.Counter(t.split("_", 1)[0] for _, t in plans)
    print("%d files in LooseArt" % len(have))
    print("  %d would be re-filed" % len(plans))
    print("  %d already correctly named or already present" % already)
    print("  %d ambiguous between writable namespaces (skipped)" % len(ambiguous))
    print("  %d matched only non-written namespaces (skipped)" % len(unwritable))
    for fn, hits in ambiguous[:8]:
        print("      ambiguous: %s -> %s" % (fn, hits))
    print("\nby namespace: %s" % dict(by_ns))
    for src, dst in plans[:10]:
        print("  %-44s -> %s" % (src, dst))
    if len(plans) > 10:
        print("  ... and %d more" % (len(plans) - 10))

    if not apply_changes:
        print("\n(dry run - pass --apply)")
        return

    written = 0
    for src, dst in plans:
        out = os.path.join(LOOSE_ART, dst)
        tmp = out + ".part"
        shutil.copyfile(os.path.join(LOOSE_ART, src), tmp)
        os.replace(tmp, out)
        written += 1
    print("\nre-filed %d product images" % written)


if __name__ == "__main__":
    main(sys.argv[1:])
