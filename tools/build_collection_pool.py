"""
Builds browsable card records for the sets whose card data never arrived.

SM5 (Ultra Prism) through SWSH10 (Astral Radiance) is 3,855 cards the servers
were still streaming when they went away. The ART for almost all of them is on
disk - recovered from donated caches - and so is a great deal of the text. What
is missing is seven numbers per card: HP, weakness, resistance, retreat cost,
stage, evolves-from and rarity.

Those seven are what stop a card being PLAYED. They are not what stops it being
SEEN, and conflating the two has kept 3,756 card faces in a folder doing
nothing. The collection does not draw a card from its attributes; it draws the
card's texture, which already has the HP and the weakness printed on it. Every
collection code path that reads one of the seven defaults it:

    SortHitpower / SortRetreatType / SortWeaknessValue / SortResistanceValue
                                    x.GetAttribute(...).get_Value() ?? 0
    SortByRarity / SortByEvolution / Evolution() / Rarity() / PokemonFamily()
                                    TryGetOne<T>()?.A ?? <default>
    RetreatCost()                   returns 0 unless HasAttribute
    IsLegend()                      guarded TryGetOne(out ...)

and CardImageRenderer never asks for any of them - it wants Set, Type,
EnergyType, EnergyProvided, CardImage, IsFoil, FoilMask, FoilEffects, Version.

Exactly ONE attribute here is load-bearing, and it is not one of the seven:
**200570 PokemonTypes**. `getDefaultPerCardType` does `get_EnergyType().Value`
with no HasValue check, and that throw unwinds RefreshRequestData before
getImageRequestString, so the card never requests its texture and renders blank
for the whole session. We can supply it, because the bundle names carry it.

WHAT THE BUNDLE NAMES GIVE US, all of it verifiable rather than inferred:

    SM5_fire_CRSM5     -> ["SM5/018" .. "SM5/027"]   cards 18-27 are Fire
    SM5_trainer_CRSM5  -> ["SM5/118", "SM5/121_toolpip", ...]
                                                     118 is a Trainer,
                                                     121 is a Pokemon Tool
    SM5_trainer_CR50   -> ["SM5/136_energyicon"]     136 is a Special Energy
    SM5_wp_std_Foil2   -> ["SM5_wp_std_Foil2/009"]   9 is holo
    SM5_wp_ph_Foil2    -> ...                        reverse holo
    SM5_wp_secondary   -> ...                        secret rare

So: set, collector number, Pokemon type, supertype, tool-ness and foil
treatment, for every card, from files we own.

WHAT WE DELIBERATELY DO NOT CLAIM: the card's NAME. The donated AttributeDB has
real names and real attack text for 5,578 of these archetypes, but nothing
locally joins a name to a collector number. Cache-key order is not collector
order - measured, 50% inversions, which is chance. So each card is labelled by
what we can prove, "UPR 018", and the face shows the rest. Guessing names by
alphabetising a type block would be wrong often enough to poison search, and a
card that misstates itself is worse than one that says less.

SAFETY. These cards have no HP, so they must never reach a match. Two
independent guards, neither relying on anyone remembering:

  1. They are written to `carddata_browse/`, not `carddata/`. server.card_db()
     builds the ENGINE's database from `carddata/` alone, so the engine does
     not know these cards exist and cannot be handed one.
  2. server.build_format_legality() marks them legal in NO format, so the
     client's own deck validation refuses them before a deck can be queued.

Ids are UUIDv5 over a namespace of our own: stable across reruns, so a rebuild
does not invalidate anything, and unable to collide with the v4 ids the real
sets use.

Usage:
    python tools/build_collection_pool.py
    python tools/build_collection_pool.py --apply
    python tools/build_collection_pool.py --apply --only SM5
"""

import argparse
import collections
import json
import os
import re
import sys
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

CARD_DIR = os.path.join(HERE, "carddata")
OUT_DIR = os.path.join(HERE, "carddata_browse")
INDEX = os.path.join(HERE, "bundle_index.json")
SETDATA = os.path.join(HERE, "donor", "setdata.json")

# Ours, fixed, arbitrary. What matters is that it never changes and that the
# result is a v5 uuid, which cannot collide with the real sets' v4 ids.
NAMESPACE = uuid.UUID("f2b7c1de-4a6e-5d38-9a41-0c5e7b3d9a20")

T_ARRAY, T_STRING, T_BOOL, T_INT, T_GUID, T_TEXT = 1, 3, 4, 5, 7, 8

