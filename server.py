"""
Local WARG server for the discontinued PTCGO client (v2.95.0.5815).

Implements the gateway + session + digest-login handshake that the client
performs before it will leave the login screen.

Protocol (recovered from core.dll / pie-src.dll):

  Frame:  [int32 BE length][uint32 BE requestID][uint32 BE flags][payload]
          length counts requestID + flags + payload, i.e. payload = length - 8

  Payload: UTF-8 JSON, enveloped as {"name": "<ClassName>", "value": {...}}

  Flow:
    gateway :39389   C-> RequestConnectionServiceWithVersion {clientVersion}
                     S-> ConnectionService {connectionEndPoint "host:port"}
    game    :39390   C-> RequestSession {connectionInfo{...}}
                     S-> GrantedSession {version, serverTime, options, session}
                     C-> RequestLogin
                     S-> RequestedAuthType {validAuthTypes ["sha1"]}
                     C-> StartAuthentication {authType "sha1"}
                     S-> RequestedUsername {}
                     C-> RequestSaltForUser {username}
                     S-> DigestSalt {salt}
                     C-> AuthenticateDigest {username, digest}
                     S-> AuthenticationSuccessful {account, sessionID}

  digest = sha1_hex(password + ":" + salt)

Both listeners are TLS. The client accepts self-signed and expired certs
(CertificateValidator is constructed with allowSelfSigned/allowExpired true)
but still enforces hostname match, so certs/server.crt carries SANs for
127.0.0.1, localhost and tcgo-gateway.direwolfdigital.com.
"""

import hashlib
import json
import logging
import os
import socket
import sqlite3
import ssl
import random
import struct
import threading
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
CERT = os.path.join(HERE, "certs", "server.crt")
KEY = os.path.join(HERE, "certs", "server.key")

BIND_HOST = "0.0.0.0"
GATEWAY_PORT = 39389          # hardcoded in the client (pie-src: d.A = 39389)
GAME_PORT = 39390             # our choice; handed to the client via ConnectionService
ADVERTISED_HOST = "127.0.0.1"  # must match a SAN on the cert

# RawFlags bits
FLAG_COMPRESSED = 0x01
FLAG_PROTOBUF = 0x02
FLAG_PINGPONG = 0x04
FLAG_CONNECTION_ERROR = 0x10
FLAG_ACK_REQUEST = 0x20

HEADER = struct.Struct(">III")

log = logging.getLogger("ptcgo")


# --------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------

def recv_exact(sock, n):
    """Read exactly n bytes, or return None on clean EOF."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    head = recv_exact(sock, 12)
    if head is None:
        return None
    length, request_id, flags = HEADER.unpack(head)
    if length < 8:
        raise ValueError("bad frame length %d" % length)
    body = recv_exact(sock, length - 8) if length > 8 else b""
    if body is None:
        return None
    return request_id, flags, body


def write_frame(sock, obj, request_id=0, flags=0):
    payload = b"" if obj is None else json.dumps(obj, separators=(",", ":")).encode("utf-8")
    sock.sendall(HEADER.pack(len(payload) + 8, request_id, flags) + payload)


def msg(name, value=None):
    """Build the client's {"name":..., "value":...} envelope."""
    return {"name": name, "value": value}


# --------------------------------------------------------------------------
# protobuf envelope
# --------------------------------------------------------------------------
#
# A few replies are protobuf rather than JSON (frame flag 0x02). The client
# wraps every one in dwd.Protobuf.ProtoMessage:
#
#     field 1 (string)  messageName - full .NET type name in ProtobufMessages
#     field 2 (varint)  messageTag  - field number holding the body
#     field <tag>       the serialized message itself
#
# ProtoMessage declares only fields 1 and 2, so the body arrives as a
# protobuf-net "extension" and is read back with Extensible.TryGetValue at
# that tag. The tag is chosen by the writer; any non-declared field works.
#
# Only enough protobuf is implemented to emit an envelope around an empty
# body, which is valid for messages whose fields are all repeated/optional.

PROTO_TAG = 100


def _varint(n):
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def _key(field, wire):
    return _varint((field << 3) | wire)


def _len_field(field, payload):
    return _key(field, 2) + _varint(len(payload)) + payload


def _zigzag(n):
    return (n << 1) ^ (n >> 31) if n >= 0 else ((-n) << 1) - 1


def _fixed64(n):
    return n.to_bytes(8, "little")


def pb_uuid(guid):
    """dwd.Protobuf.UUID - two required fixed64 halves of a GUID."""
    raw = guid.bytes
    return (_key(1, 1) + _fixed64(int.from_bytes(raw[8:], "big")) +
            _key(2, 1) + _fixed64(int.from_bytes(raw[:8], "big")))


def pb_object_int(n):
    """dwd.Protobuf.Object holding an INT (objectType 5)."""
    return _key(1, 0) + _varint(5) + _key(6, 0) + _varint(n)


def pb_attribute(key, value_obj):
    """dwd.Protobuf.Attribute - name is ZigZag-encoded."""
    return (_key(1, 0) + _varint(_zigzag(key)) +
            _len_field(2, value_obj))


# P.F.M, resolved from the scenario constructor's IL: the int attribute the
# client casts to O.g.League (NONE=0, Gold=1, Platinum=2, CityChampionship=3).
ATTR_LEAGUE_ORDER = 201420


def pb_scenario(guid, league_order):
    attrs = pb_attribute(ATTR_LEAGUE_ORDER, pb_object_int(league_order))
    return _len_field(1, pb_uuid(guid)) + _len_field(2, attrs)


def build_all_scenarios():
    """dwd.Protobuf.Progression.AllScenarios.

    determineLeagueAvailability() indexes the league map by Gold/Platinum/
    CityChampionship without checking, so those three roots must exist or the
    client throws KeyNotFoundException and drops the connection. Roots are
    also looked up in the scenario dictionary built from available +
    unavailable, so each root is listed in both places.

    fields: 1=completed 2=available 3=unavailable 4=roots
    """
    roots = []
    for order in (1, 2, 3):          # Gold, Platinum, CityChampionship
        guid = uuid.uuid5(uuid.NAMESPACE_DNS, "ptcgo-local.league.%d" % order)
        roots.append(pb_scenario(guid, order))
    body = b""
    for r in roots:
        body += _len_field(2, r)     # available
    for r in roots:
        body += _len_field(4, r)     # roots
    return body


# --------------------------------------------------------------------------
# card database
# --------------------------------------------------------------------------
#
# carddata/*.json is exported from the BinaryFormatter archetype blobs the
# client ships in StreamingAssets (see tools/export notes in README). Each
# archetype is {lo, hi, attrs:[{n, v}]} where v is a dwd.Protobuf.Object.

CARD_DIR = os.path.join(HERE, "carddata")
_cards = None
_cards_by_set = None


def load_cards():
    """Load carddata/, both flattened and grouped by set.

    The client asks for archetypes one set at a time:
        GetArchetypeListKeys      -> ArchetypeKeys {keys: [set names]}
        GetProtobufArchetypesList -> ArchetypesFound {archetypes, checksum, key}
    which lines up exactly with the per-set files the export produced.

    Archetype IDs must be unique across sets: the client does
    dictionary.Add(archetypeID, ...) per archetype and that throws on a
    duplicate key, which would abort the whole load.
    """
    global _cards, _cards_by_set
    if _cards is not None:
        return _cards
    _cards, _cards_by_set = [], {}
    if not os.path.isdir(CARD_DIR):
        log.error("no carddata/ directory - collection will be empty")
        return _cards
    seen = set()
    dupes = 0
    for fn in sorted(os.listdir(CARD_DIR)):
        if not fn.endswith(".json"):
            continue
        with open(os.path.join(CARD_DIR, fn), encoding="utf-8") as fh:
            data = json.load(fh)
        key = data.get("set") or fn[:-5]
        unique = []
        for a in data.get("archetypes") or []:
            ident = (a["lo"], a["hi"])
            if ident in seen:
                dupes += 1
                continue
            seen.add(ident)
            unique.append(a)
        _cards_by_set[key] = unique
        _cards.extend(unique)
    log.info("loaded %d archetypes across %d sets%s", len(_cards),
             len(_cards_by_set),
             (" (%d cross-set duplicates dropped)" % dupes) if dupes else "")
    return _cards


