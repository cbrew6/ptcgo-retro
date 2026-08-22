#!/usr/bin/env python3
"""Rebuild the avatar wardrobe catalog that PTCGO's servers used to send.

Why this exists
---------------
Wardrobe items reached the client through exactly one message,
``dwd.Protobuf.cake.item.AllAvatarArchetypesFound``. Those archetypes were
server-side data, so unlike card archetypes they were never written to disk by
the installer and never cached under ``archetypes/`` (a scan of all 9,942
cached archetypes finds zero carrying any avatar attribute). They are simply
gone, and while the server answers that message with an empty list the client
builds an empty wardrobe and then never requests a single avatar asset - which
is why ``output_log.txt`` contains no avatar traffic at all.

Everything needed to rebuild them, however, survives on this machine:

* the shipped bundles enumerate the real sprite names (1,719 ``_thumb``
  assets, ~2,900 body assets),
* the wardrobe slot is recoverable from the body asset's suffix
  (``_hair``, ``_hatmask``, ``_jacketll``, ...),
* gender is the leading ``f``/``m`` on every stem,
* ``LocalizationDB-UTF16.db`` still holds the item display names.

So this script synthesises one archetype per thumbnail sprite and writes
``avatars.json`` in the same shape as ``carddata/*.json``. It never invents an
asset name: every sprite value here is a stem that exists in
``bundle_index.json`` (the sole exception being the skin-colour swatches, which
the client renders from fixed sprites and a hex code - see SKIN_COLOUR below).

What the client actually does with these attributes
---------------------------------------------------
Established by decompiling ``pie-src.dll`` and resolving the obfuscated
attribute fields from IL (``ldsfld`` operands -> ``AttributeDefinition.Key``),
because the decompiler renders several distinct fields under the same name:

  ArchetypesUtil.CreateAvatarRenderers   200215 -> .Value            (unguarded)
  ArchetypesUtil.DefaultItemForGroup     10220 -> .Value             (unguarded)
                                         200890, 200940, 200930
  ArchetypeExtensions.IsEmptyItem        10220 -> .Value             (unguarded)
  AvatarItemsRenderer.setItemSpriteName  200930, 10220, 200890
  AvatarItemsRenderer.UpdateThumbnail    200890, 200930
  AvatarBuilderController                200930, 200890,
      .compileAllAvatarItemsList         200950 -> .Value            (unguarded)
      .createCollectionItemList          200880 -> .ID               (unguarded)
  w.f (rarity component) ctor            200900 -> .ID               (unguarded)
      - reached for every archetype that carries 200890, because
        w.g.CreateArchetype builds the rarity component with
        isAvatarItem=true whenever the Group attribute is present.

A missing attribute is not an error at lookup time - ``MutableAttributes``
hands back a default-constructed attribute - so the value is ``null`` and the
unguarded dereferences above throw. That is why this script emits more than
the five "interesting" attributes: 200900/200950/200880 are pure crash guards
and are documented as such at each constant. ``--minimal`` drops them.

Thumbnail request format (verified)
-----------------------------------
``I.e.FormatAssetRequest("avatar_thumbs", setItemSpriteName() + "_thumb")``
with format ``"{0}/{1}"``, i.e. ``avatar_thumbs/<sprite>_thumb``, where
``setItemSpriteName()`` applies two rewrites:

* Group.Hair and the sprite is neither ``MBald`` nor ``FBald``: ``<sprite>fr``
* Group.Skin_color: the sprite is replaced outright by ``mdefinedchin``
  (male) or ``fvulpinehead`` (female), then tinted with the hex code parsed
  out of the sprite name.

The body/flat renderer (bundle ``avatar``, ``<sprite>.ToLower() + "_" +
suffix``) is a different code path and was NOT verified end to end, so nothing
here is tailored to it beyond keeping the sprite value equal to a real body
stem.

Encoding of 200930 (was the open question)
------------------------------------------
Bare sprite name inside a JSON string literal - ``{"s": "\\"fafroblack\\"",
"t": 8}`` - not the ``$$$key$$$`` form. Type 8 is ``Object.Type.JSON`` and the
client does ``JSON.Deserialize(stringValue, typeof(LocalizableText))``, so the
payload must be valid JSON, hence the quotes; and the value must be the sprite
stem rather than a localisation key because:

* the body renderer builds a real asset name out of it
  (``text.ToLower() + "_" + ItemSuffix``),
* ``j.Z.get_SkinColor()`` reads its last six characters as a hex colour,
* ``IsEmptyItem`` compares it against literals ``"MBald"``, ``"FNoHat"``,
  ``"FNoJacket"``, ``"MNoGlasses"``, ``"MBareFace"`` ...,
* and ``LocalizationLookup.Localize`` returns the key unchanged when there is
  no row for it, so a sprite stem survives the LocalizableText round trip
  (no ``Lookup`` row is ever a bare stem - the keys are dotted paths).

Usage
-----
    python tools/build_avatar_catalog.py            # dry run, writes nothing
    python tools/build_avatar_catalog.py --apply    # writes ../avatars.json

Output is deterministic: archetypes sorted by sprite, GUIDs derived by uuid5
from the sprite name, compact separators. Two runs are byte-identical.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import sqlite3
import sys
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

BUNDLE_INDEX = os.path.join(REPO, "bundle_index.json")
CARD_DIR = os.path.join(REPO, "carddata")
DEFAULT_OUT = os.path.join(REPO, "avatars.json")
LOCALIZATION_DB = os.path.join(
    os.environ.get("USERPROFILE", ""), "AppData", "LocalLow",
    "The Pokémon Company International",
    "Pokemon Trading Card Game Online", "LocalizationDB-UTF16.db")

SET_NAME = "AvatarWardrobe"

# uuid5 namespace input prefix. Anything stable works; keeping it explicit
# means the ids can be regenerated from the sprite list alone.
GUID_PREFIX = "ptcgo-local.avatar."

# --------------------------------------------------------------------------
# attributes
# --------------------------------------------------------------------------
ATTR_NAME = 10140        # LocalizableText, feeds NameData -> tooltips + sort
ATTR_GENDER = 10220      # enum GenderTypes
ATTR_PRODUCT_TYPE = 10540  # enum ProductTypes, must be AvatarItems
ATTR_PAIR_KEY = 200215   # int, male/female pairing (GenderMatchLookup)
ATTR_COLLECTION = 200880  # LocalizableText, "named item collection"
ATTR_GROUP = 200890      # enum j.y.Group, the wardrobe slot
ATTR_RARITY_TEXT = 200900  # LocalizableText parsed into Cake.enums.Rarities
ATTR_SPRITE = 200930     # LocalizableText carrying the sprite base name
ATTR_IS_DEFAULT = 200940  # bool?, marks the fallback item for a slot
ATTR_FREE = 200950       # bool?, "no purchase needed"

# Value encodings, matching the exporter that produced carddata/*.json and
# server.pb_object(): 3=STRING/enum, 4=BOOL, 5=INT, 8=JSON.
def v_enum(name):
    return {"s": name, "t": 3}


def v_int(n):
    return {"i": n, "t": 5}


def v_bool(b):
    # server.pb_object only emits boolValue when truthy, so {"t":4} is false.
    return {"t": 4, "b": True} if b else {"t": 4}


def v_text(raw):
    """LocalizableText: a JSON string literal, hence the embedded quotes."""
    return {"s": json.dumps(raw), "t": 8}


# --------------------------------------------------------------------------
# group inference
# --------------------------------------------------------------------------
# Body asset suffix -> j.y.Group member name. The enum names are matched
# case-sensitively by the client (TryEnumConvert is a plain dictionary lookup
# over Enum.GetNames), so "face_makeup" really does start lowercase. A suffix
# absent from this table means "this asset is not a wardrobe slot" - the
# hand/handheld suffixes have no Group member at all, so those items are
# skipped rather than guessed at.
SUFFIX_GROUP = {
    "hair": "Hair",
    "hat": "Hat", "hatmask": "Hat", "hats": "Hat",
    "jackett": "Jacket", "jacketh": "Jacket", "jacketll": "Jacket",
    "jacketlu": "Jacket", "jacketrl": "Jacket", "jacketru": "Jacket",
    "jacketth": "Jacket", "jackth": "Jacket",
    "shirtt": "Shirt", "shirtt_b": "Shirt", "shirtll": "Shirt",
    "shirtlu": "Shirt", "shirtrl": "Shirt", "shirtru": "Shirt",
    "trousers": "Trousers",
    "shoes": "Shoes",
    "eyes": "Eyes",
    "eyebrows": "Eyebrows",
    "mouth": "Mouth",
    "nose": "Nose",
    "face_prop": "Face_prop",
    "face_makeup": "face_makeup",
    "facial_hair": "Facial_hair",
    "shape": "Shape",
}

# Sprite names the client compares case-sensitively (ArchetypeExtensions
# .IsEmptyItem, AvatarItemsRenderer.setItemSpriteName, DefaultItemForGroup and
# the AvatarItemRendererBase "empty" set). The bundles store these assets in
# lowercase, and the manifest lookup is StringComparer.OrdinalIgnoreCase, so
# emitting the client's casing still resolves to the real asset - while
# emitting the lowercase form would silently break the comparisons (a bald
# head would ask for "mbaldfr_thumb", which does not exist).
SENTINEL_CASE = {
    "mbald": "MBald", "fbald": "FBald",
    "mnohat": "MNoHat", "fnohat": "FNoHat",
    "mnojacket": "MNoJacket", "fnojacket": "FNoJacket",
    "mnoglasses": "MNoGlasses", "fnoglasses": "FNoGlasses",
    "mbareface": "MBareFace",
    "mdefinedchin": "MDefinedChin", "fheartshaped": "FHeartShaped",
    "fbarefoot": "FBarefoot",
}

# Rarity is genuinely lost: nothing on disk records which wardrobe items were
# rare. The client parses this text into Cake.enums.Rarities after stripping
# backslashes and spaces, and an unparseable value leaves Rarities.UNSET,
# which renders the same grey frame as Common - so Common is both the honest
# and the visually identical choice.
RARITY_TEXT = "Common"

# createCollectionItemList() groups items into "named collections" by cutting
# this string at its last 'm'/'f'. It skips anything empty or equal to "na",
# and "na" is a string the client itself carries, so it is the sentinel the
# original data must have used for "not part of a collection".
NO_COLLECTION = "na"

LOC_PREFIX = "com.direwolfdigital.cake.data.products.avataritems."

# Skin tones have no art at all: setItemSpriteName() swaps in a fixed sprite
# per gender and tints it with the hex code embedded in the item name, and
# j.Z.get_SkinColor() reads the same six characters. So these items are
# rebuilt from the localisation keys rather than from the bundles - the value
# is not an asset name for this group.
SKIN_KEY_RE = re.compile(r"^([fm])01colorselection_0x[0-9a-f]{6}$")


def load_bundle_assets(path):
    """Return (thumb stems, {body stem: {suffixes}}) from bundle_index.json."""
    with open(path, encoding="utf-8") as fh:
        index = json.load(fh)
    thumbs, body = set(), collections.defaultdict(set)
    for bundle, assets in index.items():
        low = bundle.lower()
        if low.startswith("avatar_thumbs_"):
            for a in assets:
                if a.endswith("_thumb"):
                    thumbs.add(a[:-len("_thumb")])
        elif low.startswith("avatar_"):
            for a in assets:
                if "_" in a:
                    stem, suffix = a.split("_", 1)
                    body[stem].add(suffix)
    return thumbs, body


def load_localization(path):
    """{stem: display name} for com.direwolfdigital...avataritems.<stem>.name.

    Seven rows in the shipped DB have a truncated ".nam" suffix; they are real
    strings, so they are accepted too.
    """
    if not os.path.exists(path):
        return {}
    uri = "file:%s?mode=ro" % path.replace("\\", "/")
    con = sqlite3.connect(uri, uri=True)
    try:
        rows = con.execute(
            "select key, value from Lookup where key like ?",
            (LOC_PREFIX + "%",)).fetchall()
    finally:
        con.close()
    out = {}
    for key, value in rows:
        stem = key[len(LOC_PREFIX):]
        if stem.endswith(".name"):
            out[stem[:-5]] = (key, value)
        elif stem.endswith(".nam"):
            out.setdefault(stem[:-4], (key, value))
    return out


def load_existing_guid_halves(card_dir):
    """Every (lo, hi) already in carddata, to prove the new ids collide with
    nothing. The client does dictionary.Add() per archetype and a duplicate
    key aborts the entire load."""
    seen = set()
    if not os.path.isdir(card_dir):
        return seen
    for name in sorted(os.listdir(card_dir)):
        if not name.endswith(".json"):
            continue
        with open(os.path.join(card_dir, name), encoding="utf-8") as fh:
            data = json.load(fh)
        for a in data.get("archetypes") or []:
            seen.add((a["lo"], a["hi"]))
    return seen


def guid_halves(sprite):
    """Deterministic archetype id. Split exactly the way server.pb_uuid()
    does: hi = first eight bytes big-endian, lo = last eight."""
    raw = uuid.uuid5(uuid.NAMESPACE_DNS, GUID_PREFIX + sprite.lower()).bytes
    return int.from_bytes(raw[8:], "big"), int.from_bytes(raw[:8], "big")


class Item:
    __slots__ = ("sprite", "group", "gender", "stem", "loc_key", "name",
                 "is_default", "pair_id")

    def __init__(self, sprite, group, gender, stem):
        self.sprite = sprite      # value of attribute 200930
        self.group = group        # j.y.Group member name
        self.gender = gender      # "Male" / "Female"
        self.stem = stem          # bundle stem the sprite came from
        self.loc_key = None
        self.name = None
        self.is_default = False


def collect_items(thumbs, body, skin_stems):
    """Turn thumbnail sprites into wardrobe items, reporting what is dropped.

    Only thumbnails are used, because the thumbnail is what the wardrobe grid
    renders and its request format is the one that was actually verified.
    """
    items, skipped = [], collections.Counter()
    dropped = collections.defaultdict(list)

    for stem in sorted(thumbs):
        suffixes = body.get(stem)
        if not suffixes:
            # Mostly numbered per-face variants (mblackeyes3eye) and loose
            # props (pen, thermos, ball_base) whose slot cannot be read off
            # any asset name. Guessing one would be exactly the kind of
            # plausible-looking invention this project has been burned by.
            skipped["no body asset -> no derivable slot"] += 1
            dropped["no body asset"].append(stem)
            continue
        groups = {SUFFIX_GROUP[s] for s in suffixes if s in SUFFIX_GROUP}
        if not groups:
            # left_hand / right_hand / *handheldmask / characters: the Group
            # enum has no member for held items at all.
            skipped["no wardrobe slot for suffix"] += 1
            dropped["no slot"].append(stem)
            continue
        if len(groups) > 1:
            skipped["ambiguous slot"] += 1
            dropped["ambiguous"].append("%s %s" % (stem, sorted(groups)))
            continue
        group = groups.pop()
        if stem[0] not in "fm":
            skipped["no gender prefix"] += 1
            dropped["no gender"].append(stem)
            continue
        gender = "Male" if stem[0] == "m" else "Female"

        sprite = stem
        if group == "Hair":
            # The client appends "fr" to hair sprites, so the stored value is
            # the stem without it. A "bk" thumbnail is the back half of a hair
            # item whose front already produced an entry; a hair thumb that is
            # neither would make the client ask for an asset that does not
            # exist, so it is dropped instead.
            if stem.endswith("fr"):
                sprite = stem[:-2]
            elif stem.endswith("bk") and (stem[:-2] + "fr") in thumbs:
                skipped["hair back-half duplicate"] += 1
                continue
            elif stem not in ("mbald", "fbald"):
                skipped["hair without an 'fr' sprite"] += 1
                dropped["hair no fr"].append(stem)
                continue
        sprite = SENTINEL_CASE.get(sprite, sprite)
        items.append(Item(sprite, group, gender, stem))

    for stem in sorted(skin_stems):
        items.append(Item(stem, "Skin_color",
                          "Male" if stem[0] == "m" else "Female", stem))

    return items, skipped, dropped


def assign_pair_keys(items):
    """Attribute 200215 groups an item with its opposite-gender twin.

    CreateAvatarRenderers builds GenderMatchLookup[value] -> [archetypes] and
    GetLinkedArchetypeList hands that list to the UI when the player flips
    gender, so the pairing is simply "same sprite, other leading letter".
    Ids are indices into the sorted list of pair keys: stable across runs and
    never zero (server.pb_object drops a zero int, which would leave 200215
    unset and put the crash back).
    """
    keys = sorted({it.sprite[1:].lower() for it in items})
    ids = {k: i + 1 for i, k in enumerate(keys)}
    for it in items:
        it.pair_id = ids[it.sprite[1:].lower()]
    return len(keys)


def resolve_names(items, loc):
    """Attach the real localisation key where one exists.

    The 2011-era catalogue named its products differently from its art
    (loc "m01beretblk" vs asset "mblackberet"), and nothing on disk bridges
    the two, so unmatched items fall back to their own sprite name rather
    than to a fuzzy match - the wrong name is worse than a plain one.
    """
    resolved = 0
    for it in items:
        hit = loc.get(it.stem)
        if hit:
            it.loc_key, it.name = hit
            resolved += 1
    return resolved


def choose_defaults(items):
    """Mark one item per (slot, gender) with 200940.

    DefaultItemForGroup() returns the first archetype whose gender and Group
    match and which has 200940 set; with nothing marked it falls through to a
    sprite named "EmptyAvatarItem" and, finding none, returns null - which
    the renderers dereference. Prefer the slot's own "empty" item (MNoHat,
    FNoJacket, MBald ...) so an unset slot renders as bare, else the first
    sprite alphabetically.
    """
    empties = set(SENTINEL_CASE.values())
    by_slot = collections.defaultdict(list)
    for it in items:
        by_slot[(it.group, it.gender)].append(it)
    chosen = 0
    for key in sorted(by_slot):
        group = sorted(by_slot[key], key=lambda i: i.sprite)
        pick = next((i for i in group if i.sprite in empties), group[0])
        pick.is_default = True
        chosen += 1
    return chosen


def thumbnail_asset(item):
    """Re-implementation of AvatarItemsRenderer.setItemSpriteName().

    Kept here so the catalog can be checked against the bundles the way the
    client will actually ask for it, rather than against the stem it was built
    from - the Hair and Skin_color rewrites are exactly where a wrong sprite
    value would go unnoticed.
    """
    sprite = item.sprite
    if item.group == "Skin_color":
        sprite = "mdefinedchin" if item.gender == "Male" else "fvulpinehead"
    elif item.group == "Hair" and sprite not in ("MBald", "FBald"):
        sprite += "fr"
    return sprite + "_thumb"


def verify_thumbnails(items, thumbs):
    """Every emitted item must resolve to an asset that really exists.

    The manifest is keyed case-insensitively (StringComparer.OrdinalIgnoreCase
    in AssetBundleRequester), so the comparison is too.
    """
    have = {t.lower() + "_thumb" for t in thumbs}
    return [i for i in items if thumbnail_asset(i).lower() not in have]


def build_attrs(item, minimal, free):
    attrs = [
        {"n": ATTR_SPRITE, "v": v_text(item.sprite)},
        {"n": ATTR_GROUP, "v": v_enum(item.group)},
        {"n": ATTR_GENDER, "v": v_enum(item.gender)},
        {"n": ATTR_PRODUCT_TYPE, "v": v_enum("AvatarItems")},
        {"n": ATTR_PAIR_KEY, "v": v_int(item.pair_id)},
    ]
    if minimal:
        return attrs
    name = "$$$%s$$$" % item.loc_key if item.loc_key else item.sprite
    attrs += [
        {"n": ATTR_NAME, "v": v_text(name)},
        {"n": ATTR_RARITY_TEXT, "v": v_text(RARITY_TEXT)},
        {"n": ATTR_COLLECTION, "v": v_text(NO_COLLECTION)},
        {"n": ATTR_FREE, "v": v_bool(free)},
    ]
    if item.is_default:
        attrs.append({"n": ATTR_IS_DEFAULT, "v": v_bool(True)})
    return attrs


def build_catalog(items, minimal, free):
    archetypes = []
    for item in sorted(items, key=lambda i: i.sprite.lower()):
        lo, hi = guid_halves(item.sprite)
        archetypes.append({"lo": lo, "hi": hi,
                           "attrs": build_attrs(item, minimal, free)})
    payload = json.dumps(archetypes, separators=(",", ":"), sort_keys=True)
    checksum = hashlib.md5(payload.encode("utf-8")).hexdigest()
    return {"set": SET_NAME, "archetypes": archetypes, "checksum": checksum}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true",
                    help="write the file (default is a dry run)")
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--bundle-index", default=BUNDLE_INDEX)
    ap.add_argument("--localization-db", default=LOCALIZATION_DB)
    ap.add_argument("--minimal", action="store_true",
                    help="emit only the five wardrobe attributes, without the "
                         "200900/200950/200880 crash guards")
    ap.add_argument("--not-free", action="store_true",
                    help="emit 200950=false; wardrobe items are then only "
                         "usable if the collection grants them")
    ap.add_argument("--verbose", action="store_true",
                    help="list the skipped sprites")
    args = ap.parse_args(argv)

    thumbs, body = load_bundle_assets(args.bundle_index)
    loc = load_localization(args.localization_db)
    skin = sorted(s for s in loc if SKIN_KEY_RE.match(s))

    items, skipped, dropped = collect_items(thumbs, body, skin)
    pairs = assign_pair_keys(items)
    resolved = resolve_names(items, loc)
    defaults = choose_defaults(items)
    catalog = build_catalog(items, args.minimal, not args.not_free)

    # Ids must be unique among themselves and disjoint from carddata.
    existing = load_existing_guid_halves(CARD_DIR)
    mine = [(a["lo"], a["hi"]) for a in catalog["archetypes"]]
    dupes_internal = len(mine) - len(set(mine))
    collisions = sorted(set(mine) & existing)

    by_group = collections.Counter((i.group, i.gender) for i in items)
    print("thumbnail sprites in bundles : %d" % len(thumbs))
    print("archetypes emitted           : %d" % len(catalog["archetypes"]))
    print("  pair keys (200215)         : %d" % pairs)
    print("  slot defaults (200940)     : %d" % defaults)
    print("names from localization      : %d" % resolved)
    print("names fell back to sprite    : %d" % (len(items) - resolved))
    print()
    print("%-14s %8s %8s %8s" % ("slot", "female", "male", "total"))
    for group in sorted({g for g, _ in by_group}):
        f = by_group[(group, "Female")]
        m = by_group[(group, "Male")]
        print("%-14s %8d %8d %8d" % (group, f, m, f + m))
    print("%-14s %8d %8d %8d" % (
        "TOTAL",
        sum(v for (g, s), v in by_group.items() if s == "Female"),
        sum(v for (g, s), v in by_group.items() if s == "Male"),
        len(items)))
    print()
    for reason, count in sorted(skipped.items()):
        print("skipped: %-34s %d" % (reason, count))
    if args.verbose:
        for reason, names in sorted(dropped.items()):
            print("  [%s] %s" % (reason, " ".join(sorted(names))))
    print()
    bad = verify_thumbnails(items, thumbs)
    print("carddata archetypes checked  : %d" % len(existing))
    print("id collisions with carddata  : %d" % len(collisions))
    print("duplicate ids within catalog : %d" % dupes_internal)
    print("thumbnail requests missing   : %d" % len(bad))
    if bad:
        print("  e.g. " + ", ".join(thumbnail_asset(i) for i in bad[:5]))
    if collisions or dupes_internal or bad:
        print("REFUSING to write: an id collides (the client does "
              "dictionary.Add() per archetype and throws on duplicates) or a "
              "sprite does not resolve to a real asset.")
        return 1

    # Not sort_keys: the dicts are built in a fixed order already, and keeping
    # it puts lo/hi/attrs in the order carddata's own records read in.
    text = json.dumps(catalog, separators=(",", ":"))
    print("payload                      : %d bytes, md5 %s"
          % (len(text), hashlib.md5(text.encode("utf-8")).hexdigest()))
    if not args.apply:
        print("\ndry run - pass --apply to write %s" % args.out)
        return 0
    with open(args.out, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    print("wrote %s" % args.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
