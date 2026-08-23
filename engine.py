"""
Rules engine for the local PTCGO server.

The shipped client is a pure renderer: it holds no rules at all, so every
legality decision and every state transition has to come from here. This
module is deliberately isolated from server.py - no sockets, no protocol
envelopes, no import of the server - because the only way to trust a rules
engine is to be able to run it thousands of times with no client attached.

The engine answers exactly two questions:

    legal_actions(state, player) -> [Action, ...]
    apply(state, action)         -> (new_state, [Change, ...])

`Change` is the seam. The engine says "card 41 moved from hand to bench slot 3"
and never says how that reaches the wire; the protocol layer turns Changes into
client messages. Actions carry the card's real `abilityID` GUID, because that
is the identifier the client sends and expects echoed back.

State is treated as immutable: apply() deep-copies and returns a new state, so
callers can keep history, replay, or search without defensive copying. A state
is a few hundred ints and the card database refuses to be copied (CardDB
implements __deepcopy__ as identity), so this costs far less than it looks.

Scope is a theme-deck game: Basic/Stage1/Stage2 Pokemon, Energy, plain-damage
attacks, Special Conditions. Per-card text is *modelled but inert* - an attack
with game text still deals its printed damage, it just does not do the extra
thing the text describes, and Trainers cannot be played at all. The seam for
growing past that is Rules.attack_effects; see EXTENSION POINTS at the bottom.

Card data is carddata/*.json - the same files server.py serves to the client -
read through the ATTR_* ids below. Nothing here invents an attribute id.
"""

from __future__ import annotations

import copy
import itertools
import json
import os
import random
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping, Optional

# --------------------------------------------------------------------------
# archetype attribute ids
# --------------------------------------------------------------------------
#
# Verified against carddata/*.json. Values are dwd.Protobuf.Objects: a type tag
# `t` plus one of s (string), i (int), b (bool), a (array), g (guid halves).
# An int attribute whose value is zero is written as {"t": 5} with no "i", so
# "absent" and "zero" are the same thing - that is why _int() defaults to 0,
# and why a retreat cost of 0 never appears explicitly in the data.

ATTR_ARCHETYPE_ID = 10000
ATTR_CARD_TYPES = 200300         # "Pokemon" | "LegendHalf" | "TrainerCard" | "Energy"
ATTR_STAGE = 200540              # "Basic" | "Stage1" | "Stage2" | "Restored" | ...
ATTR_HP = 200490                 # printed HP; at runtime this is the max, and
                                 # damage is (originalValue - value)
ATTR_POKEMON_TYPES = 200570
ATTR_WEAKNESS_TYPES = 200590
ATTR_WEAKNESS_OPERATOR = 200660    # "x" (7243 cards) or "+" (exactly one card)
ATTR_WEAKNESS_AMOUNT = 200820      # 2 for every "x", 10 for the one "+"
ATTR_RESISTANCE_TYPE = 200600      # "NoColor" when the card has no resistance
ATTR_RESISTANCE_OPERATOR = 200650  # always "-" where a resistance exists
ATTR_RESISTANCE_AMOUNT = 200830    # always 20 where a resistance exists
ATTR_RETREAT_COST = 200800
ATTR_SPECIAL_CONDITIONS = 200340   # runtime-only: no archetype in carddata has it
ATTR_ABILITIES = 200740            # array of JSON *strings*, one object each
ATTR_ENERGY_PROVIDED = 201040      # JSON string {"options": [[type, ...], ...]}
ATTR_IS_BASIC_ENERGY = 200520
ATTR_FAMILY_ID = 200260
ATTR_CARD_NAME = 200630
ATTR_EVOLVES_FROM = 200640         # the *name* (ATTR_CARD_NAME) of the pre-evolution
ATTR_SET = 200580
ATTR_COLLECTOR_NUMBER = 200780
ATTR_TRAINER_TYPES = 200270        # "Item" | "Supporter" | "Stadium" | "PokemonTool"

# --------------------------------------------------------------------------
# vocabulary
# --------------------------------------------------------------------------

CARD_TYPE_POKEMON = "Pokemon"
CARD_TYPE_TRAINER = "TrainerCard"
CARD_TYPE_ENERGY = "Energy"
CARD_TYPE_LEGEND_HALF = "LegendHalf"

COLORLESS = "Colorless"
NO_COLOR = "NoColor"          # the data's way of writing "no weakness/resistance"

ABILITY_ATTACK = "Attack"

# Stage progression we are prepared to evolve through. Break/LevelUp/Legend/
# VMAX/VUNION/VSTAR exist in the data but are out of scope, and leaving them
# out of this map is exactly what makes evolving into them illegal.
STAGE_BASIC = "Basic"
STAGE_ORDER = {"Basic": 0, "Restored": 0, "Stage1": 1, "Stage2": 2}

ASLEEP = "Asleep"
BURNED = "Burned"
CONFUSED = "Confused"
PARALYZED = "Paralyzed"
POISONED = "Poisoned"
SPECIAL_CONDITIONS = (ASLEEP, BURNED, CONFUSED, PARALYZED, POISONED)

# Asleep/Paralyzed/Confused occupy the same "slot" and replace each other;
# Burned and Poisoned sit alongside them and alongside each other.
EXCLUSIVE_CONDITIONS = (ASLEEP, PARALYZED, CONFUSED)

# Zone names are server.py's zone strings verbatim, so a Change can be handed
# to the protocol layer without a translation table. k.P.introduce() throws on
# any zone name it does not recognise, so these are exact.
ZONE_DECK = "deck"
ZONE_HAND = "hand"
ZONE_PRIZES = "prizePile"
ZONE_ACTIVE = "activePokemonArea"
ZONE_BENCH = "bench"
ZONE_DISCARD = "discard"
ZONE_LOST = "lostZone"

PHASE_SETUP = "setup"
PHASE_MAIN = "main"
PHASE_GAME_OVER = "gameOver"

WINNER_TIE = "tie"

# Reasons attached to the gameOver Change, so the protocol layer can pick an
# end-of-game localization key without re-deriving why the game ended.
WIN_PRIZES = "prizes"
WIN_NO_POKEMON = "noPokemonInPlay"
WIN_DECK_OUT = "deckOut"


class IllegalAction(Exception):
    """apply() refused an action. legal_actions() is advisory; this is law."""


# --------------------------------------------------------------------------
# rules assumptions
# --------------------------------------------------------------------------
#
# Every place the real TCG is ambiguous, or where PTCGO's era-specific rules
# differ, is a named field here rather than a number buried in the code. The
# defaults are the Sun & Moon-era rules, the newest ruleset this client shipped
# with; a BW/XY-era table is a different Rules(), not a different engine.

@dataclass(frozen=True)
class Rules:
    bench_size: int = 5
    hand_size: int = 7
    prize_count: int = 6

    # SM-era: the player going first does not draw on their first turn. Under
    # BW/XY they did. This is the only thing the first player skips.
    first_player_draws_on_first_turn: bool = False

    # Also SM-era: the first player may not attack on turn 1. (They *may* play
    # a Supporter, which is moot here since Trainers are out of scope.)
    first_player_may_attack_on_first_turn: bool = False

    # A Pokemon cannot evolve the turn it was put into play. Setup placements
    # are recorded as played on that player's own turn 1, which is what makes
    # the separate "nobody evolves on the first turn of the game" rule fall out
    # with no second check anywhere.
    setup_play_turn: int = 1

    # Between-turns (Pokemon Checkup) numbers.
    poison_damage: int = 10
    burn_damage: int = 20
    burn_flip_removes: bool = True     # heads between turns removes Burned
    sleep_flip_wakes: bool = True      # heads between turns removes Asleep
    confusion_self_damage: int = 30    # tails when attacking while Confused

    # Modern rules let a Confused Pokemon retreat freely; only attacking needs
    # the coin flip. Older rulings required a flip to retreat as well.
    confusion_blocks_retreat: bool = False

    # Prizes per knockout. Cards with rule boxes (EX/GX/V/VMAX) are worth 2 or
    # 3, but carddata has no *verified* attribute for "has a rule box", so this
    # engine invents nothing and always takes one.
    prizes_per_knockout: int = 1

    energy_attachments_per_turn: int = 1
    retreats_per_turn: int = 1

    # Guards rather than rules: a deck with no Basic Pokemon would mulligan for
    # ever, and enumerating retreat payments over a huge pile of Energy is
    # pointless work for a theme-deck game.
    max_mulligans: int = 100
    max_enumerated_energy: int = 10

    # abilityID -> callable(state, context, changes), called after an attack's
    # damage lands. This is where per-card text goes when someone writes it.
    # Empty by default: attacks are inert beyond their printed damage.
    attack_effects: Mapping[str, Callable[..., None]] = field(default_factory=dict)