def set_keys():
    load_cards()
    return sorted(_cards_by_set)


_set_bodies = {}


def build_set_archetypes(key):
    """dwd.Protobuf.Collection.ArchetypesFound: 1=archetypes 2=checksum 3=key"""
    if key not in _set_bodies:
        load_cards()
        body = b""
        for a in _cards_by_set.get(key, []):
            body += _len_field(1, pb_archetype(a))
        body += _len_field(2, CARD_CHECKSUM.encode())
        body += _len_field(3, key.encode("utf-8"))
        _set_bodies[key] = body
    return _set_bodies[key]


def uuid_to_guid_str(lo, hi):
    """Mirror of ProtobufExtensions.ToGuid - UUID(lo,hi) -> GUID string.

    Must match exactly, or the archetype IDs in CollectionCount won't line up
    with the archetypes the client built from the protobuf UUIDs.
    """
    c = hi & 0xFFFF
    b = (hi >> 16) & 0xFFFF
    a = (hi >> 32) & 0xFFFFFFFF
    tail = [(lo >> s) & 0xFF for s in (56, 48, 40, 32, 24, 16, 8, 0)]
    return "%08x-%04x-%04x-%02x%02x-%s" % (
        a, b, c, tail[0], tail[1], "".join("%02x" % x for x in tail[2:]))


def pb_uuid_lohi(lo, hi):
    return _key(1, 1) + _fixed64(lo) + _key(2, 1) + _fixed64(hi)


# dwd.Protobuf.Object.Type
OBJ_ARRAY, OBJ_DICT, OBJ_STRING = 1, 2, 3
OBJ_BOOL, OBJ_INT, OBJ_FLOAT, OBJ_UUID = 4, 5, 6, 7


def pb_object(o):
    """Encode a dwd.Protobuf.Object from the exported JSON form."""
    if o is None:
        return _key(1, 0) + _varint(0)          # UNKNOWN
    out = _key(1, 0) + _varint(o.get("t", 0))
    for e in o.get("a", []):                    # arrayValue
        out += _len_field(2, pb_object(e))
    for kv in o.get("d", []):                   # dictionaryValue
        k = (kv.get("k") or "").encode("utf-8")
        pair = _len_field(1, k) + _len_field(2, pb_object(kv.get("v")))
        out += _len_field(3, pair)
    if "s" in o and o["s"] is not None:
        out += _len_field(4, o["s"].encode("utf-8"))
    if o.get("b"):
        out += _key(5, 0) + _varint(1)
    if o.get("i"):
        out += _key(6, 0) + _varint(o["i"] & 0xFFFFFFFF)
    if o.get("f"):
        out += _key(7, 5) + struct.pack("<f", o["f"])
    if o.get("g"):
        out += _len_field(8, pb_uuid_lohi(o["g"][0], o["g"][1]))
    return out


def pb_archetype(a):
    out = _len_field(1, pb_uuid_lohi(a["lo"], a["hi"]))
    for at in a.get("attrs", []):
        out += _len_field(2, pb_attribute(at["n"], pb_object(at.get("v"))))
    return out


_all_archetypes_body = None


def build_all_archetypes():
    """dwd.Protobuf.Collection.AllArchetypesFound: 1=archetypes 2=checksum."""
    global _all_archetypes_body
    if _all_archetypes_body is None:
        body = b""
        for a in load_cards():
            body += _len_field(1, pb_archetype(a))
        body += _len_field(2, CARD_CHECKSUM.encode())
        _all_archetypes_body = body
        log.info("built AllArchetypesFound payload: %d bytes", len(body))
    return _all_archetypes_body


CARD_CHECKSUM = "ptcgo-local-1"

AVATARS_PATH = os.path.join(HERE, "avatars.json")
_avatar_archetypes_body = None

# The equipped avatar. 201310 is what AddDecks() looks for when deciding which
# deck becomes DefaultAvatarDeck; 201300 marks the deck as an avatar one.
AVATAR_DECK_ID = "a5f0d0c2-1111-4a00-9000-0000000000a1"
ATTR_AVATAR_IS_DEFAULT, ATTR_AVATAR_IS_AVATAR = 201310, 201300
ATTR_GROUP, ATTR_GENDER, ATTR_DEFAULT_ITEM = 200890, 10220, 200940
AVATAR_PILE = "AvatarPile"
AVATAR_GENDER = "Female"
_avatar_items = None
_avatar_decks = {}


def _archetype_guid(a):
    """The exported lo/hi halves back into the GUID string decks use.

    Validated against a client-written deck in decks.json: its pile entries
    decode to BW9 #67 Absol and a Free Darkness Energy, which is what that
    deck actually contains.
    """
    return str(uuid.UUID(bytes=a["hi"].to_bytes(8, "big")
                         + a["lo"].to_bytes(8, "big")))


def build_avatar_deck(want_gender=AVATAR_GENDER, randomize=False,
                      deck_id=None):
    """One equipped avatar, without which the wardrobe screen cannot open.

    AddDecks() records DefaultAvatarDeck only for a deck carrying 201310, and
    AvatarBuilderController.Awake() then calls AvatarUtil.Gender() on it
    unguarded. Replying with no decks therefore throws in Awake, so Start()
    never runs, so everything Start would have initialised - CurrentAvatarModel
    included - stays null. That is why the screen rendered nothing AND why
    clicking a category raised a second, unrelated-looking NullReference: one
    cause, two symptoms.

    The pile carries one item per wardrobe slot, all of a single gender. The
    first entry's gender becomes CurrentItemGender and the category filter then
    shows only items matching it, so a mixed-gender deck would hide half the
    wardrobe. Items are chosen by attribute 200940, the catalogue's own
    per-slot default, rather than by picking arbitrarily.

    randomize picks a different item per slot instead, which is what the
    Random button and the gender switch both ask for.
    """
    global _avatar_items
    if _avatar_items is None:
        if not os.path.exists(AVATARS_PATH):
            log.warning("no avatars.json - avatar builder will not open")
            _avatar_items = []
        else:
            with open(AVATARS_PATH, encoding="utf-8") as fh:
                archetypes = json.load(fh).get("archetypes") or []
            _avatar_items = []
            for a in archetypes:
                at = dict((x["n"], (x.get("v") or {})) for x in a["attrs"])
                group = at.get(ATTR_GROUP, {}).get("s")
                gender = at.get(ATTR_GENDER, {}).get("s")
                if group and gender:
                    _avatar_items.append((
                        gender, group, _archetype_guid(a),
                        bool(at.get(ATTR_DEFAULT_ITEM, {}).get("b"))))
    if not _avatar_items:
        return None

    by_group = {}
    for gender, group, guid, is_default in _avatar_items:
        if gender == want_gender:
            by_group.setdefault(group, []).append((guid, is_default))
    if not by_group:
        return None

    pile = []
    for group, items in sorted(by_group.items()):
        if randomize:
            pile.append(random.choice(items)[0])
        else:
            defaults = [g for g, d in items if d]
            pile.append(defaults[0] if defaults else items[0][0])
    return {
        "deckID": deck_id or AVATAR_DECK_ID,
        "deckName": "Avatar",
        "attributes": [
            {"name": ATTR_AVATAR_IS_DEFAULT, "value": True,
             "originalValue": True},
            {"name": ATTR_AVATAR_IS_AVATAR, "value": True,
             "originalValue": True},
        ],
        "piles": {AVATAR_PILE: pile},
    }


