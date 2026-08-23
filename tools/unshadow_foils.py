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

Nothing is deleted. Files are moved to a sibling directory, and `--restore`
puts them all back, so a bad call here is one command away from being undone.

--------------------------------------------------------------------------
Why this is request-driven now
--------------------------------------------------------------------------

The previous two versions worked backwards: take a LooseArt filename, guess
which part of it is the "namespace", and ask whether a bundle with a matching
name exists. That guess is where both earlier rounds went wrong, because a
bundle's name ends in a content-release token of at least four shapes
(_CR105, _CRR65p1, _CRSM4, a bare _SM3, or nothing at all) and the request
namespace is not recoverable from it by pattern-matching.

It is recoverable from the other end, exactly and with no guessing. The client
builds its foil request in CardImageRenderer from card data, and it picks
between two forms based on DoesAssetExistInManifest - so `foil_coverage` can
compute the one string the client will actually ask for, per card. This tool
now enumerates those strings, asks the manifest (as asset_server serves it)
which of them a bundle can answer, and moves aside only the placeholders
sitting on top of those. No namespace is ever derived.

The second thing that changed: only files that are byte-for-byte the neutral
placeholder are moved. A real mask dropped into LooseArt by hand now stays
put no matter what a bundle claims.

Usage:
    python tools/unshadow_foils.py             # dry run
    python tools/unshadow_foils.py --apply
    python tools/unshadow_foils.py --unused    # also report never-requested blanks
    python tools/unshadow_foils.py --restore   # put everything back
"""

import os
import shutil
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tools"))

import foil_coverage as fc                                    # noqa: E402

LOOSE_ART = fc.LOOSE_ART
SHADOWED = os.path.join(os.path.dirname(HERE), "_looseart_shadowed_foils")


def placeholders():
    """LooseArt mask files that are the neutral placeholder -> full path."""
    out = {}
    if not os.path.isdir(LOOSE_ART):
        return out
    with os.scandir(LOOSE_ART) as it:
        for entry in it:
            if "_wp_" not in entry.name or not entry.name.endswith(".png"):
                continue
            try:
                if entry.stat().st_size == fc.PLACEHOLDER_SIZE:
                    out[entry.name.lower()] = entry.path
            except OSError:
                pass
    return out


def analyse():
    """-> (shadowing, unused) lists of (filename, request)."""
    manifest = fc.manifest_asset_names()
    blanks = placeholders()
    wanted = set()
    shadowing = []
    for card in fc.enumerate_cards():
        for _role, request in fc.requests_for(card, manifest):
            stem = request.replace("/", "_").lower() + ".png"
            wanted.add(stem)
            if stem in blanks and request in manifest:
                shadowing.append((os.path.basename(blanks[stem]), request))
    seen = set()
    shadowing = [x for x in shadowing
                 if not (x[0] in seen or seen.add(x[0]))]
    unused = sorted(os.path.basename(p) for s, p in blanks.items()
                    if s not in wanted)
    return shadowing, unused


def restore():
    if not os.path.isdir(SHADOWED):
        print("nothing to restore: %s does not exist" % SHADOWED)
        return
    names = os.listdir(SHADOWED)
    for fn in names:
        shutil.move(os.path.join(SHADOWED, fn), os.path.join(LOOSE_ART, fn))
    print("restored %d files to LooseArt" % len(names))


def main(argv):
    if "--restore" in argv:
        restore()
        return
    shadowing, unused = analyse()

    by_ns = {}
    for _fn, request in shadowing:
        ns = request.split("/")[0]
        by_ns[ns] = by_ns.get(ns, 0) + 1
    print("%d neutral placeholders are shadowing a mask a bundle can serve"
          % len(shadowing))
    for ns, count in sorted(by_ns.items())[:12]:
        print("   %-34s %d" % (ns, count))
    if len(by_ns) > 12:
        print("   ... and %d more request namespaces" % (len(by_ns) - 12))

    if "--unused" in argv:
        print("\n%d placeholders are never requested by any archetype "
              "(dead weight, safe to leave)" % len(unused))
        pref = {}
        for fn in unused:
            key = fn.rsplit("_", 1)[0]
            pref[key] = pref.get(key, 0) + 1
        for key, count in sorted(pref.items(), key=lambda x: -x[1])[:12]:
            print("   %-34s %d" % (key, count))

    if "--apply" not in argv:
        print("\n(dry run - pass --apply to move them aside)")
        return

    os.makedirs(SHADOWED, exist_ok=True)
    moved = 0
    for fn, _request in shadowing:
        src = os.path.join(LOOSE_ART, fn)
        if os.path.exists(src):
            shutil.move(src, os.path.join(SHADOWED, fn))
            moved += 1
    print("\nmoved %d files to %s" % (moved, SHADOWED))


if __name__ == "__main__":
    main(sys.argv[1:])