DEFAULT_RULES = Rules()


# --------------------------------------------------------------------------
# card database
# --------------------------------------------------------------------------

def archetype_guid(archetype: Mapping[str, Any]) -> str:
    """The exported lo/hi halves back into the GUID decks and the client use.

    Same construction as server.py's _archetype_guid, duplicated rather than
    imported so this module has no dependency on the server at all.
    """
    return str(uuid.UUID(bytes=archetype["hi"].to_bytes(8, "big")
                         + archetype["lo"].to_bytes(8, "big")))


def _attrs(archetype: Mapping[str, Any]) -> dict:
    return {a["n"]: (a.get("v") or {}) for a in archetype.get("attrs") or []}


def _str(attrs, ident, default=None):
    return attrs.get(ident, {}).get("s", default)


def _int(attrs, ident, default=0):
    # A zero-valued int attribute is serialised with no "i" key at all, so
    # missing has to mean zero or every 0-retreat Pokemon reads as unknown.
    value = attrs.get(ident, {}).get("i")
    return default if value is None else value


def _bool(attrs, ident, default=False):
    value = attrs.get(ident, {}).get("b")
    return default if value is None else value


def _strings(attrs, ident) -> tuple:
    """An attribute that is either a string array or a bare string.

    CardTypes and TrainerTypes are documented as arrays but appear in the
    export as scalars, so both shapes are accepted rather than guessed at.
    """
    value = attrs.get(ident)
    if not value:
        return ()
    if "a" in value:
        return tuple(e["s"] for e in value["a"] if isinstance(e, dict) and "s" in e)
    if "s" in value:
        return (value["s"],)
    return ()


@dataclass(frozen=True)
class Ability:
    """One entry of ATTR_ABILITIES, already parsed out of its JSON string.

    ability_id is the card's real abilityID GUID and is preserved verbatim:
    the client sends it to say which attack was chosen, so it has to survive
    the round trip through the engine untouched.
    """
    ability_id: str
    ability_type: str
    title: str
    game_text: str
    cost: Mapping[str, int]
    damage: int
    # The printed suffix on damage: "30+" and "20x" mean the real number
    # depends on card text we do not implement, so we deal the base only.
    amount_operator: str

    @property
    def is_attack(self) -> bool:
        return self.ability_type == ABILITY_ATTACK

    @property
    def has_unimplemented_text(self) -> bool:
        """True if this attack does more than deal its printed base damage.

        Nothing in the engine consumes this; it exists so the protocol layer
        (or a future effects pass) can tell which attacks are being resolved
        approximately rather than silently trusting the number.
        """
        return bool(self.game_text) or bool(self.amount_operator)


@dataclass(frozen=True)
class Card:
    """A read-only view of one archetype, in engine terms rather than attrs."""
    guid: str
    name: str
    card_types: tuple
    stage: Optional[str]
    max_hp: int
    types: tuple
    weakness_types: tuple
    weakness_operator: str
    weakness_amount: int
    resistance_type: Optional[str]
    resistance_operator: str
    resistance_amount: int
    retreat_cost: int
    abilities: tuple
    energy_options: tuple
    is_basic_energy: bool
    evolves_from: Optional[str]
    family_id: Optional[int]
    set_code: Optional[str]
    collector_number: Optional[int]
    trainer_types: tuple

    @classmethod
    def from_archetype(cls, archetype: Mapping[str, Any]) -> "Card":
        at = _attrs(archetype)

        abilities = []
        for entry in (at.get(ATTR_ABILITIES) or {}).get("a") or []:
            raw = entry.get("s") if isinstance(entry, dict) else None
            if not raw:
                continue
            obj = json.loads(raw)
            abilities.append(Ability(
                ability_id=obj.get("abilityID") or "",
                ability_type=obj.get("abilityType") or "",
                title=obj.get("title") or "",
                game_text=obj.get("gameText") or "",
                cost=dict(obj.get("cost") or {}),
                damage=int(obj.get("damage") or 0),
                amount_operator=obj.get("amountOperator") or "",
            ))

        energy_options = ()
        raw_energy = _str(at, ATTR_ENERGY_PROVIDED)
        if raw_energy:
            options = json.loads(raw_energy).get("options") or []
            energy_options = tuple(tuple(o) for o in options)

        # "NoColor" is the data's null, not a real type; normalising it here
        # keeps every downstream weakness/resistance check a plain membership
        # test instead of a special case at each call site.
        weakness = tuple(t for t in _strings(at, ATTR_WEAKNESS_TYPES)
                         if t and t != NO_COLOR)
        resistance = _str(at, ATTR_RESISTANCE_TYPE)
        if resistance == NO_COLOR:
            resistance = None

        return cls(
            guid=archetype_guid(archetype),
            name=_str(at, ATTR_CARD_NAME) or "",
            card_types=_strings(at, ATTR_CARD_TYPES),
            stage=_str(at, ATTR_STAGE),
            max_hp=_int(at, ATTR_HP),
            types=_strings(at, ATTR_POKEMON_TYPES),
            weakness_types=weakness,
            weakness_operator=_str(at, ATTR_WEAKNESS_OPERATOR, "") or "",
            weakness_amount=_int(at, ATTR_WEAKNESS_AMOUNT),
            resistance_type=resistance,
            resistance_operator=_str(at, ATTR_RESISTANCE_OPERATOR, "") or "",
            resistance_amount=_int(at, ATTR_RESISTANCE_AMOUNT),
            retreat_cost=_int(at, ATTR_RETREAT_COST),
            abilities=tuple(abilities),
            energy_options=energy_options,
            is_basic_energy=_bool(at, ATTR_IS_BASIC_ENERGY),
            evolves_from=_str(at, ATTR_EVOLVES_FROM),
            family_id=at.get(ATTR_FAMILY_ID, {}).get("i"),
            set_code=_str(at, ATTR_SET),
            collector_number=at.get(ATTR_COLLECTOR_NUMBER, {}).get("i"),
            trainer_types=_strings(at, ATTR_TRAINER_TYPES),
        )

    # -- classification ----------------------------------------------------
    #
    # ATTR_ENERGY_PROVIDED and CardTypes == "Energy" cover exactly the same 369
    # archetypes, so either test alone would do; both are used because 1066
    # archetypes (avatar items, rewards) carry no CardTypes at all and must not
    # be mistaken for anything playable.

    @property
    def is_energy(self) -> bool:
        return CARD_TYPE_ENERGY in self.card_types or bool(self.energy_options)

    @property
    def is_pokemon(self) -> bool:
        return CARD_TYPE_POKEMON in self.card_types and not self.is_energy

    @property
    def is_trainer(self) -> bool:
        return CARD_TYPE_TRAINER in self.card_types

    @property
    def is_basic_pokemon(self) -> bool:
        return self.is_pokemon and self.stage == STAGE_BASIC

    @property
    def is_evolution(self) -> bool:
        return self.is_pokemon and STAGE_ORDER.get(self.stage, 0) > 0

    @property
    def attacks(self) -> tuple:
        return tuple(a for a in self.abilities if a.is_attack)

    def attack(self, ability_id: str) -> Optional[Ability]:
        for a in self.attacks:
            if a.ability_id == ability_id:
                return a
        return None

    @property
    def energy_units(self) -> int:
        """How many Energy symbols this card provides, for retreat payment.

        Basic Energy provides one, Double Colorless two. A special Energy whose
        options are [[]] provides none until its text is implemented, and so
        cannot pay for anything - which is the correct conservative answer.
        """
        return max((len(o) for o in self.energy_options), default=0)


