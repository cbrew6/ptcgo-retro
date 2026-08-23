"""
End-to-end check of foil-mask coverage, measured the way the CLIENT measures it.

Nothing here is inferred from a naming convention. Every request string is
rebuilt from the decompiled client, and every lookup is resolved against the
exact manifest `asset_server.py` serves, so the answer cannot drift from what
the game actually asks for.

--------------------------------------------------------------------------
The client's foil path, from `CardImageRenderer` (pie-src.dll)
--------------------------------------------------------------------------

    RefreshRequestData()                                     pie.cs:37037
        data.TryGetOne<s.A>(typeData) && TryGetOne<Q.k>(setData)
          && TryGetOne<w.B>(artData) && artData.IsFoil()
        fx           = artData.FoilEffects
        requestFoil2 = GetFoil2Path(artData.FoilMask, fx[0])
        requestFoil  = requestFoil2  if DoesAssetExistInManifest(requestFoil2)
                       else GetFoilPath(artData.FoilMask, fx[0])
        if fx.Length > 1:
            requestFoilSecondary = GetFoil2Path(mask, fx[1], secondFoil:true)

    GetFoilPath  = "{0}_{1}/{2}"        (decryptor .ki)      pie.cs:37592
    GetFoil2Path = "{0}_{1}_Foil2/{2}"  (decryptor .kJ)      pie.cs:37597
        {0} = setData.Set
        {1} = SetupFoilTexture(mask, effect, secondFoil)
        {2} = textureLookup(setData, typeData)

    SetupFoilTexture                                         pie.cs:37602
        secondFoil                  -> "wp_secondary"  (.kj)
        effect == Cracked_Ice       -> "wp_pcd"        (.kK)
        mask   == FoilMasks.Reverse -> "wp_ph"         (.kL)
        otherwise                   -> "wp_std"        (.kk)

    textureLookup                                            pie.cs:37615
        typeData.CardImage if non-empty, else
        setData.PaddedCollectorNumber  ==  collectorNumber.ToString("D3")

    w.B.IsFoil                                               pie.cs:212092
        FoilMask != None, or FoilEffects[0] != None
    w.B.FoilEffects                                          pie.cs:212045
        [attr 200610] ++ attr 200611, then sorted so SwSecret sinks last
        (w.B.sortTransparencySupported, pie.cs:232869)

Attribute ids come from IL field resolution (scratchpad/pfmap.json), not from
the decompiled source - the obfuscator collapses distinct fields onto the same
name there:

    200580  set code            200780  collector number
    200610  FoilEffects?        200611  FoilEffects[]
    200620  FoilMasks?          10020   CardImage (asset-name override)
    200550  rarity              200630  card name

--------------------------------------------------------------------------
How a request resolves
--------------------------------------------------------------------------

1. LooseArt (patched `AssetBundleImageCache.Contains`/`GetTexture`, which call
   `PtcgoLooseArt.Ensure`) is consulted FIRST. File name = request with "/"
   replaced by "_", plus .png/.jpg/.jpeg.
2. Otherwise the bundle system: `DictionaryBackedAssetRequester.assetPaths`,
   built as `assetPaths[asset.name] = descriptor` over every descriptor's
   `assets[]` - which is what `asset_server.discover_bundles()` emits.
3. Otherwise nothing: the shader gets no mask.

Every mask this repo ever wrote into LooseArt is a fully transparent 4144-byte
placeholder, so "resolves from LooseArt" and "renders flat" are the same
statement. That distinction is the whole point of this script: a placeholder
hit looks identical to a real mask hit from the outside, which is how two
rounds of "foils are fixed" survived.

Usage:
    python tools/foil_coverage.py                # summary
    python tools/foil_coverage.py --by-set       # per-set table
    python tools/foil_coverage.py --gx           # only GX archetypes
    python tools/foil_coverage.py --set SM3      # one set, card by card
    python tools/foil_coverage.py --json out.json
"""