ATTR_ARCHETYPE = 10000
ATTR_ASSET_NAME = 10020
ATTR_UNKNOWN_BOOL = 10090          # bool?, present-and-empty on every real card
ATTR_NAME_KEY = 10140
ATTR_SET_CARD_ID = 10190
ATTR_CARD_TYPE = 200300
ATTR_TRAINER_TYPE = 200270
ATTR_TYPES = 200570                # the load-bearing one
ATTR_SET = 200580
ATTR_FOIL_EFFECT = 200610
ATTR_FOIL_MASK = 200620
ATTR_NUMBER = 200780
ATTR_PRINT_GROUP = 201710          # an ArchetypeID; real cards point at themselves
ATTR_CARD_CLASS = 202080           # int, 201 on 8,811 of 8,874 real cards

# Bundle-name segments that name a Pokemon's type. Anything else is a Trainer,
# an Energy, or a namespace we do not synthesize from.
TYPES = {
    "grass": "Grass", "fire": "Fire", "water": "Water",
    "lightning": "Lightning", "psychic": "Psychic", "fighting": "Fighting",
    "darkness": "Darkness", "metal": "Metal", "fairy": "Fairy",
    "dragon": "Dragon", "colorless": "Colorless",
}

# Foil enum members, taken from values that already appear in carddata rather
# than from the enum definition - a value the client cannot parse throws.
FOIL_BY_KIND = {
    "std": ("Holo", "Cosmos"),
    "ph": ("Reverse", "Rainbow"),
    "secondary": ("Holo", "Rainbow"),
    "pcd": ("Reverse", "Rainbow"),
}

LEAF = re.compile(r"^(\d{3})([a-z0-9]*)$")
SUFFIX = re.compile(r"^(\d{3}[a-z0-9]*)_(toolpip|energyicon|energypip)$")


def obj_int(v):
    return {"i": v, "t": T_INT} if v else {"t": T_INT}


def obj_str(v):
    return {"s": v, "t": T_STRING}


def obj_text(v):
    return {"s": v, "t": T_TEXT}


def obj_guid(lo, hi):
    return {"g": [lo, hi], "t": T_GUID}


def guid_halves(u):
    """The lo/hi protobuf halves carddata stores, matching uuid_to_guid_str."""
    import server
    # uuid_to_guid_str builds the first three groups out of `hi` and the last
    # eight bytes out of `lo`, both big-endian, so the halves are simply the
    # two halves of the canonical hex - not a little-endian split.
    raw = u.bytes
    hi = int.from_bytes(raw[:8], "big")
    lo = int.from_bytes(raw[8:], "big")
    # Round-trip through the server's own converter so the client sees exactly
    # the id we think it does. A mismatch here is invisible until a collection
    # count refers to a card nothing can find.
    if server.uuid_to_guid_str(lo, hi) != str(u):
        raise RuntimeError("guid halves do not round-trip for %s" % u)
    return lo, hi


def load_index():
    with open(INDEX, encoding="utf-8") as fh:
        return json.load(fh)


def load_sets():
    with open(SETDATA, encoding="utf-8") as fh:
        return {s["name"]: s for s in json.load(fh)}


def have_carddata():
    return {f[:-5] for f in os.listdir(CARD_DIR) if f.endswith(".json")}


def scan(index, set_name):
    """Everything the bundles say about one set.

    Driven off the asset NAMESPACE (the part before the "/") rather than the
    bundle name, because the namespace is exactly what the client requests and
    a set whose name contains an underscore - Promo_SWSH, SWSH_Energy - cannot
    be split out of a bundle name reliably.
    """
    art, kinds, foils, marks = {}, {}, {}, {}
    for bundle, assets in index.items():
        segment = None
        if bundle.startswith(set_name + "_"):
            segment = bundle[len(set_name) + 1:].split("_")[0]
        for asset in assets:
            if "/" not in asset:
                continue
            namespace, leaf = asset.split("/", 1)
            if namespace == set_name:
                mark = SUFFIX.match(leaf)
                if mark:
                    marks.setdefault(mark.group(1), set()).add(mark.group(2))
                    continue
                if LEAF.match(leaf):
                    art[leaf] = leaf
                    if segment:
                        kinds.setdefault(leaf, segment)
            elif namespace.startswith(set_name + "_wp_"):
                rest = namespace[len(set_name) + 4:]
                foils.setdefault(leaf, set()).add(rest.split("_")[0])
    return art, kinds, foils, marks