class CardDB:
    """Archetype GUID -> Card, built once and shared by every state.

    Deliberately immutable and deliberately un-copyable: apply() deep-copies
    the whole GameState, and a 9,940-card database being copied once per action
    would make that unusable. __deepcopy__ returning self is the whole trick.
    """

    def __init__(self, cards):
        self._by_guid = {}
        self._by_name = {}
        for card in cards:
            self._by_guid[card.guid] = card
            self._by_name.setdefault(card.name, []).append(card)

    @classmethod
    def from_archetypes(cls, archetypes) -> "CardDB":
        return cls(Card.from_archetype(a) for a in archetypes)

    @classmethod
    def from_directory(cls, path=None) -> "CardDB":
        """Load every carddata/*.json; the first definition of a GUID wins.

        Cross-set duplicates exist - server.py drops them for the same reason,
        the client's dictionary.Add throws on a repeated key.
        """
        if path is None:
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "carddata")
        cards = {}
        for name in sorted(os.listdir(path)):
            if not name.endswith(".json"):
                continue
            with open(os.path.join(path, name), encoding="utf-8") as fh:
                data = json.load(fh)
            for archetype in data.get("archetypes") or []:
                card = Card.from_archetype(archetype)
                cards.setdefault(card.guid, card)
        return cls(cards.values())

    def get(self, guid: str) -> Card:
        try:
            return self._by_guid[guid]
        except KeyError:
            raise KeyError("unknown archetype %s" % guid) from None

    def by_name(self, name: str) -> list:
        return list(self._by_name.get(name, ()))

    def __contains__(self, guid) -> bool:
        return guid in self._by_guid

    def __getitem__(self, guid) -> Card:
        return self.get(guid)

    def __len__(self) -> int:
        return len(self._by_guid)

    def __iter__(self) -> Iterator[Card]:
        return iter(self._by_guid.values())

    def __deepcopy__(self, memo):
        return self


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------
#
# Cards are integers. Every physical card in the game gets a card id (cid) that
# never changes and never leaves the state, so a Change can name a card without
# ambiguity even when a deck holds four identical Energy. The protocol layer
# owns the mapping from cid to the client's entityID; the engine never sees it.
#
# A Pokemon in play is a Slot: an evolution stack plus everything sitting on
# it. Slots have their own stable ids for the same reason cards do - a bench
# Pokemon that shifts position when a neighbour is knocked out is still the
# same Pokemon, and a slot id says so where a bench index would not.

@dataclass
class CardInstance:
    cid: int
    archetype: str
    owner: int


@dataclass
class Slot:
    slot_id: int
    stack: list                      # bottom .. top; stack[-1] is the Pokemon
    damage: int = 0
    energy: list = field(default_factory=list)
    tools: list = field(default_factory=list)   # modelled, never populated
    conditions: set = field(default_factory=set)
    # The owner's own turn counter when this Pokemon was played or evolved.
    # Compared against PlayerState.turns_taken, never against the global turn.
    played_on_turn: int = 0

    @property
    def top(self) -> int:
        return self.stack[-1]

    @property
    def cards(self) -> list:
        """Every card that leaves play with this Pokemon when it is KO'd."""
        return list(self.stack) + list(self.energy) + list(self.tools)


@dataclass
class PlayerState:
    index: int
    deck: list = field(default_factory=list)     # deck[0] is the top card
    hand: list = field(default_factory=list)
    discard: list = field(default_factory=list)
    lost: list = field(default_factory=list)
    prizes: list = field(default_factory=list)
    active: Optional[Slot] = None
    bench: list = field(default_factory=list)
    turns_taken: int = 0
    energy_attached_this_turn: int = 0
    retreats_this_turn: int = 0
    setup_done: bool = False
    mulligans: int = 0
    # Set the moment a draw is required and the deck is empty. The loss is
    # recorded rather than raised so both players' losses can land on the same
    # check and produce a tie instead of a race.
    decked_out: bool = False

    @property
    def in_play(self) -> list:
        """Active first, then bench - the order the rules resolve things in."""
        return ([self.active] if self.active else []) + list(self.bench)


@dataclass
class GameState:
    db: CardDB
    rules: Rules
    rng: random.Random
    players: list
    cards: dict = field(default_factory=dict)
    phase: str = PHASE_SETUP
    to_move: int = 0
    turn_number: int = 0
    first_player: int = 0
    # Players who owe a promotion after a knockout, turn player first. While
    # this is non-empty nothing else may happen, including ending the turn.
    pending_promotions: list = field(default_factory=list)
    # What to do once pending_promotions drains: a knockout can interrupt the
    # end-of-turn sequence at two different points, and this remembers which.
    after_promotions: Optional[str] = None
    winner: Any = None

    _next_cid: int = 1
    _next_slot_id: int = 1

    # -- lookups -----------------------------------------------------------

    def card(self, cid: int) -> Card:
        return self.db.get(self.cards[cid].archetype)

    def owner_of(self, cid: int) -> int:
        return self.cards[cid].owner

    def slot(self, slot_id: int):
        """(player index, Slot, is_active) for a slot id, or None."""
        for p in (0, 1):
            ps = self.players[p]
            if ps.active is not None and ps.active.slot_id == slot_id:
                return p, ps.active, True
            for s in ps.bench:
                if s.slot_id == slot_id:
                    return p, s, False
        return None

    def pokemon(self, slot: Slot) -> Card:
        return self.card(slot.top)

    def max_hp(self, slot: Slot) -> int:
        return self.pokemon(slot).max_hp

    @property
    def over(self) -> bool:
        return self.phase == PHASE_GAME_OVER


# --------------------------------------------------------------------------
# actions
# --------------------------------------------------------------------------
#
# Actions are frozen dataclasses, hashable and comparable, so a test can assert
# `expected in legal_actions(...)` and a protocol layer can round-trip one
# through a dict. Every action names the player that performs it; apply() never
# infers the actor from whose turn it is.

@dataclass(frozen=True)
class Action:
    player: int

    def as_dict(self) -> dict:
        d = {"action": type(self).__name__}
        d.update(self.__dict__)
        return d


@dataclass(frozen=True)
class SetupPlaceActive(Action):
    """Put a Basic from hand face down as the Active Pokemon."""
    card: int


@dataclass(frozen=True)
class SetupPlaceBench(Action):
    card: int


@dataclass(frozen=True)
class SetupDone(Action):
    """Finish placing. Prizes are dealt once both players have said this."""


@dataclass(frozen=True)
class PlayBasic(Action):
    card: int


@dataclass(frozen=True)
class Evolve(Action):
    card: int          # the evolution card, in hand
    slot: int          # the slot it is played onto


@dataclass(frozen=True)
class AttachEnergy(Action):
    card: int
    slot: int


@dataclass(frozen=True)
class Retreat(Action):
    """Promote a benched Pokemon, paying the Active's retreat cost.

    `energy` is the exact set of attached Energy cards to discard. It is
    explicit rather than chosen by the engine because the client asks the
    player which Energy to pay with, and two identical-looking Energy can be
    different physical cards to the renderer.
    """
    slot: int          # the benched slot being promoted
    energy: tuple = ()


