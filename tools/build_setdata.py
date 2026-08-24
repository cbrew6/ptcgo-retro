"""
Synthesizes carddata for the sets PTCGO's servers never handed us.

The installer's card data stops at SM4 (Crimson Invasion). Everything from
SM5 (Ultra Prism) to Crown Zenith arrived over the wire on first login and
is simply absent - which is why Honchkrow-GX (sm10-109) cannot be found.

The art for those sets DOES exist locally, recovered from donated bundle
caches, and the client already implements their mechanics: the January 2023
assemblies carry RareHoloVMAX, RareHoloVSTAR, VSTARDamageColor, RareRadiant
and FUSION_STRIKE. Only the archetype RECORDS are missing, and those are
numbers - HP, costs, damage, weakness - that a public database has.

What this does NOT do is recover the original archetype IDs. The donated
AttributeDB holds 1,336 GUIDs we lack, and they are UUIDv5 - name-derived -
but the namespace was the server's and is not in any local binary, so they
cannot be recomputed. That does not matter: we mint the collection ourselves,
so nothing has to match TPCI's identifiers. The ids here are UUIDv5 over a
namespace of our own, which makes them stable across reruns (a rebuild does
not invalidate saved decks) and unable to collide with the v4/v1 ids the real
sets use.

Localization is written alongside as <SET>.loc.json rather than into the
client's DB, which PieDB.Init wipes on every launch. server.py serves those
rows with the shipped ones.

Usage:
    python tools/build_setdata.py --list
    python tools/build_setdata.py SM5
    python tools/build_setdata.py --with-art --apply
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

CARD_DIR = os.path.join(HERE, "carddata")
API = "https://api.pokemontcg.io/v2"

# Our own namespace. Any fixed GUID works; this one is arbitrary and local.
# What matters is that it is CONSTANT, so ids are reproducible, and that the
# result is a v5 uuid, which cannot collide with the real sets' v4/v1 ids.
NAMESPACE = uuid.UUID("6f0f5f2a-1d1a-4a52-9a3f-0b7c9d2e4a10")

# The protobuf Object type tags carddata uses.
T_ARRAY, T_STRING, T_BOOL, T_INT, T_GUID = 1, 3, 4, 5, 7
T_TEXT = 8                      # LocalizableText: a string, tagged differently

SUPERTYPE = {"Pokémon": "Pokemon", "Pokemon": "Pokemon",
             "Trainer": "Trainer", "Energy": "Energy"}

STAGE = {"Basic": "Basic", "Stage 1": "Stage1", "Stage 2": "Stage2",
         "VMAX": "Stage1", "VSTAR": "Stage1", "V": "Basic",
         "Restored": "Basic", "LEGEND": "Basic", "BREAK": "Stage1"}

# PTCGO's own rarity vocabulary. Anything unmapped falls back to Common,
# which affects only how the card is labelled, never how it plays.
RARITY = {
    "Common": "Common", "Uncommon": "Uncommon", "Rare": "Rare",
    "Rare Holo": "RareHolo", "Rare Holo EX": "RareHoloEX",
    "Rare Holo GX": "RareHoloGX", "Rare Holo V": "RareHoloV",
    "Rare Holo VMAX": "RareHoloVMAX", "Rare Holo VSTAR": "RareHoloVSTAR",
    "Rare Ultra": "RareUltra", "Rare Secret": "RareSecret",
    "Rare Rainbow": "RareRainbow", "Rare Shiny": "RareShiny",
    "Rare Shining": "RareShining", "Rare Prism Star": "RarePrismStar",
    "Rare Holo LV.X": "RareHoloLVX", "Amazing Rare": "AmazingRare",
    "Radiant Rare": "RareRadiant", "Promo": "Promo",
    "Rare ACE": "RareACE", "Rare BREAK": "RareBREAK",
    "Rare Prime": "RarePrime", "LEGEND": "RareLegend",
}

TRAINER_TYPE = {"Item": "Item", "Supporter": "Supporter",
                "Stadium": "Stadium", "Pokémon Tool": "PokemonTool",
                "Pokemon Tool": "PokemonTool", "Tool": "PokemonTool"}

ATTRS = {
    "ARCHETYPE": 10000, "UNK_10090": 10090, "NAME_KEY": 10140,
    "CARD_ID": 10190, "FAMILY": 200260, "EVOLVES_KEY": 200280,
    "CARD_TYPE": 200300, "HP": 200490, "STAGE": 200540, "RARITY": 200550,
    "TYPES": 200570, "SET": 200580, "WEAK_TYPES": 200590, "RESIST_TYPE": 200600,
    "FOIL_EFFECT": 200610, "FOIL_MASK": 200620, "NAME": 200630,
    "EVOLVES_FROM": 200640, "WEAK_OP": 200660, "TRAINER_TYPE": 200670_0 // 10,
    "ABILITIES": 200740, "NUMBER": 200780, "RETREAT": 200800,
    "WEAK_AMOUNT": 200820, "RESIST_AMOUNT": 200830, "RULES_TEXT": 200310,
    "ENERGY_PROVIDED": 201040, "BASIC_ENERGY": 200520,
}
ATTR_TRAINER_TYPE = 200270


def clean_name(name):
    """PTCGO writes names without spaces or punctuation.

    "Honchkrow-GX" -> "HonchkrowGX", "Nidoran F" -> "NidoranFemale". The
    engine matches evolvesFrom against exactly this form, so a Stage 1 whose
    name is normalised differently from its Basic never evolves.
    """
    n = (name or "")
    n = n.replace("♀", "Female").replace("♂", "Male")
    n = n.replace("é", "e").replace("É", "E")
    n = re.sub(r"[^A-Za-z0-9]", "", n)
    return n


def loc_key(*parts):
    return ".".join(p for p in parts if p)


def obj(**kw):
    return kw


def attr(ident, value):
    return {"n": ident, "v": value}


def s_val(text):
    return {"s": text, "t": T_STRING}


def i_val(number):
    return {"i": number, "t": T_INT}


def arr_val(items):
    return {"a": items, "t": T_ARRAY}


def fetch(path, params, tries=4):
    """One API call, with the retry budget a whole set deserves.

    A single 500 on the set-metadata call costs 200+ cards, and those do
    happen - three sets were lost to transient 500s during the art run.
    """
    url = "%s/%s?%s" % (API, path, urllib.parse.urlencode(params))
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "ptcgo-local/1.0"})
            with urllib.request.urlopen(req, timeout=60) as fh:
                return json.load(fh)
        except Exception as exc:            # noqa: BLE001 - retry anything
            last = exc
            time.sleep(2 + 3 * attempt)
    raise RuntimeError("%s failed after %d tries: %s" % (url, tries, last))


def fetch_set(set_id):
    out, page = [], 1
    while True:
        body = fetch("cards", {"q": "set.id:%s" % set_id,
                               "page": page, "pageSize": 250})
        data = body.get("data") or []
        out.extend(data)
        if len(out) >= (body.get("totalCount") or 0) or not data:
            break
        page += 1
    return out


def card_guid(set_code, number):
    """Stable, local, and unable to collide with a real archetype id.

    Derived rather than random so that rebuilding a set does not orphan every
    deck that referenced it.
    """
    return uuid.uuid5(NAMESPACE, "ptcgo-local:%s:%s" % (set_code, number))


def guid_halves(value):
    """The lo/hi pair carddata stores, matching ProtobufExtensions.ToGuid."""
    raw = value.bytes
    lo = int.from_bytes(raw[:8], "little", signed=False)
    hi = int.from_bytes(raw[8:], "little", signed=False)
    return lo, hi


def energy_cost(cost_list):
    counted = {}
    for element in cost_list or []:
        counted[element] = counted.get(element, 0) + 1
    return counted


def split_damage(text):
    """"30+" -> (30, "+"). The suffix means the real number depends on card
    text we may not implement, so the engine deals the base and records why."""
    raw = (text or "").strip()
    if not raw:
        return 0, ""
    match = re.match(r"^(\d+)\s*([+x×-]?)$", raw)
    if not match:
        digits = re.sub(r"\D", "", raw)
        return (int(digits) if digits else 0), ("x" if "x" in raw or "×" in raw
                                                else "+" if "+" in raw else "")
    return int(match.group(1)), match.group(2).replace("×", "x")


def build_card(card, set_code, strings, families):
    """One API card -> one carddata archetype, plus its localization rows."""
    name = card.get("name") or ""
    pname = clean_name(name)
    number = card.get("number") or "0"
    guid = card_guid(set_code, number)
    lo, hi = guid_halves(guid)
    attrs = [attr(ATTRS["ARCHETYPE"], {"g": [lo, hi], "t": T_GUID})]

    supertype = SUPERTYPE.get(card.get("supertype"), "Pokemon")
    attrs.append(attr(ATTRS["CARD_TYPE"], s_val(supertype)))
    attrs.append(attr(ATTRS["NAME"], s_val(pname)))
    attrs.append(attr(ATTRS["SET"], s_val(set_code)))

    # The number is what every art request is keyed on, so a card whose
    # number will not parse would render blank no matter what else is right.
    digits = re.sub(r"\D", "", number)
    attrs.append(attr(ATTRS["NUMBER"], i_val(int(digits) if digits else 0)))
    if digits != number:
        # "TG05" / "SV12" style. 10020 is the asset-name override the client
        # prefers over the padded number.
        attrs.append(attr(10020, s_val(number)))

    key = loc_key("com.direwolfdigital.cake.data.archetypes",
                  supertype.lower(), pname, "name")
    strings[key] = name
    attrs.append(attr(ATTRS["NAME_KEY"],
                      {"s": '"$$$%s$$$"' % key, "t": T_TEXT}))

    rarity = RARITY.get(card.get("rarity"), "Common")
    attrs.append(attr(ATTRS["RARITY"], s_val(rarity)))

    subtypes = card.get("subtypes") or []
    if supertype == "Pokemon":
        hp = re.sub(r"\D", "", card.get("hp") or "")
        attrs.append(attr(ATTRS["HP"], i_val(int(hp) if hp else 0)))
        stage = next((STAGE[s] for s in subtypes if s in STAGE), "Basic")
        attrs.append(attr(ATTRS["STAGE"], s_val(stage)))
        types = card.get("types") or ["Colorless"]
        attrs.append(attr(ATTRS["TYPES"],
                          arr_val([s_val(t) for t in types])))
        attrs.append(attr(ATTRS["RETREAT"],
                          i_val(len(card.get("retreatCost") or []))))

        weak = (card.get("weaknesses") or [{}])[0]
        if weak.get("type"):
            attrs.append(attr(ATTRS["WEAK_TYPES"],
                              arr_val([s_val(weak["type"])])))
            amount = re.sub(r"\D", "", weak.get("value") or "2") or "2"
            attrs.append(attr(ATTRS["WEAK_AMOUNT"], i_val(int(amount))))
            attrs.append(attr(ATTRS["WEAK_OP"],
                              s_val("x" if "×" in (weak.get("value") or "")
                                    or "x" in (weak.get("value") or "")
                                    else "+")))
        resist = (card.get("resistances") or [{}])[0]
        attrs.append(attr(ATTRS["RESIST_TYPE"],
                          s_val(resist.get("type") or "NoColor")))
        if resist.get("type"):
            amount = re.sub(r"\D", "", resist.get("value") or "30") or "30"
            attrs.append(attr(ATTRS["RESIST_AMOUNT"], i_val(int(amount))))

        evolves = card.get("evolvesFrom")
        if evolves:
            attrs.append(attr(ATTRS["EVOLVES_FROM"], s_val(clean_name(evolves))))
            ekey = loc_key("com.direwolfdigital.cake.data.archetypes.pokemon",
                           clean_name(evolves), "name")
            attrs.append(attr(ATTRS["EVOLVES_KEY"],
                              {"s": '"$$$%s$$$"' % ekey, "t": T_TEXT}))
        # Evolution families are how the client groups a line together. The
        # root name is a good enough key: every stage of one line shares it.
        root = clean_name(evolves) if evolves else pname
        attrs.append(attr(ATTRS["FAMILY"],
                          i_val(families.setdefault(root, len(families) + 1))))
    elif supertype == "Trainer":
        ttype = next((TRAINER_TYPE[s] for s in subtypes if s in TRAINER_TYPE),
                     "Item")
        attrs.append(attr(ATTR_TRAINER_TYPE, s_val(ttype)))
        rules = " ".join(card.get("rules") or [])
        if rules:
            rkey = loc_key("com.direwolfdigital.cake.data.archetypes.trainer",
                           pname, "gametext")
            strings[rkey] = rules
            attrs.append(attr(ATTRS["RULES_TEXT"],
                              {"s": '"$$$%s$$$"' % rkey, "t": T_TEXT}))
    elif supertype == "Energy":
        # Special Energy provides whatever its text says, which we cannot
        # parse reliably; basic Energy provides its own colour. Only the
        # basic case is asserted here - a Special Energy with no options is
        # inert rather than wrong.
        basic = "Basic" in subtypes
        if basic:
            colour = re.sub(r"\s*Energy$", "", name).strip()
            attrs.append(attr(ATTRS["BASIC_ENERGY"], {"b": True, "t": T_BOOL}))
            attrs.append(attr(ATTRS["ENERGY_PROVIDED"],
                              s_val(json.dumps({"options": [[colour]]}))))
        rules = " ".join(card.get("rules") or [])
        if rules:
            rkey = loc_key("com.direwolfdigital.cake.data.archetypes.energy",
                           pname, "gametext")
            strings[rkey] = rules
            attrs.append(attr(ATTRS["RULES_TEXT"],
                              {"s": '"$$$%s$$$"' % rkey, "t": T_TEXT}))

    entries = []
    for ability in card.get("abilities") or []:
        entries.append(ability_json(set_code, pname, ability, strings,
                                    is_attack=False))
    for attack in card.get("attacks") or []:
        entries.append(ability_json(set_code, pname, attack, strings,
                                    is_attack=True))
    if entries:
        attrs.append(attr(ATTRS["ABILITIES"],
                          arr_val([s_val(e) for e in entries])))
    return {"lo": lo, "hi": hi, "attrs": attrs}


def ability_json(set_code, pname, entry, strings, is_attack):
    """The JSON string one entry of attribute 200740 holds.

    abilityID is a real GUID because the client echoes it back to say which
    attack was chosen; it has to be stable across rebuilds for the same
    reason the archetype id does.
    """
    label = entry.get("name") or ""
    slug = clean_name(label) or "Ability"
    kind = "attacks" if is_attack else "pokeabilities"
    tkey = loc_key("com.direwolfdigital.cake.rules.abilities", kind,
                   set_code, pname, slug, "title")
    gkey = loc_key("com.direwolfdigital.cake.rules.abilities", kind,
                   set_code, pname, slug, "gametext")
    strings[tkey] = label
    text = entry.get("text") or ""
    if text:
        strings[gkey] = text
    damage, operator = split_damage(entry.get("damage"))
    ability_id = str(uuid.uuid5(NAMESPACE,
                                "ability:%s:%s:%s" % (set_code, pname, slug)))
    return json.dumps({
        "cost": energy_cost(entry.get("cost")) if is_attack else {},
        "damage": damage,
        "title": "$$$%s$$$" % tkey,
        "gameText": ("$$$%s$$$" % gkey) if text else "",
        "abilityID": ability_id,
        "amountOperator": operator,
        "abilityType": "Attack" if is_attack else "PokeAbility",
        "conditionExceptions": [],
    }, separators=(",", ":"))


def build_set(set_id, set_code, verbose=True):
    cards = fetch_set(set_id)
    strings, families = {}, {}
    archetypes = [build_card(c, set_code, strings, families) for c in cards]
    if verbose:
        print("  %-8s %-28s %4d cards, %4d strings"
              % (set_code, set_id, len(archetypes), len(strings)))
    return archetypes, strings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("sets", nargs="*", help="set codes, e.g. SM5 SM10")
    ap.add_argument("--list", action="store_true",
                    help="show which sets are missing card data")
    ap.add_argument("--with-art", action="store_true",
                    help="every missing set we already hold art for")
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args(argv)

    os.environ.pop("SSLKEYLOGFILE", None)   # breaks python's TLS if it points
                                            # somewhere unwritable
    have = {f[:-5] for f in os.listdir(CARD_DIR) if f.endswith(".json")}
    art_sets = art_backed_sets()
    missing = sorted(art_sets - have)

    if args.list:
        print("card data for %d sets; art with no card data:" % len(have))
        for code in missing:
            print("   %s" % code)
        return 0

    wanted = args.sets or (missing if args.with_art else [])
    if not wanted:
        print("nothing to do: name sets, or pass --with-art")
        return 1

    for set_code in wanted:
        set_id = set_code.lower()
        archetypes, strings = build_set(set_id, set_code)
        if not archetypes:
            print("  %s: no cards returned; skipped" % set_code)
            continue
        if not args.apply:
            continue
        blob = {"set": set_code, "checksum": "", "archetypes": archetypes}
        with open(os.path.join(CARD_DIR, set_code + ".json"), "w",
                  encoding="utf-8") as fh:
            json.dump(blob, fh)
        with open(os.path.join(CARD_DIR, set_code + ".loc.json"), "w",
                  encoding="utf-8") as fh:
            json.dump(strings, fh, ensure_ascii=False, indent=0)
    if not args.apply:
        print("\n(dry run - pass --apply to write carddata/)")
    return 0


def art_backed_sets():
    """Set codes we hold numbered card art for."""
    index = os.path.join(HERE, "bundle_index.json")
    if not os.path.exists(index):
        return set()
    with open(index, encoding="utf-8") as fh:
        idx = json.load(fh)
    out = set()
    for bundle, assets in idx.items():
        if "_wp_" in bundle:
            continue
        code = bundle.split("_")[0]
        if not re.fullmatch(r"(SM|SWSH)\d+", code):
            continue
        if any(re.fullmatch(r"\d{1,3}[a-z]?", a) for a in assets):
            out.add(code)
    return out


if __name__ == "__main__":
    sys.exit(main())