def equipped_avatar_deck(gender=AVATAR_GENDER):
    """The deck handed over at login, cached per gender."""
    if gender not in _avatar_decks:
        _avatar_decks[gender] = build_avatar_deck(gender)
        deck = _avatar_decks[gender]
        if deck:
            log.info("built %s avatar deck: %d items",
                     gender, len(deck["piles"][AVATAR_PILE]))
    return _avatar_decks[gender]


def build_avatar_archetypes():
    """dwd.Protobuf.cake.item.AllAvatarArchetypesFound: 1=archetypes 2=checksum.

    Identical field layout to AllArchetypesFound, so pb_archetype encodes it
    unchanged - only the message type name differs.

    Avatar wardrobe items arrive through this message and nowhere else; the
    AvatarItems *set* carries only the two shop pack products. Answering with an
    empty body (which is what this did for a long time) leaves the wardrobe
    empty, and the client then never requests a single avatar asset - so the
    symptom looks like missing art when it is really a missing reply.

    avatars.json is reconstructed by tools/build_avatar_catalog.py. If it is
    absent we fall back to the old empty reply, which is also the kill switch:
    rename the file and restart if the client ever chokes on the catalog.
    """
    global _avatar_archetypes_body
    if _avatar_archetypes_body is None:
        body, count = b"", 0
        if os.path.exists(AVATARS_PATH):
            with open(AVATARS_PATH, encoding="utf-8") as fh:
                data = json.load(fh)
            for a in data.get("archetypes") or []:
                body += _len_field(1, pb_archetype(a))
                count += 1
        else:
            log.warning("no avatars.json - avatar wardrobe will be empty")
        body += _len_field(2, CARD_CHECKSUM.encode())
        _avatar_archetypes_body = body
        log.info("built AllAvatarArchetypesFound payload: %d archetypes, %d bytes",
                 count, len(body))
    return _avatar_archetypes_body

# How many of each card to grant. 4 is the deck-building limit for most cards.
CARDS_PER_ARCHETYPE = 4

_collection = None


def build_collection():
    """CollectionCount[] granting CARDS_PER_ARCHETYPE of every archetype.

    archetypeID is a TypedID, which serializes as a plain GUID string.
    """
    global _collection
    if _collection is None:
        _collection = [
            {
                "archetypeID": uuid_to_guid_str(a["lo"], a["hi"]),
                "tradable": CARDS_PER_ARCHETYPE,
                "nontradable": 0,
            }
            for a in load_cards()
        ]
        log.info("built collection: %d archetypes x %d",
                 len(_collection), CARDS_PER_ARCHETYPE)
    return _collection


_family_map = None

# Attribute 200260 is the evolution family id, confirmed against the data:
# family 27 is Pikachu / Raichu / AlolanRaichu / RaichuGX / RaichuBREAK,
# family 84 is Charmander / Charmeleon / Charizard.
ATTR_FAMILY, ATTR_STAGE = 200260, 200540


def build_family_map():
    """familyMap: {family id: {PokemonStage: [archetypeID, ...]}}.

    This one is not optional either, and it fails harder than the legality
    list did. PreviousEvolutionsOwned() does

        get_archUtil().get_ArchetypeIDsByFamily()[num].ContainsKey(...)

    where the OUTER lookup is unguarded, so any Pokemon whose family id is
    missing from the map throws KeyNotFoundException. That happens inside the
    deck builder's collection filter, which aborts the whole list rebuild -
    so the symptom is not one broken card but a deck you cannot add anything
    to at all.

    Building from carddata means every family id the client can ask about is
    present by construction.

    Stage keys are the enum member names (Basic, Stage1, Stage2, Break...).
    carddata stores exactly those strings in attribute 200540, which is what
    the original server sent, so that is the wire form.
    """
    global _family_map
    if _family_map is None:
        families = {}
        for a in load_cards():
            attrs = {x["n"]: (x.get("v") or {}) for x in a["attrs"]}
            family = attrs.get(ATTR_FAMILY, {}).get("i")
            stage = attrs.get(ATTR_STAGE, {}).get("s")
            if family is None or not stage:
                continue
            families.setdefault(str(family), {}).setdefault(stage, []).append(
                uuid_to_guid_str(a["lo"], a["hi"]))
        _family_map = families
        log.info("built family map: %d families, %d archetypes placed",
                 len(families),
                 sum(len(v) for f in families.values() for v in f.values()))
    return _family_map


# --------------------------------------------------------------------------
# decks
# --------------------------------------------------------------------------
#
# The client sends CakeSaveDeck with a SerializableDeck:
#
#   {"deckID": <guid>, "deckName": str,
#    "attributes": [{"name": int, "value": guid, "originalValue": guid}],
#    "piles": {"CakePile": [archetypeID, ...]}}
#
# deckID is the zero GUID for a new deck; the server is what assigns a real
# one. The reply is DeckSaved {deckID, deck, validationResults}, and
# SaveDeckToServer.handle() just stores those and finishes, so an empty
# validation array means "no problems" and is safe.
#
# Decks live in decks.json next to this file. Keeping them as the exact
# structure the client sent means GetDeckList can hand them straight back
# without a second format to keep in step.

DECKS_PATH = os.path.join(HERE, "decks.json")
ZERO_GUID = "00000000-0000-0000-0000-000000000000"

# validationResults must NOT be empty, and this is the second time an empty
# array has looked harmless and not been. The deck builder's DeckSaved
# coroutine does, in effect:
#
#     foreach (r in validation) if (r.Valid) list.Add(r);
#     if (list.Count > 0) {
#         r2 = validation.FirstOrDefault(x => x.FormatName == <primary>);
#         if (r2 != null && r2.Valid)  ... proceed ...
#         else { ... may set flag2 = false ... }
#     }
#     if (!flag2) yield break;          // never reaches HandleSaveDeckCleanup
#
# so with nothing valid the save completes on the server, the deck is written
# to disk, and the UI simply never advances - exactly the "sits in the deck
# builder" symptom.
#
# The format name it looks for is an encrypted string constant, so rather than
# decrypt it we answer for every format name the client knows about (the
# DeckType and FormatType enums, unioned). FirstOrDefault then matches
# whichever one it wanted. Extra names are inert: every lookup in that
# coroutine is a by-name search, not an iteration over what we sent.
DECK_FORMAT_NAMES = ("Standard", "Modified", "Expanded", "Legacy", "Unlimited",
                     "ThemeDeck", "TrainerChallengeDeck", "Intermediate",
                     "Unfinished")


def deck_validation(deck_id):
    """Every format, valid, no failure details - consistent with legality."""
    return [
        {
            "deckID": deck_id,
            "format": ZERO_GUID,
            "formatName": name,
            "valid": True,
            "results": [],
        }
        for name in DECK_FORMAT_NAMES
    ]

_decks = None
_decks_lock = threading.Lock()


def load_decks():
    global _decks
    if _decks is None:
        try:
            with open(DECKS_PATH, encoding="utf-8") as fh:
                _decks = json.load(fh)
            log.info("loaded %d saved deck(s) from %s", len(_decks), DECKS_PATH)
        except FileNotFoundError:
            _decks = []
        except Exception as exc:
            # A corrupt file must not cost the session; start clean and say so
            # rather than failing the save that is about to happen.
            log.error("could not read %s (%s) - starting with no decks",
                      DECKS_PATH, exc)
            _decks = []
    return _decks


def store_decks():
    """Write atomically so an interrupted save cannot truncate the file."""
    tmp = DECKS_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_decks, fh, indent=1)
        os.replace(tmp, DECKS_PATH)
    except Exception as exc:
        log.error("could not save decks: %s", exc)