@dataclass(frozen=True)
class Attack(Action):
    """ability_id is the card's real abilityID GUID, straight from carddata."""
    ability_id: str


@dataclass(frozen=True)
class Promote(Action):
    """Choose a new Active after the old one was knocked out."""
    slot: int


@dataclass(frozen=True)
class Pass(Action):
    """End the turn without attacking."""


# --------------------------------------------------------------------------
# changes
# --------------------------------------------------------------------------

CHANGE_SHUFFLE = "shuffle"
CHANGE_MULLIGAN = "mulligan"
CHANGE_MOVE = "move"
CHANGE_ATTACH = "attach"
CHANGE_EVOLVE = "evolve"
CHANGE_DAMAGE = "damage"
CHANGE_CONDITION = "condition"
CHANGE_KNOCKOUT = "knockout"
CHANGE_PRIZE = "prize"
CHANGE_COIN_FLIP = "coinFlip"
CHANGE_ATTACK = "attack"
CHANGE_RETREAT = "retreat"
CHANGE_PROMOTE = "promote"
CHANGE_TURN_START = "turnStart"
CHANGE_TURN_END = "turnEnd"
CHANGE_PHASE = "phase"
CHANGE_GAME_OVER = "gameOver"


@dataclass(frozen=True)
class Change:
    """One observable thing that happened, in engine terms.

    The common fields are typed because the protocol layer reads them on nearly
    every kind; anything kind-specific goes in `detail` rather than growing the
    struct. A Change never contains a Slot or a Card object, only ids, so it
    stays valid after the state it came from has been superseded.
    """
    kind: str
    player: Optional[int] = None
    card: Optional[int] = None
    slot: Optional[int] = None
    from_zone: Optional[str] = None
    to_zone: Optional[str] = None
    amount: Optional[int] = None
    detail: dict = field(default_factory=dict)

    def __repr__(self) -> str:
        parts = [repr(self.kind)]
        for name in ("player", "card", "slot", "from_zone", "to_zone", "amount"):
            value = getattr(self, name)
            if value is not None:
                parts.append("%s=%r" % (name, value))
        if self.detail:
            parts.append("detail=%r" % (self.detail,))
        return "Change(%s)" % ", ".join(parts)


# --------------------------------------------------------------------------
# energy accounting
# --------------------------------------------------------------------------

def _energy_options(state: GameState, slot: Slot) -> list:
    """Per attached card, the alternative sets of symbols it can provide.

    A basic Fire Energy is [("Fire",)]; a Double Colorless is
    [("Colorless", "Colorless")]; a Rainbow is [("Grass",), ("Fire",), ...].
    An Energy with no implemented text is [()] and pays for nothing.
    """
    out = []
    for cid in slot.energy:
        options = state.card(cid).energy_options
        out.append(list(options) if options else [()])
    return out


def can_pay_cost(option_sets, cost: Mapping[str, int]) -> bool:
    """Can these attached Energy cards satisfy this attack cost?

    Coloured requirements must be paid with symbols of exactly that colour;
    Colorless requirements take anything left over. Within one card's chosen
    option, spending a symbol on a colour that is still owed is never worse
    than putting it in the generic pool, because pool symbols are
    interchangeable - so the only real branching is *which* option a
    multi-option Energy (Rainbow) contributes, and that is searched.
    """
    need = {t: n for t, n in cost.items() if t != COLORLESS and n > 0}
    colorless = int(cost.get(COLORLESS, 0) or 0)
    seen = set()

    def search(index, need, pool):
        if not need and pool >= colorless:
            return True
        if index == len(option_sets):
            return False
        # Optimistic bound: every remaining card gives at most this many symbols.
        remaining = sum(max((len(o) for o in option_sets[i]), default=0)
                        for i in range(index, len(option_sets)))
        if remaining < sum(need.values()) + max(0, colorless - pool):
            return False
        key = (index, tuple(sorted(need.items())), min(pool, colorless))
        if key in seen:
            return False
        seen.add(key)
        for option in option_sets[index]:
            sub, spare = dict(need), pool
            for symbol in option:
                if sub.get(symbol, 0) > 0:
                    sub[symbol] -= 1
                    if not sub[symbol]:
                        del sub[symbol]
                else:
                    spare += 1
            if search(index + 1, sub, spare):
                return True
        return False

    return search(0, need, 0)


def _retreat_payments(state: GameState, slot: Slot, cost: int) -> list:
    """Distinct minimal sets of attached Energy that cover a retreat cost.

    Retreat is paid in Energy *symbols*, not cards, so a Double Colorless pays
    for two. Sets discarding the same archetypes are collapsed: which of two
    identical Fire Energy you discard is not a decision worth surfacing to a
    player, and enumerating it would flood legal_actions().
    """
    if cost <= 0:
        return [()]
    units = {cid: state.card(cid).energy_units for cid in slot.energy}
    usable = [cid for cid in slot.energy if units[cid] > 0]
    if len(usable) > state.rules.max_enumerated_energy:
        usable = usable[:state.rules.max_enumerated_energy]
    seen, out = set(), []
    for size in range(1, len(usable) + 1):
        for combo in itertools.combinations(usable, size):
            total = sum(units[cid] for cid in combo)
            if total < cost:
                continue
            # Minimal: no single card can be dropped and still cover the cost.
            if any(total - units[cid] >= cost for cid in combo):
                continue
            key = tuple(sorted(state.cards[cid].archetype for cid in combo))
            if key in seen:
                continue
            seen.add(key)
            out.append(combo)
    return out


# --------------------------------------------------------------------------
# damage
# --------------------------------------------------------------------------

def damage_after_weakness(attacker: Card, defender: Card, base: int) -> int:
    """Printed damage through the defender's Weakness and Resistance.

    Order is fixed by the rules: Weakness first, then Resistance, and the
    result is floored at zero. Both are keyed on the *attacking* Pokemon's
    types. Zero-damage attacks stay zero - an attack that does nothing is not
    turned into something by a Resistance.
    """
    if base <= 0:
        return 0
    damage = base

    if any(t in defender.weakness_types for t in attacker.types):
        amount = defender.weakness_amount
        if defender.weakness_operator == "x":
            damage *= amount if amount else 2
        elif defender.weakness_operator == "+":
            damage += amount

    if defender.resistance_type and defender.resistance_type in attacker.types:
        if defender.resistance_operator == "-":
            damage -= defender.resistance_amount

    return max(0, damage)


# --------------------------------------------------------------------------
# primitives
# --------------------------------------------------------------------------
#
# Everything below mutates the state it is given. That is safe because apply()
# hands them a private deep copy; nothing here is reachable from public API
# without going through that copy first.

def _flip(state: GameState, changes: list, reason: str, player=None) -> bool:
    """One coin flip, recorded. True is heads."""
    heads = state.rng.random() < 0.5
    changes.append(Change(CHANGE_COIN_FLIP, player=player,
                          detail={"reason": reason,
                                  "result": "heads" if heads else "tails"}))
    return heads


def _new_card(state: GameState, archetype: str, owner: int) -> int:
    cid = state._next_cid
    state._next_cid += 1
    state.cards[cid] = CardInstance(cid, archetype, owner)
    return cid


def _new_slot(state: GameState, cid: int, played_on_turn: int) -> Slot:
    slot = Slot(slot_id=state._next_slot_id, stack=[cid],
                played_on_turn=played_on_turn)
    state._next_slot_id += 1
    return slot


