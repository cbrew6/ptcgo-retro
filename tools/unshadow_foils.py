"""
Stops our neutral foil masks from hiding the real ones.

LooseArt is an override layer: the client looks there BEFORE its asset bundles.
When no real foil masks existed we wrote fully transparent placeholders for
every card, because with no mask bound at all the shader samples stale
reflection state and smears a sheen across the card - a blank mask is the
lesser evil.

Now that donated bundles supply genuine hand-authored masks, those placeholders
are actively harmful: they win the lookup and render the card flat. This moves
aside exactly the placeholders a bundle can now serve, and leaves every other
one in place - a set with no donated foils still needs its blank, or the smear
comes back.

Nothing is deleted. Files are moved to a sibling directory so a bad call here
is one move command away from being undone.

Usage:
    python tools/unshadow_foils.py [--apply]
"""

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
SHADOWED = os.path.join(os.path.dirname(HERE), "_looseart_shadowed_foils")

# en_US_COL_wp_pcd_Foil2_CR116_2 -> COL_wp_pcd_Foil2
# The CR token is a content release, not a version, so it is stripped along
# with the trailing build number to get the namespace the client requests.
BUNDLE_RE = re.compile(r"^(?:en_US_)?(.+?)(?:_CRR?\d+)?(?:_\d+)?$")


def bundle_namespaces():
    """namespace -> set of asset names the bundles can serve."""
    with open(INDEX, encoding="utf-8") as fh:
        bundles = json.load(fh)
    served = {}
    for name, assets in bundles.items():
        m = BUNDLE_RE.match(name)
        if not m:
            continue
        served.setdefault(m.group(1), set()).update(assets)
    return served


def main(argv):
    apply_changes = "--apply" in argv
    served = bundle_namespaces()
    moves = []
    for fn in sorted(os.listdir(LOOSE_ART)):
        # Only the mask layer. Card faces in LooseArt are ours and stay.
        if "_wp_" not in fn or not fn.endswith(".png"):
            continue
        stem = fn[:-4]
        # Split into the longest namespace that a bundle actually serves.
        for cut in range(len(stem) - 1, 0, -1):
            if stem[cut] != "_":
                continue
            namespace, asset = stem[:cut], stem[cut + 1:]
            if asset in served.get(namespace, ()):
                moves.append((fn, namespace))
                break

    by_ns = {}
    for _fn, ns in moves:
        by_ns[ns] = by_ns.get(ns, 0) + 1
    print("%d mask files in LooseArt are now served by a bundle" % len(moves))
    for ns, count in sorted(by_ns.items())[:12]:
        print("   %-34s %d" % (ns, count))
    if len(by_ns) > 12:
        print("   ... and %d more namespaces" % (len(by_ns) - 12))

    if not apply_changes:
        print("\n(dry run - pass --apply to move them aside)")
        return

    os.makedirs(SHADOWED, exist_ok=True)
    for fn, _ns in moves:
        shutil.move(os.path.join(LOOSE_ART, fn), os.path.join(SHADOWED, fn))
    print("\nmoved %d files to %s" % (len(moves), SHADOWED))


if __name__ == "__main__":
    main(sys.argv[1:])