# --------------------------------------------------------------------------
# products
# --------------------------------------------------------------------------

ATTR_SET, ATTR_CARD_NAME, ATTR_CARD_NUM = 200580, 200630, 200780
PACK_SIZE = 10                     # a real booster; bundles get the same

_by_guid = None


def archetype_by_guid(guid):
    """Archetype lookup by the GUID string the client sends."""
    global _by_guid
    if _by_guid is None:
        _by_guid = {uuid_to_guid_str(a["lo"], a["hi"]): a for a in load_cards()}
    return _by_guid.get(guid)


def pack_contents(set_key):
    """Cards a product from this set opens into.

    Only archetypes carrying a collector number count, which keeps products
    themselves - packs, bundles, deck boxes, all of which live in the same set
    files - out of the contents. Without that filter a pack could contain
    another pack.
    """
    if not set_key:
        return []
    load_cards()                      # populates _cards_by_set
    pool = [a for a in (_cards_by_set or {}).get(set_key, [])
            if {x["n"]: (x.get("v") or {}) for x in a["attrs"]}
            .get(ATTR_CARD_NUM, {}).get("i") is not None]
    if not pool:
        return []
    return random.sample(pool, min(PACK_SIZE, len(pool)))


_family_names = None

ATTR_NAME_KEY = 10140

# Lowest stage first: the family is named after what it evolves from, so
# family 27 reads "Pikachu" and family 84 "Charmander".
STAGE_ORDER = ["Basic", "Restored", "Stage1", "Stage2", "Break", "LevelUp",
               "Legend", "VMAX", "VSTAR", "VUNION"]


def build_pokemon_family_names():
    """pokemonFamilyMap: {family id: LocalizableText}, i.e. {"id": <key>}.

    The third unguarded lookup in this chain. w.D.get_PokemonFamilyString()
    is just

        return n.j.A[this.A];

    and n.j.A is assigned straight from this message, so a missing family id
    throws. It is called from DeckCoallationUtil.BuildPokemonFamilyMap, which
    runs from CakeDeckBuilderDeckDataSource.Update() - every frame - so an
    empty map means the deck panel can never draw. Cards go into the deck
    (the 4-copy limit still fires) but nothing about it renders.

    Attribute 10140 holds the card's name key, wrapped as "$$$...$$$".
    LocalizableText's constructor trims '$', so the wrapper is harmless, but
    the keys are stored mixed-case while the localization table is lowercase -
    and the table is what we serve, so lowercase is what the client will have.
    """
    global _family_names
    if _family_names is None:
        best = {}
        for a in load_cards():
            attrs = {x["n"]: (x.get("v") or {}) for x in a["attrs"]}
            family = attrs.get(ATTR_FAMILY, {}).get("i")
            raw = attrs.get(ATTR_NAME_KEY, {}).get("s")
            stage = attrs.get(ATTR_STAGE, {}).get("s")
            if family is None or not raw:
                continue
            rank = (STAGE_ORDER.index(stage) if stage in STAGE_ORDER
                    else len(STAGE_ORDER))
            key = raw.strip('"').strip("$").lower()
            if str(family) not in best or rank < best[str(family)][0]:
                best[str(family)] = (rank, key)
        _family_names = {f: {"id": k} for f, (_, k) in best.items()}
        log.info("built pokemon family names: %d families", len(_family_names))
    return _family_names


_format_legality = None

# ArchFormatLegality.FormatLegality is a bool[] indexed by FormatType:
#   0 Standard ("Modified")   1 Expanded   2 Legacy   3 Unlimited
# The client reads 0..2 unconditionally, so the array must have at least
# three entries or it throws IndexOutOfRange inside the message handler.
FORMAT_COUNT = 4

# formatLegalityTime is "legal from this moment", checked as
#   ServerTimeNow() >= WargTime.FromMilliseconds(value)
# but only recorded when the value is >= 0. Sending -1 skips the entry
# entirely, and isTimeLegal() returns true when there is no entry - so the
# legality answer never depends on our clock agreeing with the client's.
NEVER_TIME_LOCKED = -1


def build_format_legality():
    """Every archetype legal in every format.

    A deliberate deviation: real legality was rotation data the server owned,
    and it is gone. Everything-legal is the useful answer for a local sandbox,
    and the alternative - inventing a rotation - would silently make cards
    unplayable for reasons no one could check.
    """
    global _format_legality
    if _format_legality is None:
        _format_legality = [
            {
                "archetypeID": uuid_to_guid_str(a["lo"], a["hi"]),
                "formatLegality": [True] * FORMAT_COUNT,
                "formatLegalityTime": [NEVER_TIME_LOCKED] * FORMAT_COUNT,
            }
            for a in load_cards()
        ]
        log.info("built format legality: %d archetypes, all formats",
                 len(_format_legality))
    return _format_legality


def protobuf_message(type_name, body=b""):
    """Wrap `body` in a ProtoMessage naming `type_name`."""
    name = type_name.encode("utf-8")
    return (
        _key(1, 2) + _varint(len(name)) + name +
        _key(2, 0) + _varint(PROTO_TAG) +
        _key(PROTO_TAG, 2) + _varint(len(body)) + body
    )


def parse(body):
    """Return (name, value) from a client frame body."""
    if not body:
        return None, None
    try:
        obj = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None, None
    if isinstance(obj, dict) and "name" in obj:
        return obj.get("name"), obj.get("value")
    return None, obj


# --------------------------------------------------------------------------
# accounts
# --------------------------------------------------------------------------

class Accounts:
    """Flat-file account store. Any unknown username is auto-created."""

    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()
        self.data = {}
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as fh:
                self.data = json.load(fh)

    def get_or_create(self, username):
        with self.lock:
            key = username.lower()
            if key not in self.data:
                self.data[key] = {
                    "username": username,
                    "accountID": str(uuid.uuid4()),
                    "salt": uuid.uuid4().hex,
                    # None = accept any password on first login and pin it then.
                    "password": None,
                }
                self._save()
            return self.data[key]

    def set_password(self, username, password):
        with self.lock:
            self.data[username.lower()]["password"] = password
            self._save()

    def visited_scenes(self, username):
        return self.data[username.lower()].get("visitedScenes") or []

    def add_visited_scene(self, username, scene):
        with self.lock:
            account = self.data[username.lower()]
            scenes = account.setdefault("visitedScenes", [])
            if scene not in scenes:
                scenes.append(scene)
                self._save()
                return True
            return False

    def _save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, indent=2)
        os.replace(tmp, self.path)


ACCOUNTS = Accounts(os.path.join(HERE, "accounts.json"))

# --------------------------------------------------------------------------
# onboarding
# --------------------------------------------------------------------------
#
# The client decides you are a new user by asking its own account attributes:
#
#     VisitedScenesDataProvider.HasVisitedScene(scene):
#         string[] value = userModel.A.Attributes.GetAttribute(P.F.c).get_Value();
#         return value.Contains(scene.ToString());
#
# and it reports visits back with UserHasVisitedScene, which the server is
# meant to persist. We were dropping those and sending "attributes": {}, so
# every login looked like a first login: the account upsell, the "Have Fun!"
# decline dialog, and the forced walk into Trainer Challenge.
#
# P.F.c is the AttributeDefinition<string[]> at 202101. The decompiler folds
# several P.F fields onto the name "c" (they differ only by case), so the id
# is picked by TYPE rather than position: HasVisitedScene calls get_Value()
# into a string[], and 202101 is the only string[] definition named c.
# 201730, 10910, 201545, 10860 and 202200 are the other string[] fields, under
# different names - if 202101 turns out to be wrong, they are the candidates.
ATTR_VISITED_SCENES = 202101