def _draw(state: GameState, player: int, count: int, changes: list) -> int:
    """Draw up to `count`. Sets decked_out if the deck runs out mid-draw."""
    ps = state.players[player]
    drawn = 0
    for _ in range(count):
        if not ps.deck:
            ps.decked_out = True
            break
        cid = ps.deck.pop(0)
        ps.hand.append(cid)
        drawn += 1
        changes.append(Change(CHANGE_MOVE, player=player, card=cid,
                              from_zone=ZONE_DECK, to_zone=ZONE_HAND))
    return drawn


def _add_condition(state: GameState, slot: Slot, condition: str, changes: list):
    """Apply a Special Condition, honouring the mutually-exclusive group.

    Nothing in scope calls this - no in-scope attack inflicts a condition - but
    the whole between-turns machine is built on it, and an effect hook is one
    line away from needing it.
    """
    if condition in EXCLUSIVE_CONDITIONS:
        for other in EXCLUSIVE_CONDITIONS:
            if other != condition and other in slot.conditions:
                slot.conditions.discard(other)
                changes.append(Change(CHANGE_CONDITION, slot=slot.slot_id,
                                      detail={"condition": other, "added": False}))
    if condition not in slot.conditions:
        slot.conditions.add(condition)
        changes.append(Change(CHANGE_CONDITION, slot=slot.slot_id,
                              detail={"condition": condition, "added": True}))


def _clear_conditions(state: GameState, slot: Slot, changes: list, reason: str):
    """Every Special Condition comes off when a Pokemon leaves Active or evolves."""
    for condition in sorted(slot.conditions):
        changes.append(Change(CHANGE_CONDITION, slot=slot.slot_id,
                              detail={"condition": condition, "added": False,
                                      "reason": reason}))
    slot.conditions.clear()


def _apply_damage(state: GameState, slot: Slot, amount: int, changes: list,
                  detail=None):
    if amount <= 0:
        return
    slot.damage += amount
    owner = state.owner_of(slot.top)
    info = {"total": slot.damage, "maxHP": state.max_hp(slot)}
    if detail:
        info.update(detail)
    changes.append(Change(CHANGE_DAMAGE, player=owner, slot=slot.slot_id,
                          amount=amount, detail=info))


def _is_knocked_out(state: GameState, slot: Slot) -> bool:
    return slot.damage >= state.max_hp(slot)


def _take_prize(state: GameState, player: int, changes: list):
    """Take one prize into hand.

    Prizes are face down, so there is no meaningful choice to expose: the pile
    was dealt off an already-shuffled deck, and taking the top of it is the
    same distribution as letting a player point at one. Cards that let you
    choose which prize would need a real action here.
    """
    ps = state.players[player]
    if not ps.prizes:
        return
    cid = ps.prizes.pop(0)
    ps.hand.append(cid)
    changes.append(Change(CHANGE_MOVE, player=player, card=cid,
                          from_zone=ZONE_PRIZES, to_zone=ZONE_HAND))
    changes.append(Change(CHANGE_PRIZE, player=player, card=cid,
                          amount=len(ps.prizes)))


def _discard_slot(state: GameState, owner: int, slot: Slot, changes: list):
    """A knocked-out Pokemon and everything on it goes to its owner's discard."""
    ps = state.players[owner]
    from_zone = ZONE_ACTIVE if (ps.active is slot) else ZONE_BENCH
    for cid in slot.cards:
        ps.discard.append(cid)
        changes.append(Change(CHANGE_MOVE, player=owner, card=cid,
                              slot=slot.slot_id, from_zone=from_zone,
                              to_zone=ZONE_DISCARD))
    if ps.active is slot:
        ps.active = None
    elif slot in ps.bench:
        ps.bench.remove(slot)


def _resolve_knockouts(state: GameState, changes: list):
    """Knock out anything at or past its HP, award prizes, queue promotions.

    Prizes go to the *opponent of the owner*, never to "the attacker": a
    Pokemon that knocks itself out with confusion damage still gives its
    opponent a prize, and so does one that dies to poison between turns.
    """
    order = (state.to_move, 1 - state.to_move)
    knocked = []
    for owner in order:
        for slot in state.players[owner].in_play:
            if _is_knocked_out(state, slot):
                knocked.append((owner, slot))

    for owner, slot in knocked:
        changes.append(Change(CHANGE_KNOCKOUT, player=owner, slot=slot.slot_id,
                              card=slot.top,
                              detail={"damage": slot.damage,
                                      "maxHP": state.max_hp(slot)}))
        _discard_slot(state, owner, slot, changes)
        for _ in range(state.rules.prizes_per_knockout):
            _take_prize(state, 1 - owner, changes)

    # Only queue a promotion for someone who can actually make one. A player
    # with no Active and no bench has lost, and _check_game_end says so.
    for owner in order:
        ps = state.players[owner]
        if ps.active is None and ps.bench and owner not in state.pending_promotions:
            state.pending_promotions.append(owner)


def _check_game_end(state: GameState, changes: list) -> bool:
    """Evaluate all three win conditions at once, so simultaneous ones tie.

    Deliberately not gated on pending promotions: taking your last prize wins
    immediately, even though the player you just knocked out still owes a
    promotion. The "no Pokemon in play" test cannot misfire mid-promotion
    because a player with a bench to promote from fails its `not them.bench`.
    """
    if state.phase != PHASE_MAIN:
        return False

    winners, reasons = set(), {}
    for p in (0, 1):
        me, them = state.players[p], state.players[1 - p]
        if not me.prizes:
            winners.add(p)
            reasons.setdefault(p, WIN_PRIZES)
        if them.active is None and not them.bench:
            winners.add(p)
            reasons.setdefault(p, WIN_NO_POKEMON)
        if them.decked_out:
            winners.add(p)
            reasons.setdefault(p, WIN_DECK_OUT)

    if not winners:
        return False
    state.winner = WINNER_TIE if len(winners) == 2 else next(iter(winners))
    state.phase = PHASE_GAME_OVER
    changes.append(Change(CHANGE_GAME_OVER,
                          player=None if state.winner == WINNER_TIE else state.winner,
                          detail={"winner": state.winner,
                                  "reasons": {p: reasons[p] for p in sorted(winners)}}))
    return True


# --------------------------------------------------------------------------
# turn sequence
# --------------------------------------------------------------------------

def _begin_turn(state: GameState, player: int, changes: list):
    state.turn_number += 1
    state.to_move = player
    ps = state.players[player]
    ps.turns_taken += 1
    ps.energy_attached_this_turn = 0
    ps.retreats_this_turn = 0
    changes.append(Change(CHANGE_TURN_START, player=player,
                          detail={"turn": state.turn_number,
                                  "playerTurn": ps.turns_taken}))

    # The one thing the first player skips. Skipping it also skips the
    # deck-out check, which is correct: they were never asked to draw.
    skip = (state.turn_number == 1
            and player == state.first_player
            and not state.rules.first_player_draws_on_first_turn)
    if not skip:
        _draw(state, player, 1, changes)
    _check_game_end(state, changes)


def _checkup(state: GameState, changes: list):
    """Pokemon Checkup: the between-turns step, in rulebook order.

    Poison, then Burn, then Sleep, then Paralysis. Only the Active can hold a
    Special Condition (they come off on the way to the bench), so only Actives
    are examined. Paralysis is cured only on the player whose turn just ended -
    that is what makes a Pokemon paralysed on your opponent's turn stay
    paralysed for the whole of yours.
    """
    ended = state.to_move
    for player in (ended, 1 - ended):
        slot = state.players[player].active
        if slot is None:
            continue

        if POISONED in slot.conditions:
            _apply_damage(state, slot, state.rules.poison_damage, changes,
                          {"source": POISONED})
        if BURNED in slot.conditions:
            _apply_damage(state, slot, state.rules.burn_damage, changes,
                          {"source": BURNED})
            if state.rules.burn_flip_removes and _flip(state, changes, BURNED, player):
                slot.conditions.discard(BURNED)
                changes.append(Change(CHANGE_CONDITION, slot=slot.slot_id,
                                      detail={"condition": BURNED, "added": False}))
        if ASLEEP in slot.conditions and state.rules.sleep_flip_wakes:
            if _flip(state, changes, ASLEEP, player):
                slot.conditions.discard(ASLEEP)
                changes.append(Change(CHANGE_CONDITION, slot=slot.slot_id,
                                      detail={"condition": ASLEEP, "added": False}))
        if player == ended and PARALYZED in slot.conditions:
            slot.conditions.discard(PARALYZED)
            changes.append(Change(CHANGE_CONDITION, slot=slot.slot_id,
                                  detail={"condition": PARALYZED, "added": False}))

    _resolve_knockouts(state, changes)