import argparse
import collections
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

CARDDATA = os.path.join(HERE, "carddata")
GAME = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
)
LOOSE_ART = os.path.join(GAME, "LooseArt")

# Every neutral mask this repo writes is byte-identical: a 512x512 fully
# transparent PNG. Anything else in LooseArt under a _wp_ name would be real
# art, so the size check is how a real mask would announce itself.
PLACEHOLDER_SIZE = 4144

ATTR_SET = 200580
ATTR_NUMBER = 200780
ATTR_EFFECT = 200610
ATTR_EFFECTS = 200611
ATTR_MASK = 200620
ATTR_CARDIMAGE = 10020
ATTR_RARITY = 200550
ATTR_NAME = 200630

ERAS = [
    ("HGSS", ("HGSS1", "HGSS2", "HGSS3", "HGSS4", "HGSS_Energy", "Promo_HGSS",
              "COL", "TATM", "RSP", "DV", "SL")),
    ("BW", ("BW1", "BW2", "BW3", "BW4", "BW5", "BW6", "BW7", "BW8", "BW9",
            "BW10", "BW11", "BW_Energy", "Promo_BW")),
    ("XY", ("XY0", "XY1", "XY2", "XY3", "XY4", "XY5", "XY6", "XY7", "XY8",
            "XY9", "XY10", "XY11", "XY12", "XY_Energy", "Promo_XY",
            "TwentiethAnn")),
    ("SM", ("SM1", "SM2", "SM3", "SM4", "SM_Energy", "Promo_SM")),
]
ERA_OF = {s: era for era, sets in ERAS for s in sets}
ERA_ORDER = [era for era, _ in ERAS] + ["other"]


def era_of(set_code):
    return ERA_OF.get(set_code, "other")


# --------------------------------------------------------------------------
# what the client can see
# --------------------------------------------------------------------------

class Manifest(object):
    """The client's asset-name map, with the client's own comparison rules.

    `DictionaryBackedAssetRequester.assetPaths` is built with
    StringComparer.OrdinalIgnoreCase, so lookups fold case. Matching
    case-sensitively here would under-report: bundle assets are lowercase
    ("thunderous_fullcolor_deckbox") while the archetype's ImageName is not.
    """

    def __init__(self, names):
        self._names = {n.lower(): n for n in names}

    def __contains__(self, request):
        return request.lower() in self._names

    def __len__(self):
        return len(self._names)

    def __iter__(self):
        return iter(self._names.values())


def manifest_asset_names():
    """The exact asset-name set the running client is handed.

    Built by importing asset_server rather than re-deriving it: a coverage
    check that computes the manifest its own way can agree with itself and
    still disagree with the game, which is how earlier rounds of this reported
    "fixed" while nothing rendered.
    """
    import asset_server
    names = set()
    for bundle in asset_server.MANIFEST["bundles"]:
        for asset in bundle["assets"]:
            names.add(asset["name"])
    return Manifest(names)


def looseart_index():
    """lowercased request-stem -> True if the file is a neutral placeholder."""
    out = {}
    if not os.path.isdir(LOOSE_ART):
        return out
    with os.scandir(LOOSE_ART) as it:
        for entry in it:
            name = entry.name
            stem, dot, ext = name.rpartition(".")
            if not dot or ext.lower() not in ("png", "jpg", "jpeg"):
                continue
            try:
                size = entry.stat().st_size
            except OSError:
                size = -1
            # Windows is case-insensitive and so is the client's asset map.
            out[stem.lower()] = size == PLACEHOLDER_SIZE
    return out


# --------------------------------------------------------------------------
# what the client asks for
# --------------------------------------------------------------------------

def attrs_of(arch):
    return {a["n"]: a["v"] for a in arch["attrs"]}


def sval(v):
    return None if v is None else v.get("s")