# VisitedScenesDataProvider.VisitedScene, compared by ToString().
VISITED_SCENES = ("TrainerChallenge", "Versus", "Deckbuilder",
                  "TCGoldComplete", "TCLoss", "VersusUpdate")

SCENE_BY_FLAG = {1: "TrainerChallenge", 2: "Versus", 4: "Deckbuilder",
                 8: "TCGoldComplete", 16: "TCLoss", 32: "VersusUpdate"}

# Treat every account as having seen everything. This is a local sandbox with
# one player who has already been through the intro; the onboarding only
# slows down getting to a game.
SEED_ALL_SCENES_VISITED = True


def account_attributes(username):
    """The account attribute list sent at login.

    MutableAttributes deserialises from either an object or an array of
    attribute objects, and each is {name, value, originalValue} - the same
    shape the client uses when it sends deck attributes back to us.
    """
    scenes = list(ACCOUNTS.visited_scenes(username))
    if SEED_ALL_SCENES_VISITED:
        for scene in VISITED_SCENES:
            if scene not in scenes:
                scenes.append(scene)
    return [{
        "name": ATTR_VISITED_SCENES,
        "value": scenes,
        "originalValue": scenes,
    }]


# --------------------------------------------------------------------------
# set data
# --------------------------------------------------------------------------

# The client blocks on a "loading data from the server" bar until it receives
# SetDataList, so this has to be answered before anything else happens.
#
# The card sets are already on disk: the client ships a per-hostname archetype
# cache under StreamingAssets, one binary file per set. Deriving the list from
# those filenames keeps the server honest about what content actually exists
# instead of inventing a set list.

GAME_DIR = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "StreamingAssets",
)

# Files in the archetype folder that are not card sets.
NOT_A_SET = {"keys.bin"}


def discover_sets():
    """Build SetDataList entries from the shipped archetype cache."""
    for host_dir in ("127.0.0.1", "tcgo-gateway.direwolfdigital.com"):
        path = os.path.join(GAME_DIR, host_dir)
        if os.path.isdir(path):
            break
    else:
        log.error("no archetype folder under %s - sending empty set list", GAME_DIR)
        return []

    names = sorted(n for n in os.listdir(path)
                   if n not in NOT_A_SET
                   and os.path.isfile(os.path.join(path, n)))

    sets = []
    for i, name in enumerate(names):
        sets.append({
            "name": name,
            "externalId": name,
            "number": i,
            "count": 0,
            "filter": True,
            # arrays must not be null - the client indexes them directly
            "legalFormats": [],
            "featuredArchetypes": [],
            "visibleUnfilterable": False,
            "promo": name.startswith("Promo"),
            "block": "",
        })
    log.info("discovered %d sets in %s", len(sets), path)
    return sets


SET_DATA = None  # built on first use, after logging is configured


# --------------------------------------------------------------------------
# localization
# --------------------------------------------------------------------------
#
# The client ships a prebuilt LocalizationDB-UTF16.db with ~27,500 strings,
# but it is stamped user_version=3 while this build's database config expects
# 4. PieDB.Init wipes any database whose user_version does not match, so the
# prebuilt is ALWAYS discarded - it is stale for this client version. The live
# database is recreated empty on every launch, which is why the UI has no
# labels.
#
# So the strings have to come from the server, which is how it worked
# originally: LookupLocaleRepository.Write() pushes them into an in-memory
# LocalizationLookup. Read the prebuilt database directly and serve its
# contents as a single release.

LOC_DB = os.path.join(GAME_DIR, "LocalizationDB-UTF16.db")
LOC_RELEASE = "ptcgo-local"

_loc_cache = None


def load_localization():
    """Read key/value pairs out of the client's prebuilt localization DB."""
    global _loc_cache
    if _loc_cache is not None:
        return _loc_cache
    if not os.path.exists(LOC_DB):
        log.error("no localization DB at %s - UI labels will be missing", LOC_DB)
        _loc_cache = []
        return _loc_cache
    try:
        con = sqlite3.connect("file:%s?mode=ro" % LOC_DB.replace("\\", "/"),
                              uri=True)
        rows = con.execute("select key, value from Lookup").fetchall()
        con.close()
    except sqlite3.Error as exc:
        log.error("could not read localization DB: %s", exc)
        _loc_cache = []
        return _loc_cache
    _loc_cache = [{"key": k, "value": v} for k, v in rows]
    log.info("loaded %d localization strings", len(_loc_cache))
    return _loc_cache


# --------------------------------------------------------------------------
# gateway listener
# --------------------------------------------------------------------------

def handle_gateway(sock, peer):
    frame = read_frame(sock)
    if frame is None:
        return
    request_id, flags, body = frame
    name, value = parse(body)
    log.info("[gateway %s] <- %s %s", peer, name, value)

    if name != "RequestConnectionServiceWithVersion":
        log.warning("[gateway %s] unexpected first message %r", peer, name)

    endpoint = "%s:%d" % (ADVERTISED_HOST, GAME_PORT)
    reply = msg("ConnectionService", {"connectionEndPoint": endpoint})
    log.info("[gateway %s] -> ConnectionService %s", peer, endpoint)
    write_frame(sock, reply, request_id)


# --------------------------------------------------------------------------
# game listener
# --------------------------------------------------------------------------