def _end_turn(state: GameState, changes: list):
    """Close out the turn, pausing wherever a knockout demands a promotion.

    A knockout can interrupt this in two places - after the attack and again
    after checkup damage - so the resume point is recorded in state and picked
    up by Promote. Nothing else may happen while a promotion is owed.
    """
    changes.append(Change(CHANGE_TURN_END, player=state.to_move,
                          detail={"turn": state.turn_number}))
    if state.pending_promotions:
        state.after_promotions = "checkup"
        return
    _run_checkup_and_advance(state, changes)


def _run_checkup_and_advance(state: GameState, changes: list):
    _checkup(state, changes)
    if _check_game_end(state, changes):
        return
    if state.pending_promotions:
        state.after_promotions = "nextTurn"
        return
    _advance_turn(state, changes)


def _advance_turn(state: GameState, changes: list):
    state.after_promotions = None
    if state.phase == PHASE_MAIN:
        _begin_turn(state, 1 - state.to_move, changes)


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

def new_game(db: CardDB, decks, seed=0, rules: Rules = DEFAULT_RULES,
             first_player=None, rng=None):
    """Shuffle, deal, mulligan, and stop with both players ready to place.

    Returns (state, changes) in PHASE_SETUP: each player in turn must place an
    Active and may fill the bench, because active/bench choice is a decision
    the client has to be asked about and cannot be made here.

    Real setup is simultaneous and hidden. Doing it sequentially (first player
    places first) is visible to nobody but the server and removes a whole class
    of "who is waiting on whom" bug, so that is the assumption.

    Mulligans: a hand with no Basic is revealed, shuffled back and redealt; the
    opponent then draws one extra card per mulligan. The real rule makes that
    draw optional and takes it after placement - taking it automatically, here,
    is a simplification that never disadvantages the non-mulliganing player.

    `seed` builds a plain random.Random; `rng` overrides it entirely, which is
    how a test pins a specific shuffle or a specific run of coin flips. The
    generator lives in the state, so a deep copy of a state carries its own
    future with it and two copies diverge independently.
    """
    if rng is None:
        rng = random.Random(seed)
    state = GameState(db=db, rules=rules, rng=rng, players=[
        PlayerState(index=0), PlayerState(index=1)])
    changes = []

    for p in (0, 1):
        ps = state.players[p]
        ps.deck = [_new_card(state, guid, p) for guid in decks[p]]

    if first_player is None:
        first_player = 0 if _flip(state, changes, "firstPlayer") else 1
    state.first_player = first_player
    state.to_move = first_player

    for p in (0, 1):
        _shuffle_deck(state, p, changes)

    # Deal and mulligan. Both hands must contain a Basic before anyone places,
    # so the counts are collected first and the compensating draws applied
    # after - otherwise a player's extra cards could themselves be redealt.
    for p in (0, 1):
        ps = state.players[p]
        while True:
            _draw(state, p, rules.hand_size, changes)
            if any(state.card(cid).is_basic_pokemon for cid in ps.hand):
                break
            if ps.mulligans >= rules.max_mulligans:
                raise ValueError(
                    "player %d mulliganed %d times: deck has no Basic Pokemon"
                    % (p, ps.mulligans))
            ps.mulligans += 1
            changes.append(Change(CHANGE_MULLIGAN, player=p,
                                  detail={"count": ps.mulligans,
                                          "hand": list(ps.hand)}))
            for cid in list(ps.hand):
                ps.deck.append(cid)
                changes.append(Change(CHANGE_MOVE, player=p, card=cid,
                                      from_zone=ZONE_HAND, to_zone=ZONE_DECK))
            ps.hand.clear()
            _shuffle_deck(state, p, changes)

    for p in (0, 1):
        extra = state.players[1 - p].mulligans
        if extra:
            _draw(state, p, extra, changes)

    changes.append(Change(CHANGE_PHASE, detail={"phase": PHASE_SETUP,
                                                "firstPlayer": first_player}))
    return state, changes


def _shuffle_deck(state: GameState, player: int, changes: list):
    state.rng.shuffle(state.players[player].deck)
    changes.append(Change(CHANGE_SHUFFLE, player=player, to_zone=ZONE_DECK,
                          amount=len(state.players[player].deck)))


def _deal_prizes(state: GameState, changes: list):
    for p in (0, 1):
        ps = state.players[p]
        for _ in range(state.rules.prize_count):
            if not ps.deck:
                ps.decked_out = True
                break
            cid = ps.deck.pop(0)
            ps.prizes.append(cid)
            changes.append(Change(CHANGE_MOVE, player=p, card=cid,
                                  from_zone=ZONE_DECK, to_zone=ZONE_PRIZES))


# --------------------------------------------------------------------------
# legality
# --------------------------------------------------------------------------

def players_to_act(state: GameState) -> list:
    """Who legal_actions() will offer anything to, in priority order."""
    if state.over:
        return []
    if state.pending_promotions:
        return [state.pending_promotions[0]]
    return [state.to_move]


def _can_evolve_onto(state: GameState, card: Card, slot: Slot,
                     ps: PlayerState) -> bool:
    """Stage progression, species match and the "not this turn" timing rule.

    The species check is the authoritative one - ATTR_EVOLVES_FROM names the
    pre-evolution exactly - and the stage check is a guard that keeps a
    Stage2 from being dropped straight onto a Basic even if the names line up.
    """
    top = state.pokemon(slot)
    if card.evolves_from != top.name:
        return False
    if STAGE_ORDER.get(card.stage) is None or STAGE_ORDER.get(top.stage) is None:
        return False
    if STAGE_ORDER[card.stage] != STAGE_ORDER[top.stage] + 1:
        return False
    # Not the turn it was played. Setup placements carry setup_play_turn, so
    # this alone also forbids evolving on the first turn of the game.
    return slot.played_on_turn < ps.turns_taken


def _can_attack_now(state: GameState, player: int) -> bool:
    ps = state.players[player]
    if ps.active is None or state.players[1 - player].active is None:
        return False
    if ASLEEP in ps.active.conditions or PARALYZED in ps.active.conditions:
        return False
    if (state.turn_number == 1 and player == state.first_player
            and not state.rules.first_player_may_attack_on_first_turn):
        return False
    return True