def foil_effects(at):
    """w.B.FoilEffects: the single effect first, then the array, SwSecret last."""
    effects = []
    single = sval(at.get(ATTR_EFFECT))
    if single is not None:
        effects.append(single)
    arr = at.get(ATTR_EFFECTS)
    if arr and arr.get("a"):
        for item in arr["a"]:
            e = sval(item)
            if e is not None:
                effects.append(e)
    # Array.Sort with a comparator returning 1 for SwSecret and 0 otherwise.
    # Not a total order, but the only reordering it can cause is pushing a
    # SwSecret back, which a stable "SwSecret last" reproduces.
    if len(effects) > 1 and any(e == "SwSecret" for e in effects):
        effects = ([e for e in effects if e != "SwSecret"]
                   + [e for e in effects if e == "SwSecret"])
    return effects


def foil_kind(mask, effect, secondary=False):
    """CardImageRenderer.SetupFoilTexture."""
    if secondary:
        return "wp_secondary"
    if effect == "Cracked_Ice":
        return "wp_pcd"
    return "wp_ph" if mask == "Reverse" else "wp_std"


def enumerate_cards():
    """Yield one dict per archetype that the client would render as a card."""
    for fn in sorted(os.listdir(CARDDATA)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(CARDDATA, fn), encoding="utf-8") as fh:
            blob = json.load(fh)
        for arch in blob.get("archetypes", ()):
            at = attrs_of(arch)
            set_code = sval(at.get(ATTR_SET))
            if not set_code:
                continue
            number = (at.get(ATTR_NUMBER) or {}).get("i")
            override = sval(at.get(ATTR_CARDIMAGE)) or ""
            if override:
                lookup = override
            elif number is not None:
                lookup = "%03d" % number
            else:
                continue
            mask = sval(at.get(ATTR_MASK))
            effects = foil_effects(at)
            # w.B.get_IsFoil
            if mask in (None, "None"):
                is_foil = bool(effects) and effects[0] != "None"
            else:
                is_foil = True
            yield {
                "set": set_code,
                "number": number,
                "lookup": lookup,
                "name": sval(at.get(ATTR_NAME)) or "",
                "rarity": sval(at.get(ATTR_RARITY)) or "",
                "mask": mask or "None",
                "effects": effects,
                "is_foil": is_foil,
                "override": bool(override),
            }


def requests_for(card, manifest):
    """The foil requests the client will actually issue, in client order.

    Returns [(role, request)] - "foil" is the one bound to _NormalMap;
    "secondary" only exists for cards with two effects AND only when the
    manifest has it (RefreshRequestData leaves requestFoilSecondary empty
    otherwise, and nothing is requested at all).
    """
    if not card["is_foil"]:
        return []
    fx = card["effects"] or ["None"]
    kind = foil_kind(card["mask"], fx[0])
    foil2 = "%s_%s_Foil2/%s" % (card["set"], kind, card["lookup"])
    plain = "%s_%s/%s" % (card["set"], kind, card["lookup"])
    out = [("foil", foil2 if foil2 in manifest else plain)]
    if len(fx) > 1:
        sec = "%s_%s_Foil2/%s" % (card["set"],
                                  foil_kind(card["mask"], fx[1], secondary=True),
                                  card["lookup"])
        if sec in manifest:
            out.append(("secondary", sec))
    return out


def resolve(request, manifest, loose):
    stem = request.replace("/", "_").lower()
    if stem in loose:
        return "looseart_blank" if loose[stem] else "looseart_real"
    if request in manifest:
        return "bundle"
    return "missing"


# --------------------------------------------------------------------------

STATES = ("bundle", "looseart_real", "looseart_blank", "missing")


# --------------------------------------------------------------------------
# the runtime half: coverage on paper is not coverage on screen
# --------------------------------------------------------------------------

CLIENT_LOG = os.path.join(
    os.environ.get("USERPROFILE", ""), "AppData", "LocalLow",
    "The Pokémon Company International", "Pokemon Trading Card Game Online",
    "output_log.txt")