def build_set(set_name, meta, index):
    art, kinds, foils, marks = scan(index, set_name)
    if not art:
        return None
    archetypes = []
    for leaf in sorted(art):
        number = int(LEAF.match(leaf).group(1))
        variant = LEAF.match(leaf).group(2)
        segment = kinds.get(leaf, "")
        tags = marks.get(leaf, set())

        u = uuid.uuid5(NAMESPACE, "%s/%s" % (set_name, leaf))
        lo, hi = guid_halves(u)
        key = "ptcgo-local.card.%s.%s" % (set_name.lower(), leaf)

        attrs = [
            {"n": ATTR_ARCHETYPE, "v": obj_guid(lo, hi)},
            {"n": ATTR_PRINT_GROUP, "v": obj_guid(lo, hi)},
            {"n": ATTR_NAME_KEY, "v": obj_text('"$$$%s$$$"' % key)},
            {"n": ATTR_UNKNOWN_BOOL, "v": {"t": T_BOOL}},
            {"n": ATTR_SET, "v": obj_str(set_name)},
            {"n": ATTR_NUMBER, "v": obj_int(number)},
            {"n": ATTR_SET_CARD_ID, "v": obj_int(meta.get("number") or 0)},
            {"n": ATTR_CARD_CLASS, "v": obj_int(201)},
        ]

        if "energyicon" in tags or segment == "Energy":
            attrs.append({"n": ATTR_CARD_TYPE, "v": obj_str("Energy")})
        elif segment == "trainer" or "toolpip" in tags:
            attrs.append({"n": ATTR_CARD_TYPE, "v": obj_str("TrainerCard")})
            if "toolpip" in tags:
                attrs.append({"n": ATTR_TRAINER_TYPE,
                              "v": obj_str("PokemonTool")})
        else:
            attrs.append({"n": ATTR_CARD_TYPE, "v": obj_str("Pokemon")})
            # 200570 is why this script exists. Without it the card renders
            # blank, so a type we cannot name from the bundle falls back to
            # Colorless rather than being omitted.
            colour = TYPES.get(segment, "Colorless")
            attrs.append({"n": ATTR_TYPES,
                          "v": {"a": [obj_str(colour)], "t": T_ARRAY}})

        # A variant printing asks for a different texture; without this it
        # silently renders the plain art of the same number.
        if variant:
            attrs.append({"n": ATTR_ASSET_NAME, "v": obj_str(leaf)})

        kind = next((k for k in ("std", "secondary", "ph", "pcd")
                     if k in foils.get(leaf, ())), None)
        if kind:
            mask, effect = FOIL_BY_KIND[kind]
            attrs.append({"n": ATTR_FOIL_MASK, "v": obj_str(mask)})
            attrs.append({"n": ATTR_FOIL_EFFECT, "v": obj_str(effect)})

        archetypes.append({"lo": lo, "hi": hi, "attrs": attrs,
                           "_label": "%s %s" % (meta.get("externalId")
                                                or set_name, leaf),
                           "_key": key})
    return archetypes


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="", help="one set name")
    args = ap.parse_args(argv)

    index = load_index()
    sets = load_sets()
    known = have_carddata()
    targets = [n for n in sets if n not in known]
    if args.only:
        targets = [n for n in targets if n == args.only]
    if not targets:
        sys.exit("nothing to build (every set already has card data)")

    existing = set()
    for fn in os.listdir(CARD_DIR):
        if fn.endswith(".json"):
            with open(os.path.join(CARD_DIR, fn), encoding="utf-8") as fh:
                for a in json.load(fh).get("archetypes") or []:
                    existing.add((a["lo"], a["hi"]))

    built, strings, total, skipped = {}, {}, 0, []
    for name in sorted(targets, key=lambda n: sets[n].get("number") or 0):
        archetypes = build_set(name, sets[name], index)
        if not archetypes:
            skipped.append(name)
            continue
        for a in archetypes:
            if (a["lo"], a["hi"]) in existing:
                raise RuntimeError("id collision with a real card in %s" % name)
            strings[a.pop("_key")] = a.pop("_label")
        built[name] = archetypes
        total += len(archetypes)

    print("%-12s %6s  %s" % ("set", "cards", "official count"))
    for name in sorted(built, key=lambda n: sets[n].get("number") or 0):
        print("%-12s %6d  %d" % (name, len(built[name]),
                                 sets[name].get("count") or 0))
    print("\n%d sets, %d browsable cards" % (len(built), total))
    if skipped:
        print("no art on disk, skipped: %s" % ", ".join(sorted(skipped)))

    if not args.apply:
        print("\n(dry run - pass --apply to write %s)" % OUT_DIR)
        return 0

    if not os.path.isdir(OUT_DIR):
        os.makedirs(OUT_DIR)
    for name, archetypes in built.items():
        path = os.path.join(OUT_DIR, name + ".json")
        payload = {"set": name, "archetypes": archetypes,
                   "checksum": uuid.uuid5(NAMESPACE, name + str(len(archetypes))).hex}
        tmp = path + ".part"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, path)
    labels = os.path.join(OUT_DIR, "_labels.json")
    tmp = labels + ".part"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(strings, fh, indent=1, sort_keys=True)
    os.replace(tmp, labels)
    print("\nwrote %d files into %s" % (len(built) + 1, OUT_DIR))
    print("now restart the server; card_db() still reads carddata/ only")
    return 0


if __name__ == "__main__":
    sys.exit(main())