def legal_actions(state: GameState, player: int) -> list:
    """Everything `player` may legally do right now.

    Advisory, not authoritative: apply() re-validates from scratch, and will
    accept a legal action this function chose not to enumerate (see
    _retreat_payments, which collapses interchangeable Energy).
    """
    if player not in players_to_act(state):
        return []

    ps = state.players[player]
    actions = []

    if state.pending_promotions and state.pending_promotions[0] == player:
        return [Promote(player, slot.slot_id) for slot in ps.bench]

    if state.phase == PHASE_SETUP:
        if ps.active is None:
            return [SetupPlaceActive(player, cid) for cid in ps.hand
                    if state.card(cid).is_basic_pokemon]
        if len(ps.bench) < state.rules.bench_size:
            actions += [SetupPlaceBench(player, cid) for cid in ps.hand
                        if state.card(cid).is_basic_pokemon]
        actions.append(SetupDone(player))
        return actions

    if state.phase != PHASE_MAIN:
        return []

    in_play = ps.in_play
    for cid in ps.hand:
        card = state.card(cid)
        if card.is_basic_pokemon and len(ps.bench) < state.rules.bench_size:
            actions.append(PlayBasic(player, cid))
        elif card.is_evolution:
            for slot in in_play:
                if _can_evolve_onto(state, card, slot, ps):
                    actions.append(Evolve(player, cid, slot.slot_id))
        elif card.is_energy:
            if ps.energy_attached_this_turn < state.rules.energy_attachments_per_turn:
                actions += [AttachEnergy(player, cid, slot.slot_id)
                            for slot in in_play]

    if (ps.active is not None and ps.bench
            and ps.retreats_this_turn < state.rules.retreats_per_turn
            and ASLEEP not in ps.active.conditions
            and PARALYZED not in ps.active.conditions
            and not (state.rules.confusion_blocks_retreat
                     and CONFUSED in ps.active.conditions)):
        cost = state.pokemon(ps.active).retreat_cost
        payments = _retreat_payments(state, ps.active, cost)
        actions += [Retreat(player, slot.slot_id, payment)
                    for slot in ps.bench for payment in payments]

    if _can_attack_now(state, player):
        option_sets = _energy_options(state, ps.active)
        for attack in state.pokemon(ps.active).attacks:
            if can_pay_cost(option_sets, attack.cost):
                actions.append(Attack(player, attack.ability_id))

    actions.append(Pass(player))
    return actions


# --------------------------------------------------------------------------
# apply
# --------------------------------------------------------------------------

def apply(state: GameState, action: Action):
    """Validate and perform one action; return (new_state, changes).

    The input state is never mutated. Every handler below works on the copy,
    and every one of them re-checks legality itself rather than trusting that
    the caller consulted legal_actions().
    """
    state = copy.deepcopy(state)
    changes = []
    player = action.player

    if state.over:
        raise IllegalAction("the game is over")
    if player not in (0, 1):
        raise IllegalAction("no such player: %r" % (player,))

    expected = players_to_act(state)
    if player not in expected:
        raise IllegalAction("it is not player %d's turn to act (expected %r)"
                            % (player, expected))

    handler = _HANDLERS.get(type(action))
    if handler is None:
        raise IllegalAction("unknown action %r" % (action,))
    handler(state, action, changes)
    return state, changes


def _require(condition, message):
    if not condition:
        raise IllegalAction(message)


def _hand_card(state: GameState, player: int, cid: int) -> Card:
    _require(cid in state.players[player].hand,
             "card %r is not in player %d's hand" % (cid, player))
    return state.card(cid)


def _own_slot(state: GameState, player: int, slot_id: int) -> Slot:
    found = state.slot(slot_id)
    _require(found is not None, "no such slot %r" % (slot_id,))
    owner, slot, _ = found
    _require(owner == player, "slot %r is not player %d's" % (slot_id, player))
    return slot


def _do_setup_place_active(state, action, changes):
    ps = state.players[action.player]
    _require(state.phase == PHASE_SETUP, "not in setup")
    _require(ps.active is None, "an Active Pokemon is already placed")
    card = _hand_card(state, action.player, action.card)
    _require(card.is_basic_pokemon, "%s is not a Basic Pokemon" % card.name)
    ps.hand.remove(action.card)
    ps.active = _new_slot(state, action.card, state.rules.setup_play_turn)
    changes.append(Change(CHANGE_MOVE, player=action.player, card=action.card,
                          slot=ps.active.slot_id, from_zone=ZONE_HAND,
                          to_zone=ZONE_ACTIVE))


def _do_setup_place_bench(state, action, changes):
    ps = state.players[action.player]
    _require(state.phase == PHASE_SETUP, "not in setup")
    _require(ps.active is not None, "place an Active Pokemon first")
    _require(len(ps.bench) < state.rules.bench_size, "the bench is full")
    card = _hand_card(state, action.player, action.card)
    _require(card.is_basic_pokemon, "%s is not a Basic Pokemon" % card.name)
    ps.hand.remove(action.card)
    slot = _new_slot(state, action.card, state.rules.setup_play_turn)
    ps.bench.append(slot)
    changes.append(Change(CHANGE_MOVE, player=action.player, card=action.card,
                          slot=slot.slot_id, from_zone=ZONE_HAND,
                          to_zone=ZONE_BENCH))


def _do_setup_done(state, action, changes):
    ps = state.players[action.player]
    _require(state.phase == PHASE_SETUP, "not in setup")
    _require(ps.active is not None, "place an Active Pokemon first")
    ps.setup_done = True

    if not all(p.setup_done for p in state.players):
        state.to_move = 1 - action.player
        return

    # Prizes come off the deck after both boards are set, which is the real
    # order and matters: cards placed during setup can never be prizes.
    _deal_prizes(state, changes)
    state.phase = PHASE_MAIN
    state.turn_number = 0
    changes.append(Change(CHANGE_PHASE, detail={"phase": PHASE_MAIN}))
    _begin_turn(state, state.first_player, changes)


def _do_play_basic(state, action, changes):
    ps = state.players[action.player]
    _require(state.phase == PHASE_MAIN, "not the main phase")
    _require(len(ps.bench) < state.rules.bench_size, "the bench is full")
    card = _hand_card(state, action.player, action.card)
    _require(card.is_basic_pokemon, "%s is not a Basic Pokemon" % card.name)
    ps.hand.remove(action.card)
    slot = _new_slot(state, action.card, ps.turns_taken)
    ps.bench.append(slot)
    changes.append(Change(CHANGE_MOVE, player=action.player, card=action.card,
                          slot=slot.slot_id, from_zone=ZONE_HAND,
                          to_zone=ZONE_BENCH))


def _do_evolve(state, action, changes):
    ps = state.players[action.player]
    _require(state.phase == PHASE_MAIN, "not the main phase")
    card = _hand_card(state, action.player, action.card)
    slot = _own_slot(state, action.player, action.slot)
    _require(card.is_evolution, "%s is not an evolution card" % card.name)
    _require(_can_evolve_onto(state, card, slot, ps),
             "%s cannot evolve %s this turn"
             % (card.name, state.pokemon(slot).name))

    ps.hand.remove(action.card)
    # Damage stays on the Pokemon through evolution; Special Conditions do not.
    _clear_conditions(state, slot, changes, "evolved")
    previous = slot.top
    slot.stack.append(action.card)
    slot.played_on_turn = ps.turns_taken
    changes.append(Change(CHANGE_MOVE, player=action.player, card=action.card,
                          slot=slot.slot_id, from_zone=ZONE_HAND,
                          to_zone=ZONE_ACTIVE if ps.active is slot else ZONE_BENCH))
    changes.append(Change(CHANGE_EVOLVE, player=action.player, card=action.card,
                          slot=slot.slot_id,
                          detail={"from": previous, "name": card.name,
                                  "damage": slot.damage,
                                  "maxHP": card.max_hp}))


def _do_attach_energy(state, action, changes):
    ps = state.players[action.player]
    _require(state.phase == PHASE_MAIN, "not the main phase")
    _require(ps.energy_attached_this_turn < state.rules.energy_attachments_per_turn,
             "already attached an Energy this turn")
    card = _hand_card(state, action.player, action.card)
    _require(card.is_energy, "%s is not an Energy card" % card.name)
    slot = _own_slot(state, action.player, action.slot)

    ps.hand.remove(action.card)
    slot.energy.append(action.card)
    ps.energy_attached_this_turn += 1
    changes.append(Change(CHANGE_MOVE, player=action.player, card=action.card,
                          slot=slot.slot_id, from_zone=ZONE_HAND,
                          to_zone=ZONE_ACTIVE if ps.active is slot else ZONE_BENCH))
    changes.append(Change(CHANGE_ATTACH, player=action.player, card=action.card,
                          slot=slot.slot_id))