class GameSession:
    def __init__(self, sock, peer):
        self.sock = sock
        self.peer = peer
        self.session_id = str(uuid.uuid4())
        self.username = None
        self.account = None
        self.authenticated = False

    def send(self, name, value=None, request_id=0, flags=0):
        log.info("[game %s] -> %s %s", self.peer, name, value)
        write_frame(self.sock, msg(name, value), request_id, flags)

    def run(self):
        while True:
            frame = read_frame(self.sock)
            if frame is None:
                log.info("[game %s] client closed", self.peer)
                return
            request_id, flags, body = frame

            if flags & FLAG_PINGPONG:
                write_frame(self.sock, msg("Pong"), request_id, FLAG_PINGPONG)
                continue

            name, value = parse(body)
            if name == "Ping":
                write_frame(self.sock, msg("Pong"), request_id, FLAG_PINGPONG)
                continue

            log.info("[game %s] <- %s %s", self.peer, name, value)
            self.dispatch(name, value, request_id)

    def dispatch(self, name, value, request_id):
        handler = getattr(self, "on_" + name, None) if name else None
        if handler is None:
            log.warning("[game %s] no handler for %r", self.peer, name)
            return
        handler(value, request_id)

    # -- session ---------------------------------------------------------

    def on_RequestSession(self, value, request_id):
        self.send("GrantedSession", {
            "version": "2.95.0.5815",
            "serverTime": int(time.time() * 1000),
            # every option defaults to off when absent; keep the socket simple
            "options": {},
            "session": self.session_id,
        }, request_id)

    def on_ReconnectSession(self, value, request_id):
        self.on_RequestSession(value, request_id)

    # Do NOT advertise "DeviceID" here. It is the guest path: a device login
    # makes the client mark the account as one, which is exactly what drives
    # the "Make Your Free Account Today!" upsell, the "Have Fun!" decline
    # dialog and the upgrade-account button. The client reports that flag to
    # analytics under the name "DeviceIDAcct", which is how it was identified.
    #
    # Offering only sha1 makes the client use the username/password form, and
    # a full account has none of that. Accounts here are auto-created and the
    # password is pinned on first use, so any username and password work.
    AUTH_TYPES = ["sha1"]

    def on_RequestLogin(self, value, request_id):
        self.send("RequestedAuthType",
                  {"validAuthTypes": self.AUTH_TYPES}, request_id)

    # -- post-login data -------------------------------------------------

    def on_GetSetData(self, value, request_id):
        log.info("[game %s] -> SetDataList (%d sets)", self.peer, len(SET_DATA))
        write_frame(self.sock, msg("SetDataList", {"setDataList": SET_DATA}),
                    request_id)

    def on_GetAllLocalizationReleases(self, value, request_id):
        # Serve the whole string table as one release. The client cannot use
        # its own prebuilt DB (see load_localization), so this is what puts
        # labels on the UI. Sending nothing here also raises 2700200
        # (LOCALE_NOT_SUPPORTED_BY_SERVER), because the in-memory lookup
        # starts with no checksums.
        locale = (value or {}).get("locale") or "en_US"
        strings = load_localization()
        digest = hashlib.md5(
            ("%s:%d" % (LOC_RELEASE, len(strings))).encode()).hexdigest()
        log.info("[game %s] -> AllLocalizationReleases (%d strings)",
                 self.peer, len(strings))
        write_frame(self.sock, msg("AllLocalizationReleases", {
            "locale": locale,
            "releases": {
                LOC_RELEASE: {"md5": digest, "localizationList": strings},
            },
            "version": "1",
        }), request_id)

    # Empty-but-valid replies to the data the client requests right after
    # login. Arrays must be present and non-null - the client iterates them
    # directly. A fresh account legitimately has no decks, currency or
    # notifications, so empty is correct here rather than a stub.
    def on_GetWallet(self, value, request_id):
        self.send("CurrentWallet", {"currencies": []}, request_id)

    def on_GetDeckList(self, value, request_id):
        decks = load_decks()
        log.info("[game %s] -> OnlineDecksFound (%d decks)",
                 self.peer, len(decks))
        write_frame(self.sock, msg("OnlineDecksFound", {"decks": decks}),
                    request_id)

    def on_CakeSaveDeck(self, value, request_id):
        deck = dict((value or {}).get("deck") or {})
        if not deck:
            log.warning("[game %s] CakeSaveDeck with no deck", self.peer)
            self.send("DeckSaveFailed", {"deckID": ZERO_GUID}, request_id)
            return

        deck_id = deck.get("deckID") or ZERO_GUID
        if deck_id == ZERO_GUID:
            deck_id = str(uuid.uuid4())      # a new deck; we assign the id
        deck["deckID"] = deck_id

        with _decks_lock:
            decks = load_decks()
            for i, existing in enumerate(decks):
                if existing.get("deckID") == deck_id:
                    decks[i] = deck
                    break
            else:
                decks.append(deck)
            store_decks()

        cards = len((deck.get("piles") or {}).get("CakePile") or [])
        log.info("[game %s] saved deck %r (%s, %d cards) - %d total",
                 self.peer, deck.get("deckName"), deck_id, cards,
                 len(load_decks()))
        write_frame(self.sock, msg("DeckSaved", {
            "deckID": deck_id,
            "deck": deck,
            "validationResults": deck_validation(deck_id),
        }), request_id)

    def _item(self, archetype_guid, name=None):
        """dwd.core.collection.Item."""
        return {
            "itemID": str(uuid.uuid4()),
            "ownerID": self.account["accountID"] if self.account else ZERO_GUID,
            "archetypeID": archetype_guid,
            "lockID": None,
            "created": int(time.time() * 1000),
            "isTradable": True,
            "name": name or "",
            "invoiceID": None,
            "attributes": [],
        }

    def on_OpenProductsByArchetypeID(self, value, request_id):
        # Opening a pack. The client shows the pack-opening sequence when
        # Products[0] has one, and otherwise a plain "you received" dialog
        # listing Items - so both arrays have to be populated or nothing
        # happens.
        #
        # Contents are drawn from the product's own set (attribute 200580):
        # a TK7 bundle opens TK7A cards, an XY6 booster opens XY6 cards. Only
        # archetypes with a collector number are eligible, so a pack can never
        # contain another pack.
        products = (value or {}).get("products") or []
        if not products:
            self.send("ProductsOpenedFailure",
                      {"error": {"id": "no product specified"}}, request_id)
            return

        opened, consumed = [], []
        for guid in products:
            product = archetype_by_guid(guid)
            if product is None:
                log.warning("[game %s] unknown product %s", self.peer, guid)
                continue
            attrs = {x["n"]: (x.get("v") or {}) for x in product["attrs"]}
            set_key = attrs.get(ATTR_SET, {}).get("s")
            consumed.append(self._item(guid, attrs.get(ATTR_NAME_KEY, {}).get("s")))
            for card in pack_contents(set_key):
                cattrs = {x["n"]: (x.get("v") or {}) for x in card["attrs"]}
                opened.append(self._item(
                    uuid_to_guid_str(card["lo"], card["hi"]),
                    cattrs.get(ATTR_CARD_NAME, {}).get("s")))

        if not consumed:
            self.send("ProductsOpenedFailure",
                      {"error": {"id": "unknown product"}}, request_id)
            return

        log.info("[game %s] opened %d product(s) -> %d card(s)",
                 self.peer, len(consumed), len(opened))
        write_frame(self.sock, msg("ProductsOpened", {
            "accountID": self.account["accountID"] if self.account else ZERO_GUID,
            "items": opened,
            "products": consumed,
            "additionalData": {},
        }), request_id)

    def on_ValidateDecks(self, value, request_id):
        # Sent before testing or playing a deck. DecksValidated carries the
        # same DeckValidationResult[] the save reply uses, and the handler
        # just hands them to DeckValidationManager.updateValidations, so one
        # valid result per format per deck is all it needs.
        decks = (value or {}).get("decks") or []
        results = []
        for deck in decks:
            results.extend(deck_validation(deck.get("deckID") or ZERO_GUID))
        log.info("[game %s] -> DecksValidated (%d deck(s), %d results)",
                 self.peer, len(decks), len(results))
        write_frame(self.sock, msg("DecksValidated", {"results": results}),
                    request_id)

    def on_CakeDeleteDeck(self, value, request_id):
        deck_id = (value or {}).get("deckID")
        with _decks_lock:
            decks = load_decks()
            kept = [d for d in decks if d.get("deckID") != deck_id]
            if len(kept) == len(decks):
                log.warning("[game %s] delete: no deck %s", self.peer, deck_id)
                self.send("DeleteDeckFailed", {"deckID": deck_id}, request_id)
                return
            decks[:] = kept
            store_decks()
        log.info("[game %s] deleted deck %s (%d left)",
                 self.peer, deck_id, len(load_decks()))
        self.send("DeckDeleted", {"deckID": deck_id}, request_id)

    def on_GetAvatarDeckList(self, value, request_id):
        deck = equipped_avatar_deck()
        self.send("OnlineAvatarDecksFound",
                  {"decks": [deck] if deck else []}, request_id)

    def on_GetRandomAvatarDeck(self, value, request_id):
        """Random, and also how the gender switch works.

        Not answering this is not a cosmetic gap: the Random button disables
        the NGUI input camera and only re-enables it in the reply handler, so
        an unanswered request locks the entire UI. The gender switch sends the
        same message, because the opposite-gender model does not exist client
        side until the server supplies a deck for it.
        """
        req = value or {}
        gender = req.get("gender") or AVATAR_GENDER
        deck = build_avatar_deck(gender, randomize=True,
                                 deck_id=req.get("deckID"))
        if deck is None:                       # never leave the UI camera off
            deck = {"deckID": req.get("deckID") or AVATAR_DECK_ID,
                    "deckName": "Avatar", "attributes": [],
                    "piles": {AVATAR_PILE: []}}
        log.info("[game %s] -> RequestedRandomAvatarDeck (%s, %d items)",
                 self.peer, gender, len(deck["piles"][AVATAR_PILE]))
        self.send("RequestedRandomAvatarDeck", {"deck": deck}, request_id)

    def on_GetNotifications(self, value, request_id):
        self.send("NotificationsRequested", {"notificationList": []}, request_id)

    def on_GetActiveTournaments(self, value, request_id):
        self.send("ActiveAsyncTournaments", {
            "tournamentDefinitions": [],
            "tournamentProgress": [],
            "claimedLeaderboard": {},
        }, request_id)

    def on_GetArchetypeListKeys(self, value, request_id):
        # These keys drive the whole card load: the client requests archetypes
        # once per key, and waits for exactly len(keys)+1 responses (the +1 is
        # the avatar list). An empty array here means no cards, ever.
        keys = set_keys()
        log.info("[game %s] -> ArchetypeKeys (%d sets)", self.peer, len(keys))
        self.send("ArchetypeKeys", {"keys": keys}, request_id)

    def on_GetProtobufArchetypesList(self, value, request_id):
        key = (value or {}).get("key") or ""
        body = build_set_archetypes(key)
        n = len(_cards_by_set.get(key, []))
        log.info("[game %s] -> [protobuf] ArchetypesFound %s (%d cards, %d bytes)",
                 self.peer, key, n, len(body))
        self.send_protobuf("dwd.Protobuf.Collection.ArchetypesFound",
                           body, request_id)

    def on_GetArchetypeIDsByFamily(self, value, request_id):
        families = build_family_map()
        log.info("[game %s] -> ArchetypeIDsByFamily (%d families)",
                 self.peer, len(families))
        write_frame(self.sock,
                    msg("ArchetypeIDsByFamily", {"familyMap": families}),
                    request_id)

    def on_GetFormatLegalityForArchetypes(self, value, request_id):
        # Must not be empty. FormatLegalityProvider.handle() reads indexes
        # 0, 1 and 2 of every entry unconditionally, then sets Initialized =
        # true whether or not the list had anything in it. An empty list
        # therefore leaves the provider "ready" with empty dictionaries, so
        # IsModifiedLegal/IsExpandedLegal/IsLegacyLegal answer false for every
        # card in the game - which is what makes the deck builder look empty
        # and warns on every card you try to add.
        legality = build_format_legality()
        log.info("[game %s] -> FormatLegalityForArchetypes (%d entries)",
                 self.peer, len(legality))
        write_frame(self.sock,
                    msg("FormatLegalityForArchetypes",
                        {"archLegality": legality}),
                    request_id)

    def send_protobuf(self, type_name, body=b"", request_id=0):
        log.info("[game %s] -> [protobuf] %s", self.peer, type_name)
        payload = protobuf_message(type_name, body)
        self.sock.sendall(
            HEADER.pack(len(payload) + 8, request_id, FLAG_PROTOBUF) + payload)

    def on_GetProtobufScenarios(self, value, request_id):
        self.send_protobuf("dwd.Protobuf.Progression.AllScenarios",
                           build_all_scenarios(), request_id)

    def on_GetProtobufAllAvatarArchetypesList(self, value, request_id):
        self.send_protobuf("dwd.Protobuf.cake.item.AllAvatarArchetypesFound",
                           build_avatar_archetypes(), request_id)

    def on_GetProtobufAllArchetypesList(self, value, request_id):
        # If the client's on-disk cache is the one build_cache.py wrote, just
        # confirm it and skip the ~9.7MB transfer entirely.
        if (value or {}).get("checksum") == CARD_CHECKSUM:
            log.info("[game %s] archetype checksum matches disk cache",
                     self.peer)
            self.send("AllArchetypesChecksumMatch", {}, request_id)
            return
        # Otherwise ship the whole card database. Counterpart
        # AllArchetypesFound -> ProtoAllArchetypesFound is registered, so
        # protobuf is correct here.
        body = build_all_archetypes()
        log.info("[game %s] -> [protobuf] AllArchetypesFound (%d cards, %d bytes)",
                 self.peer, len(load_cards()), len(body))
        self.send_protobuf("dwd.Protobuf.Collection.AllArchetypesFound",
                           body, request_id)

    def on_UserHasVisitedScene(self, value, request_id):
        # Not telemetry: this is how the account records that the intro for a
        # screen has been seen, and the client reads it back from its own
        # account attributes on the next login. No reply expected.
        flag = (value or {}).get("scene")
        scene = SCENE_BY_FLAG.get(flag)
        if scene and self.username:
            if ACCOUNTS.add_visited_scene(self.username, scene):
                log.info("[game %s] %s has now visited %s",
                         self.peer, self.username, scene)

    def on_GetCollectionCount(self, value, request_id):
        # JSON, not protobuf. A dwd.Protobuf.Collection.CollectionCountFound
        # type exists, but nothing registers it as a ProtobufCounterpart, so
        # ProtobufProcessor.Convert() hands back the raw protobuf object and
        # WargSocket.Read() throws InvalidCastException casting it to
        # NetworkMessageEvent - which kills the read thread and drops the
        # session. The JSON class is the one the client actually consumes.
        counts = build_collection()
        log.info("[game %s] -> CollectionCountFound (%d entries, %d each)",
                 self.peer, len(counts), CARDS_PER_ARCHETYPE)
        write_frame(self.sock,
                    msg("CollectionCountFound", {"collectionCountList": counts}),
                    request_id)

    def on_GetFeatureStatuses_v2(self, value, request_id):
        # Everything open, nothing closed. "closed" drives the "this feature
        # is unavailable" dialogs, so it must be an empty array, not null.
        self.send("AllFeatureStatuses_v2", {
            "service": (value or {}).get("service") or "cake",
            "open": [],
            "closed": [],
        }, request_id)

    # Older name, in case the client falls back to it.
    on_GetFeatureStatuses = on_GetFeatureStatuses_v2

    def on_GetArchetypeFlags(self, value, request_id):
        self.send("ArchetypeFlagsRequested", {"archetypeFlags": []}, request_id)

    def on_GetDynamicPages(self, value, request_id):
        self.send("DynamicLandingPages", {
            "pageData": [],
            "maintenanceData": [],
        }, request_id)

    def on_ViewMyLots(self, value, request_id):
        # Trade posts owned by this account - none on a fresh server.
        self.send("MyLotsRetrieved", {"lots": [], "offers": []}, request_id)

    def on_GetAllBannedCardsByFormats(self, value, request_id):
        self.send("AllBannedCardsByFormat", {"cards": {}}, request_id)

    def on_GetDynamicVersions(self, value, request_id):
        # This one gates the 90% mark: command a.J sends GetDynamicVersions
        # then spins `while (model == null) yield return null;` until a
        # DynamicVersions model lands in the data manager. Without a reply the
        # loading screen never advances.
        self.send("DynamicVersions", {"versionData": {}}, request_id)

    def on_SetDeckShareMode(self, value, request_id):
        # Fire-and-forget from the deck builder; the client does not wait for
        # anything back. Handled only so it stops being logged as unhandled.
        pass

    def on_GetThemeDeckContents(self, value, request_id):
        # ThemeDeckContentsMap.themeDeckContentsMap is
        # Dictionary<ArchetypeID, ArchetypeID[]>, read by lookup, so an empty
        # map is safe - it just means no theme deck has known contents.
        self.send("ThemeDeckContentsMap", {"themeDeckContentsMap": {}},
                  request_id)

    def on_GetArchetypeCorrections(self, value, request_id):
        self.send("ArchetypeCorrections", {"correctionMap": {}}, request_id)

    def on_GetTimeLockedArchetypes(self, value, request_id):
        self.send("TimeLockedArchetypes", {
            "currentServerTime": int(time.time() * 1000),
            "timeLockedArchetypes": {},
        }, request_id)

    def on_GetGuidOverride(self, value, request_id):
        # Command A.s blocks until either NoGuidOverride or CurrentGuidOverride
        # arrives. No bundle guid override here, so say so.
        self.send("NoGuidOverride", {}, request_id)

    def on_GetMotd(self, value, request_id):
        self.send("NoMotdSet", {}, request_id)

    def on_GetPokemonFamilyMap(self, value, request_id):
        names = build_pokemon_family_names()
        log.info("[game %s] -> PokemonFamilyMap (%d families)",
                 self.peer, len(names))
        write_frame(self.sock,
                    msg("PokemonFamilyMap", {"pokemonFamilyMap": names}),
                    request_id)

    def on_QuestsEnabled(self, value, request_id):
        # Client telling the server it opted into quests; no reply expected.
        pass

    def on_SetClientSetting(self, value, request_id):
        # Client pushing a preference at us; nothing waits on a reply.
        pass

    def on_IsUserInActiveTournament(self, value, request_id):
        # Fire-and-forget on the client; nothing waits on a reply.
        pass

    def on_LogClientError(self, value, request_id):
        # Client-side crash report. Nothing to reply; log it, it is useful.
        log.warning("[game %s] client error: %s", self.peer,
                    (value or {}).get("reason"))

    # -- auth ------------------------------------------------------------

    def on_StartAuthentication(self, value, request_id):
        auth_type = (value or {}).get("authType")
        if auth_type == "sha1":
            self.send("RequestedUsername", {}, request_id)
        elif auth_type == "DeviceID":
            self.send("RequestDeviceID", {}, request_id)
        else:
            self.send("AuthenticationError",
                      {"reason": {"text": "unsupported auth type %r" % auth_type}},
                      request_id)

    def on_AuthenticateDeviceID(self, value, request_id):
        device_id = (value or {}).get("deviceID") or "unknown-device"
        # Treat the device id as the account name for guest / auto-login.
        self.username = "device-" + device_id[:8]
        self.account = ACCOUNTS.get_or_create(self.username)
        self._succeed(self.username, request_id)

    def on_RequestSaltForUser(self, value, request_id):
        self.username = (value or {}).get("username") or "player"
        self.account = ACCOUNTS.get_or_create(self.username)
        self.send("DigestSalt", {"salt": self.account["salt"]}, request_id)

    def on_AuthenticateDigest(self, value, request_id):
        value = value or {}
        username = value.get("username") or self.username
        digest = (value.get("digest") or "").lower()

        if self.account is None:
            self.account = ACCOUNTS.get_or_create(username)

        salt = self.account["salt"]
        stored = self.account["password"]

        if stored is None:
            # First login for this account: trust the client and remember
            # whatever password produced this digest is not recoverable, so
            # just pin the digest itself.
            ACCOUNTS.set_password(username, digest)
            ok = True
        else:
            ok = (digest == stored)

        if not ok:
            log.warning("[game %s] bad password for %s", self.peer, username)
            self.send("AuthenticationFailed",
                      {"reason": {"text": "Incorrect password."}}, request_id)
            return

        self._succeed(username, request_id)

    def on_AuthenticateCASTicket(self, value, request_id):
        # The client got a ticket from our SSO stand-in. There is nothing to
        # validate against - we issued it - so accept and log in.
        value = value or {}
        username = value.get("accountName") or self.username or "player"
        self.account = ACCOUNTS.get_or_create(username)
        log.info("[game %s] CAS ticket accepted for %s", self.peer, username)
        self._succeed(username, request_id)

    def on_UpgradeDeviceAccount(self, value, request_id):
        # Sent instead of AuthenticateCASTicket when the client is already in
        # a device (guest) account. Replying with supplementaryInfo
        # "AccountUpgradeSuccessful" is what clears the guest flag: the login
        # handler takes a branch that sets it false, which is what stops the
        # upsell and the "Have Fun!" dialog.
        value = value or {}
        username = value.get("username") or self.username or "player"
        self.account = ACCOUNTS.get_or_create(username)
        log.info("[game %s] device account upgraded to %s", self.peer, username)
        self._succeed(username, request_id,
                      supplementary="AccountUpgradeSuccessful")

    def _succeed(self, username, request_id, supplementary=None):
        self.authenticated = True
        # Pin it here rather than relying on whichever auth path got us here,
        # so later handlers (visited scenes) always know who this is.
        self.username = username
        log.info("[game %s] AUTH OK for %s", self.peer, username)
        self.send("AuthenticationSuccessful", {
            "account": {
                "username": username,
                "accountID": self.account["accountID"],
                # Must be present and non-null: the client wraps this in
                # ReadOnlyAttributes and reads user flags straight off it.
                # Passing null throws NullReferenceException in the user-flags
                # constructor. It is no longer empty - see account_attributes,
                # which is what tells the client this is not a brand-new user.
                "attributes": account_attributes(username),
            },
            "sessionID": self.session_id,
            # AuthenticationSuccessful.supplementaryInfo. "AccountUpgradeSuccessful"
            # makes the login handler take the branch that clears the guest
            # flag; None on an ordinary login leaves the normal path alone.
            "supplementaryInfo": supplementary,
        }, request_id)

        # EulaViewMediator reads a EulaData state that defaults to NONE, which
        # pops "No EULA data received!". The server is expected to volunteer
        # the account's legal status after login; Successful means already
        # accepted, so nothing is shown.
        self.send("EulaSuccessful", {"version": 1})
        self.send("PrivacyPolicySuccessful", {"version": 1})

    # Anything the client asks for after login lands here via dispatch()
    # and is logged, so the next stage can be built from real traffic.