# AssetBundleImageCache is an LRU capped at 60. AddTexture() evicts before
# inserting and calls AssetRefCounter.RemoveReference, which THROWS for any
# texture it isn't counting. The loose-art helper used to insert textures
# without registering them, so once 60 entries were cached every subsequent
# AddTexture threw - and AddTexture is how a bundle-loaded texture arrives.
# The throw escapes the loading coroutine, which dies at the point it reached:
# just before setFoilMask() for a card, just before set_mainTexture() for a
# deck box or sleeve. Nothing appears in the UI to say so.
#
# This is the failure that made "foils are fixed" look true for the first
# few cards checked and false for everything after. If it ever comes back,
# it comes back silently, so check for it here rather than by eye.
REFCOUNT_THROW = re.compile(
    r"Tried to remove a Material reference to a Material that we "
    r"weren't tracking! (.*)")
CALLER = re.compile(r"^\s+at ([\w.+<>`]+)")


def scan_client_log(path=None):
    """-> (occurrences, Counter of callers, Counter of evicted asset names)."""
    path = path or CLIENT_LOG
    if not os.path.isfile(path):
        return None, None, None
    callers, names = collections.Counter(), collections.Counter()
    total = 0
    pending = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            m = REFCOUNT_THROW.search(line)
            if m:
                total += 1
                names[m.group(1).strip()] += 1
                pending = 6              # frames to attribute to this throw
                continue
            if pending:
                pending -= 1
                c = CALLER.match(line)
                if c and "AssetRefCounter" not in c.group(1) \
                        and "AddTexture" not in c.group(1):
                    callers[c.group(1)] += 1
                    pending = 0
    return total, callers, names


def analyse():
    manifest = manifest_asset_names()
    loose = looseart_index()
    rows = []
    for card in enumerate_cards():
        card["requests"] = [
            {"role": role, "request": req,
             "state": resolve(req, manifest, loose)}
            for role, req in requests_for(card, manifest)
        ]
        face = "%s/%s" % (card["set"], card["lookup"])
        card["face"] = {"request": face,
                        "state": resolve(face, manifest, loose)}
        primary = next((r for r in card["requests"] if r["role"] == "foil"),
                       None)
        card["state"] = primary["state"] if primary else "not_foil"
        rows.append(card)
    return rows, manifest, loose


def is_gx(card):
    return "GX" in card["rarity"] or card["name"].endswith("GX")


def tally(rows, key):
    out = {}
    for card in rows:
        out.setdefault(key(card), collections.Counter())[card["state"]] += 1
    return out


def print_table(title, counters, width=24):
    print("\n%s" % title)
    print("  %-*s %7s %7s %7s %7s %7s" %
          (width, "", "foil", "bundle", "real", "BLANK", "gone"))
    for name, c in sorted(counters.items()):
        total = sum(c.values())
        flat = total - c["bundle"] - c["looseart_real"]
        print("  %-*s %7d %7d %7d %7d %7d%s" %
              (width, name, total, c["bundle"], c["looseart_real"],
               c["looseart_blank"], c["missing"],
               "" if not flat else "   %5.1f%% flat" % (100.0 * flat / total)))