def _do_retreat(state, action, changes):
    ps = state.players[action.player]
    _require(state.phase == PHASE_MAIN, "not the main phase")
    _require(ps.active is not None, "no Active Pokemon to retreat")
    _require(ps.retreats_this_turn < state.rules.retreats_per_turn,
             "already retreated this turn")
    active = ps.active
    _require(ASLEEP not in active.conditions, "an Asleep Pokemon cannot retreat")
    _require(PARALYZED not in active.conditions,
             "a Paralyzed Pokemon cannot retreat")
    _require(not (state.rules.confusion_blocks_retreat
                  and CONFUSED in active.conditions),
             "a Confused Pokemon cannot retreat under these rules")

    incoming = _own_slot(state, action.player, action.slot)
    _require(incoming in ps.bench, "slot %r is not on the bench" % (action.slot,))

    cost = state.pokemon(active).retreat_cost
    payment = list(action.energy)
    _require(len(set(payment)) == len(payment), "duplicate Energy in payment")
    for cid in payment:
        _require(cid in active.energy,
                 "card %r is not attached to the Active Pokemon" % (cid,))
    paid = sum(state.card(cid).energy_units for cid in payment)
    _require(paid >= cost, "retreat costs %d, payment provides %d" % (cost, paid))
    # Refuse to over-discard: with a cost of 1 you may not throw away three
    # Energy because the client sent a sloppy selection.
    for cid in payment:
        _require(paid - state.card(cid).energy_units < cost,
                 "payment discards more Energy than the retreat cost needs")

    for cid in payment:
        active.energy.remove(cid)
        ps.discard.append(cid)
        changes.append(Change(CHANGE_MOVE, player=action.player, card=cid,
                              slot=active.slot_id, from_zone=ZONE_ACTIVE,
                              to_zone=ZONE_DISCARD))

    _clear_conditions(state, active, changes, "retreated")
    ps.bench.remove(incoming)
    ps.bench.append(active)
    ps.active = incoming
    ps.retreats_this_turn += 1
    changes.append(Change(CHANGE_RETREAT, player=action.player,
                          slot=active.slot_id,
                          detail={"promoted": incoming.slot_id, "cost": cost}))


def _do_attack(state, action, changes):
    ps = state.players[action.player]
    opponent = state.players[1 - action.player]
    _require(state.phase == PHASE_MAIN, "not the main phase")
    _require(_can_attack_now(state, action.player),
             "player %d cannot attack right now" % action.player)

    attacker_slot = ps.active
    attacker = state.pokemon(attacker_slot)
    attack = attacker.attack(action.ability_id)
    _require(attack is not None,
             "%s has no attack %r" % (attacker.name, action.ability_id))
    _require(can_pay_cost(_energy_options(state, attacker_slot), attack.cost),
             "not enough Energy attached for %s" % attack.title)

    changes.append(Change(CHANGE_ATTACK, player=action.player,
                          slot=attacker_slot.slot_id,
                          detail={"abilityID": attack.ability_id,
                                  "title": attack.title,
                                  "baseDamage": attack.damage}))

    # Confusion is checked after the attack is declared and paid for: a tails
    # still consumes the turn, and still hurts the attacker.
    if CONFUSED in attacker_slot.conditions:
        if not _flip(state, changes, CONFUSED, action.player):
            _apply_damage(state, attacker_slot, state.rules.confusion_self_damage,
                          changes, {"source": CONFUSED})
            _resolve_knockouts(state, changes)
            if not _check_game_end(state, changes):
                _end_turn(state, changes)
            return

    defender_slot = opponent.active
    defender = state.pokemon(defender_slot)
    dealt = damage_after_weakness(attacker, defender, attack.damage)
    _apply_damage(state, defender_slot, dealt, changes,
                  {"abilityID": attack.ability_id,
                   "baseDamage": attack.damage,
                   "weakness": bool(set(attacker.types) & set(defender.weakness_types)),
                   "resistance": bool(defender.resistance_type
                                      and defender.resistance_type in attacker.types)})

    # EXTENSION POINT: everything the attack's game text says happens here.
    effect = state.rules.attack_effects.get(attack.ability_id)
    if effect is not None:
        effect(state, {"player": action.player, "attack": attack,
                       "attacker": attacker_slot, "defender": defender_slot,
                       "damage": dealt}, changes)

    _resolve_knockouts(state, changes)
    if not _check_game_end(state, changes):
        _end_turn(state, changes)


def _do_promote(state, action, changes):
    _require(state.pending_promotions and state.pending_promotions[0] == action.player,
             "player %d does not owe a promotion" % action.player)
    ps = state.players[action.player]
    _require(ps.active is None, "player %d already has an Active" % action.player)
    slot = _own_slot(state, action.player, action.slot)
    _require(slot in ps.bench, "slot %r is not on the bench" % (action.slot,))

    ps.bench.remove(slot)
    ps.active = slot
    state.pending_promotions.pop(0)
    changes.append(Change(CHANGE_PROMOTE, player=action.player, slot=slot.slot_id,
                          card=slot.top, from_zone=ZONE_BENCH,
                          to_zone=ZONE_ACTIVE))

    if state.pending_promotions:
        return
    resume, state.after_promotions = state.after_promotions, None
    if resume == "checkup":
        _run_checkup_and_advance(state, changes)
    elif resume == "nextTurn":
        if not _check_game_end(state, changes):
            _advance_turn(state, changes)
    else:
        _check_game_end(state, changes)


def _do_pass(state, action, changes):
    _require(state.phase == PHASE_MAIN, "not the main phase")
    _end_turn(state, changes)


_HANDLERS = {
    SetupPlaceActive: _do_setup_place_active,
    SetupPlaceBench: _do_setup_place_bench,
    SetupDone: _do_setup_done,
    PlayBasic: _do_play_basic,
    Evolve: _do_evolve,
    AttachEnergy: _do_attach_energy,
    Retreat: _do_retreat,
    Attack: _do_attack,
    Promote: _do_promote,
    Pass: _do_pass,
}


# --------------------------------------------------------------------------
# EXTENSION POINTS
# --------------------------------------------------------------------------
#
# The four places this engine expects to grow, in the order they will hurt:
#
# 1. Rules.attack_effects - abilityID -> callable, invoked in _do_attack after
#    damage. This is where "the Defending Pokemon is now Asleep", bench damage,
#    energy discard, and the "30+" / "20x" damage modifiers go. The callable
#    already receives the attacker and defender Slots and the damage dealt, and
#    _add_condition / _apply_damage / _take_prize are the primitives it needs.
#    Ability.has_unimplemented_text marks every attack currently under-resolved.
#
# 2. Trainers. legal_actions() never offers a Trainer card, and there is no
#    PlayTrainer action, because a Trainer with no text is not a partial
#    implementation of anything - it is a card that does nothing. Adding them
#    means a PlayTrainer action, a per-turn Supporter flag on PlayerState, a
#    Stadium zone on GameState, and a registry parallel to attack_effects.
#
# 3. PokeAbility / PokePower / PokeBody. Same shape as attack effects, but they
#    need a trigger model (once per turn, on-play, continuous) rather than a
#    single call site. Card.abilities already carries them, parsed and inert.
#
# 4. Prize counts and prize choice. Rules.prizes_per_knockout is a flat number
#    because carddata has no verified "has a rule box" attribute; when one is
#    identified it becomes a lookup on the knocked-out Card. Cards that let a
#    player choose which prize to take need a real action, which means
#    _take_prize gains a pending-choice state the way promotions have one.