def handle_game(sock, peer):
    GameSession(sock, peer).run()


# --------------------------------------------------------------------------
# TLS plumbing
# --------------------------------------------------------------------------

def make_ssl_context():
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT, KEY)
    # Unity's Mono offers TLS 1.0-1.2; allow the whole range and relax
    # OpenSSL 3's default security level so the older suites stay available.
    ctx.minimum_version = ssl.TLSVersion.TLSv1
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=0")
    except ssl.SSLError:
        ctx.set_ciphers("DEFAULT:@SECLEVEL=1")
    return ctx


def serve(port, handler, label):
    ctx = make_ssl_context()
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((BIND_HOST, port))
    srv.listen(16)
    log.info("%s listening on %s:%d (TLS)", label, BIND_HOST, port)

    while True:
        raw, addr = srv.accept()
        peer = "%s:%d" % addr
        threading.Thread(target=_serve_one,
                         args=(ctx, raw, peer, handler, label),
                         daemon=True).start()


def _serve_one(ctx, raw, peer, handler, label):
    try:
        raw.settimeout(30)
        conn = ctx.wrap_socket(raw, server_side=True)
        conn.settimeout(None)
    except (ssl.SSLError, OSError) as exc:
        log.error("[%s %s] TLS handshake failed: %s", label, peer, exc)
        raw.close()
        return

    log.info("[%s %s] connected (%s)", label, peer, conn.version())
    try:
        handler(conn, peer)
    except (ConnectionResetError, ssl.SSLEOFError):
        log.info("[%s %s] connection reset", label, peer)
    except Exception:
        log.exception("[%s %s] handler error", label, peer)
    finally:
        try:
            conn.close()
        except OSError:
            pass


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )
    if not (os.path.exists(CERT) and os.path.exists(KEY)):
        raise SystemExit("missing certs/server.crt or certs/server.key")

    global SET_DATA
    SET_DATA = discover_sets()

    # Warm the expensive caches now rather than on the first request - the
    # archetype payload takes several seconds to build.
    load_localization()
    for _k in set_keys():
        build_set_archetypes(_k)
    build_collection()

    import asset_server
    threading.Thread(target=asset_server.serve, daemon=True).start()
    threading.Thread(target=serve, args=(GAME_PORT, handle_game, "game"),
                     daemon=True).start()
    serve(GATEWAY_PORT, handle_gateway, "gateway")


if __name__ == "__main__":
    main()