def main(argv=None):
    ap = argparse.ArgumentParser(description="foil coverage, client's view")
    ap.add_argument("--by-set", action="store_true", help="per-set breakdown")
    ap.add_argument("--gx", action="store_true", help="restrict to GX cards")
    ap.add_argument("--set", help="dump one set card by card")
    ap.add_argument("--json", help="write the full per-card result here")
    ap.add_argument("--list-flat", action="store_true",
                    help="print every foil request that renders flat")
    ap.add_argument("--faces", action="store_true",
                    help="also report card-face (non-foil) coverage")
    ap.add_argument("--log", nargs="?", const=CLIENT_LOG, default=None,
                    help="scan the client log for the ref-counter throw that "
                         "silently kills texture loads")
    args = ap.parse_args(argv)

    if args.log:
        total, callers, names = scan_client_log(args.log)
        if total is None:
            print("no client log at %s" % args.log)
        elif not total:
            print("client log clean: no AssetRefCounter throws in %s"
                  % args.log)
        else:
            print("!! %d AssetRefCounter throws in the client log."
                  % total)
            print("   Every one of these killed a texture-loading coroutine "
                  "before it bound its texture.")
            print("   loaders that died:")
            for c, n in callers.most_common():
                print("     %-70s %d" % (c, n))
            print("   textures whose eviction threw (these are LooseArt "
                  "insertions):")
            for c, n in names.most_common(10):
                print("     %-40s %d" % (c, n))
            print("   Fix: tools/looseart/PtcgoLooseArt.cs must call "
                  "AssetRefCounter.AddReference\n"
                  "        for every texture it puts in the cache; rebuild "
                  "with tools/looseart/build.cmd.")
        print()

    rows, manifest, loose = analyse()
    if args.gx:
        rows = [r for r in rows if is_gx(r)]

    foils = [r for r in rows if r["is_foil"]]
    counts = collections.Counter(r["state"] for r in foils)
    total = len(foils)

    print("archetypes:            %d" % len(rows))
    print("foil archetypes:       %d" % total)
    print("manifest asset names:  %d" % len(manifest))
    print("LooseArt files:        %d (%d neutral placeholders)"
          % (len(loose), sum(1 for v in loose.values() if v)))
    print()
    if total:
        for state in STATES:
            print("  %-16s %6d  %5.1f%%"
                  % (state, counts[state], 100.0 * counts[state] / total))
        good = counts["bundle"] + counts["looseart_real"]
        print("\n  REAL FOIL:       %6d  %5.1f%%" % (good, 100.0 * good / total))
        print("  RENDERS FLAT:    %6d  %5.1f%%"
              % (total - good, 100.0 * (total - good) / total))

    print_table("by era", tally(foils, lambda c: "%d %s"
                                % (ERA_ORDER.index(era_of(c["set"])),
                                   era_of(c["set"]))))

    gx = [r for r in foils if is_gx(r)]
    if gx and not args.gx:
        gxc = collections.Counter(r["state"] for r in gx)
        print("\nGX archetypes (foil): %d" % len(gx))
        for state in STATES:
            print("  %-16s %6d  %5.1f%%"
                  % (state, gxc[state], 100.0 * gxc[state] / len(gx)))

    if args.by_set:
        print_table("by set", tally(foils, lambda c: c["set"]))

    if args.faces:
        facec = collections.Counter(r["face"]["state"] for r in rows)
        print("\ncard faces (all %d archetypes):" % len(rows))
        for state in STATES:
            print("  %-16s %6d" % (state, facec[state]))

    if args.set:
        print("\n%s, card by card" % args.set)
        for card in sorted((r for r in rows if r["set"] == args.set),
                           key=lambda r: (r["number"] is None, r["number"])):
            reqs = ", ".join("%s=%s->%s" % (r["role"], r["request"], r["state"])
                             for r in card["requests"]) or "(not foil)"
            print("  %-4s %-26s %-16s mask=%-8s fx=%-22s %s"
                  % (card["number"], card["name"][:26], card["rarity"][:16],
                     card["mask"], ",".join(card["effects"])[:22], reqs))

    if args.list_flat:
        print("\nfoil requests that render flat:")
        seen = set()
        for card in foils:
            for r in card["requests"]:
                if r["role"] != "foil":
                    continue
                if r["state"] in ("bundle", "looseart_real"):
                    continue
                if r["request"] in seen:
                    continue
                seen.add(r["request"])
                print("  %-42s %-15s %s" % (r["request"], r["state"],
                                            card["name"]))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rows, fh, indent=1)
        print("\nwrote %s" % args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
