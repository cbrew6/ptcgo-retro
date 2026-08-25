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

Scope is a real game: Basic/Stage1/Stage2 Pokemon, Energy, attacks, Special
Conditions, and all four kinds of Trainer. Per-card *text* is still not here -
this module knows the structural rules ("one Supporter per turn", "a Stadium
replaces the Stadium in play") and nothing about what any individual card says.
What a card does lives in a registry on Rules, keyed by a stable id, and every
registry is EMPTY by default, so a stock engine remains inert and testable.
effects.py builds a populated Rules; see EXTENSION POINTS at the bottom.

Card data is carddata/*.json - the same files server.py serves to the client -
read through the ATTR_* ids below. Nothing here invents an attribute id.


PENDING CHOICES
---------------

Most cards ask a question: which Pokemon to heal, which two cards to discard,
which Pokemon to fetch out of the deck. apply() used to resolve every action
atomically, and could not express "waiting for an answer".

The model is a one-slot continuation on the state:

    apply(state, PlayTrainer(0, potion))
        -> state.pending is a Pending holding a Choice, and a CHANGE_CHOICE
           change describing it
    players_to_act(state)          -> [the player the Choice belongs to]
    legal_actions(state, player)   -> only Choose(player, picks) actions
    apply(state, Choose(0, (slot,))) -> the effect runs on, and either
           finishes or parks another Choice

An effect is therefore not a coroutine but a small step machine, re-entered
once per answer:

    def potion(state, ctx, changes):
        if not ctx["answers"]:
            return Choice(...)          # step 0: ask
        target = ctx["answers"][0][0]   # step 1: act
        ...

Generators would read better and were rejected: apply() deep-copies the whole
state, a suspended generator cannot be deep-copied, and every saved game would
have died on copy.deepcopy. The step machine survives copying because a
Pending is nothing but data.

THE ONE RULE AN EFFECT MUST FOLLOW: a call that returns a Choice must not have
touched `state` or appended to `changes`. The engine re-enters the effect from
the top with one more answer, so anything done before the return would be done
twice. Written in the shape above - all questions first, all mutation last -
this falls out naturally, and it is the same discipline as the _require()
guards at the top of every handler below.

`Choice.player` is who answers, which need not be who played the card: Escape
Rope asks the opponent first. players_to_act() reads it directly, so a choice
owned by the non-turn player routes correctly with no special case.

Only one Choice is ever outstanding, and while one is outstanding NOTHING else
is legal - not even ending the turn. That is what keeps the model a single
field rather than a stack.
"""

from __future__ import annotations

import copy
import dataclasses
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
ATTR_SUBTYPES = 200360           # ["TeamPlasma"], ["Ancient Trait"], ...
ATTR_RARITY = 200550             # "Common" ... "RareHoloEX", "RareHoloGX"
ATTR_SET = 200580
ATTR_COLLECTOR_NUMBER = 200780
ATTR_TRAINER_TYPES = 200270        # "Item" | "Supporter" | "Stadium" | "PokemonTool"
ATTR_ASSET_NAME = 10020            # art override; absolute when it contains "/"
# A Trainer's rules text. Pokemon carry their text per-ability inside
# ATTR_ABILITIES instead and never have this one (0 of 7,367 Pokemon do; 1,120
# of 1,120 Trainers do). Like every text attribute in this data it holds a
# LOCALIZATION KEY, not English - resolving it needs the client's shipped
# LocalizationDB, whose keys are lowercase where these are mixed case.
ATTR_GAME_TEXT = 200310
# Foil treatment. Cosmetic - no rule reads either - but the client computes
# "is this card foil" from exactly these two and requests a mask only if so.
ATTR_FOIL_EFFECT = 200610          # enum FoilEffects,  e.g. "Rainbow"
ATTR_FOIL_MASK = 200620            # enum FoilMasks,    e.g. "Reverse"

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

# The other four abilityTypes in the data, by frequency: PokeAbility (956),
# AncientTrait (93), PokePower (89), PokeBody (78). Nothing distinguishes an
# *activated* ability from a passive one, so the engine does not try: an
# ability is offered as an action only when Rules.ability_effects has an entry
# for it, and membership of that registry is the whole definition of
# "activated". AncientTrait is never activated - it is always continuous.
ABILITY_POKE = "PokeAbility"
ABILITY_POWER = "PokePower"
ABILITY_BODY = "PokeBody"
ABILITY_ANCIENT = "AncientTrait"

# ATTR_TRAINER_TYPES values, all four of them plus the XY oddity. Counted over
# carddata: Item 505, Supporter 332, PokemonTool 164, Stadium 117,
# PokemonToolF 2. "PokemonToolF" is Team Flare Gear - a Tool with a printed
# restriction on which Pokemon may carry it, which the data does not encode, so
# it is played exactly like a Tool and the restriction is not enforced.
TRAINER_ITEM = "Item"
TRAINER_SUPPORTER = "Supporter"
TRAINER_STADIUM = "Stadium"
TRAINER_TOOL = "PokemonTool"
TRAINER_TOOL_F = "PokemonToolF"
TOOL_TYPES = (TRAINER_TOOL, TRAINER_TOOL_F)

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
# The Stadium in play belongs to the board, not to a player - the client's
# IntroduceEntity routes by owningPlayerID and an owned activeStadium is never
# bound to its layout. GameState.stadium remembers who *played* it, because
# that is who discards it when it is replaced, but the zone has no owner.
ZONE_STADIUM = "activeStadium"

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

    # The player going first DOES draw on their first turn. What they may not
    # do is attack, evolve, or play a Supporter - the draw was never part of
    # the first-turn restriction, and having it off meant going first cost a
    # card every game.
    first_player_draws_on_first_turn: bool = True

    # Also SM-era: the first player may not attack on turn 1. They *may* play a
    # Supporter - the two restrictions arrived together in Sun & Moon and only
    # ever covered the draw and the attack.
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

    # Prizes per knockout for an ordinary Pokemon. Cards with rule boxes are
    # worth more, but carddata carries no attribute saying so, so the engine
    # reads nothing into it - `prize_values` below is the exception list and a
    # stock engine has none.
    prizes_per_knockout: int = 1

    # "For each of your opponent's mulligans you MAY draw a card." Drawing
    # them without asking is simpler and very nearly always what a player
    # wants, but it is not the rule, and a hand is sometimes better small.
    optional_mulligan_draw: bool = True

    energy_attachments_per_turn: int = 1
    retreats_per_turn: int = 1

    # Trainer structure. All three are per player per turn; Items are
    # deliberately absent because there is no limit on them.
    supporters_per_turn: int = 1
    stadiums_per_turn: int = 1
    tools_per_pokemon: int = 1

    # An Ability is usable once per turn by each Pokemon that has it. A few
    # real cards say "once during your turn" across the whole board instead;
    # those are the minority and are not modelled.
    ability_uses_per_turn: int = 1

    # Guards rather than rules: a deck with no Basic Pokemon would mulligan for
    # ever, and enumerating retreat payments over a huge pile of Energy is
    # pointless work for a theme-deck game.
    max_mulligans: int = 100
    max_enumerated_energy: int = 10
    # Ditto for choices: "discard 2 cards from your hand" over an eight-card
    # hand is 28 answers, but "put 2 basic Energy from a 30-card discard pile
    # into your hand" is 435. legal_actions() stops at this many and says so
    # by simply offering fewer; apply() still accepts any legal answer, exactly
    # as it does for the retreat payments _retreat_payments() collapses.
    max_enumerated_choices: int = 60

    # ---- effect registries ------------------------------------------------
    #
    # Four registries, each keyed by a stable id, each EMPTY by default so a
    # stock engine is inert and every rules test above is unaffected by them.
    # effects.py builds a Rules with them populated.
    #
    # One-shots are callable(state, ctx, changes) and may return a Choice to
    # suspend (see PENDING CHOICES in the module docstring); returning None
    # means done.

    # abilityID -> the base damage this use of the attack deals, BEFORE
    # Weakness and Resistance: callable(state, ctx, changes) -> int. This is
    # where amountOperator lives ("30+", "40x", "80-"). It runs before the
    # damage lands and so may NOT return a Choice; a coin flip it makes is
    # recorded in ctx["data"] where attack_effects can read it.
    attack_damage: Mapping[str, Callable[..., int]] = field(default_factory=dict)

    # abilityID -> callable(state, ctx, changes), called after an attack's
    # damage lands. Special Conditions, bench damage, self damage, Energy
    # discard - everything the text says that is not the damage number.
    attack_effects: Mapping[str, Callable[..., None]] = field(default_factory=dict)

    # archetype GUID -> callable(state, ctx, changes). A Trainer is playable
    # if and only if it has an entry here: a Trainer with no implemented text
    # is a card that does nothing, and offering it would be worse than leaving
    # it in hand. Keyed per printing rather than per name because two cards
    # sharing a name are not the same card.
    trainer_effects: Mapping[str, Callable[..., None]] = field(default_factory=dict)

    # abilityID -> callable(state, ctx, changes) for an ACTIVATED Ability.
    # Membership is the definition of "activated" - see ABILITY_POKE above.
    ability_effects: Mapping[str, Callable[..., None]] = field(default_factory=dict)

    # Continuous effects, keyed by abilityID (for a Pokemon's Ability) or by
    # archetype GUID (for an attached Tool or the Stadium in play). One
    # callable answers every question:
    #
    #     callable(query, state, ctx, value) -> value
    #
    # with `query` one of the STATIC_* strings below. The default is inert
    # because an empty registry is never consulted at all - _static() short
    # circuits, which is what keeps max_hp() cheap in the common case.
    static_effects: Mapping[str, Callable[..., int]] = field(default_factory=dict)

    # archetype GUID -> prizes taken when THIS printing is knocked out, for the
    # cards that are not worth one. Empty by default: nothing in carddata marks
    # a rule box, so the judgement is made in effects.py from the printed name
    # (a Pokemon-EX is literally named "...EX") and the engine only looks the
    # answer up. Keyed by the guid of the card on TOP of the stack, which is
    # the Pokemon in play - a Stage 2 sitting on two Basics is worth what the
    # Stage 2 says, and a BREAK is worth what a BREAK is worth (one).
    prize_values: Mapping[str, int] = field(default_factory=dict)


# Queries a static_effects callable must be prepared to be asked. Anything it
# does not recognise it returns `value` for unchanged.
STATIC_DAMAGE_DEALT = "damageDealt"   # added to base damage, before Weakness
STATIC_DAMAGE_TAKEN = "damageTaken"   # subtracted from damage, after Weakness
STATIC_RETREAT_COST = "retreatCost"   # symbols, floored at 0 by the caller
STATIC_MAX_HP = "maxHP"
STATIC_NO_WEAKNESS = "noWeakness"     # non-zero means Weakness does not apply
# Extra damage counters put on a Poisoned Pokemon between turns. Virbank City
# Gym is the card; it is a Stadium, so it is asked about both players' Actives.
# Separate from STATIC_DAMAGE_TAKEN because Poison is not an attack: it is not
# reduced by damage-reduction effects and Weakness never applies to it.
STATIC_POISON_DAMAGE = "poisonDamage"


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


# The set that holds the freely-granted basic Energy. Its nine archetypes are
# exactly the nine basic Energy cards, and they are the ones a deck may hold
# more than four of.
FREE_ENERGY_SET = "Free_Energy"


def _is_basic_energy(attrs):
    """Whether this is a basic Energy card.

    Attribute 200520 says so for set-printed Energy, but the Free_Energy
    archetypes omit it - even though their own name key reads
    "...FreeEnergy.energy.BasicFairyEnergy.Name" and that set contains nothing
    else. Trusting the attribute alone made every effect that looks for basic
    Energy - Energy Retrieval, Professor's Letter - blind to the exact prints
    a deck is actually built from.
    """
    if _bool(attrs, ATTR_IS_BASIC_ENERGY):
        return True
    return (_str(attrs, ATTR_SET) == FREE_ENERGY_SET
            and "Energy" in _strings(attrs, ATTR_CARD_TYPES))


def _card_image(value):
    """ATTR_ASSET_NAME, but only when it names a card face.

    The same attribute doubles as an absolute asset path for products
    ("packs/SM3Booster"), which the client resolves against the bundle root
    rather than the card's set. Those archetypes are boosters and deck boxes,
    so anything containing "/" is not a face and is dropped here.
    """
    if not value or "/" in value:
        return None
    return value


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
    # Printed rarity. No rule reads it; it is the only other signal in the
    # pool that corroborates which cards have a rule box, and the prize
    # tests use it to check the name rule has not gone wrong.
    rarity: Optional[str]
    # Printed subtypes - "TeamPlasma" on 205 cards, and the only place that
    # allegiance is recorded. A whole family of cards reads it ("Search your
    # deck for a Team Plasma Pokemon"), and without it they cannot be written
    # at all, because nothing else distinguishes a Team Plasma Pokemon from
    # any other.
    subtypes: tuple
    trainer_types: tuple
    # The localization key for the card's display name. Not used by the rules,
    # but the client's hand comparator dereferences it unguarded, so the
    # protocol layer needs it and this is where archetypes are already parsed.
    name_key: Optional[str] = None
    # Art-variant suffix ("017a", "043xy"). The client's textureLookup prefers
    # this over the padded collector number, so a variant that does not send it
    # renders the wrong printing's art.
    card_image: Optional[str] = None
    # ATTR_GAME_TEXT, the Trainer's rules text - a localization key, never
    # English. The rules never read it; effects.py resolves it against the
    # client's LocalizationDB to decide which effect a printing gets, which is
    # how a reprint whose wording changed stays unimplemented instead of
    # quietly inheriting the wrong behaviour.
    game_text_key: Optional[str] = None
    # How this printing is foiled. No rule reads either, but a card entity
    # that omits them renders flat: the client's art data derives IsFoil from
    # the mask and the effect together, and only a card that says it is foil
    # ever asks for a foil mask. Parsed here because this is the one place
    # archetype attributes are read.
    foil_mask: Optional[str] = None
    foil_effect: Optional[str] = None

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
            is_basic_energy=_is_basic_energy(at),
            evolves_from=_str(at, ATTR_EVOLVES_FROM),
            family_id=at.get(ATTR_FAMILY_ID, {}).get("i"),
            set_code=_str(at, ATTR_SET),
            collector_number=at.get(ATTR_COLLECTOR_NUMBER, {}).get("i"),
            rarity=_str(at, ATTR_RARITY),
            subtypes=tuple(_strings(at, ATTR_SUBTYPES)),
            # stored as a JSON string: "\"$$$com...Name$$$\""
            name_key=(_str(at, 10140) or "").strip('"').strip("$") or None,
            trainer_types=_strings(at, ATTR_TRAINER_TYPES),
            # A value containing "/" is an absolute asset path naming a product
            # ("packs/SM3Booster"), not a card face - those archetypes are boxes
            # and boosters, never anything that reaches a playmat.
            card_image=_card_image(_str(at, ATTR_ASSET_NAME)),
            # Same "\"$$$key$$$\"" wrapping as ATTR_NAME_KEY above.
            game_text_key=(_str(at, ATTR_GAME_TEXT) or "").strip('"').strip("$")
            or None,
            foil_mask=_str(at, ATTR_FOIL_MASK),
            foil_effect=_str(at, ATTR_FOIL_EFFECT),
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
    def trainer_kind(self) -> Optional[str]:
        """Item / Supporter / Stadium / PokemonTool, or None if not a Trainer.

        ATTR_TRAINER_TYPES is documented as an array but is a scalar in the
        export, and no card in carddata carries more than one value, so the
        first entry is the whole answer.
        """
        if not self.is_trainer:
            return None
        return self.trainer_types[0] if self.trainer_types else None

    @property
    def is_item(self) -> bool:
        return self.trainer_kind == TRAINER_ITEM

    @property
    def is_supporter(self) -> bool:
        return self.trainer_kind == TRAINER_SUPPORTER

    @property
    def is_stadium(self) -> bool:
        return self.trainer_kind == TRAINER_STADIUM

    @property
    def is_tool(self) -> bool:
        return self.trainer_kind in TOOL_TYPES

    @property
    def pokemon_abilities(self) -> tuple:
        """Everything on the card that is not an attack."""
        return tuple(a for a in self.abilities if not a.is_attack)

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
    tools: list = field(default_factory=list)
    conditions: set = field(default_factory=set)
    # The owner's own turn counter when this Pokemon was played or evolved.
    # Compared against PlayerState.turns_taken, never against the global turn.
    played_on_turn: int = 0
    # abilityIDs this Pokemon has already used this turn. Cleared for every
    # slot at the start of every turn rather than only the turn player's,
    # because an Ability used on the opponent's turn spends the same allowance.
    abilities_used: set = field(default_factory=set)

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
    supporters_this_turn: int = 0
    stadiums_this_turn: int = 0
    setup_done: bool = False
    mulligans: int = 0
    # Offers of one card, one per mulligan the opponent took, still
    # unanswered. The rule is permissive ("you MAY draw") and it is per
    # mulligan, so this is a queue of yes/no questions rather than a debt:
    # DrawMulligans answers exactly one of them, either way.
    owed_draws: int = 0
    # How many there were before any were answered. The remaining count alone
    # cannot say WHICH mulligan is being asked about, and the client's own
    # prompt is numbered ("...for mulligan {0}?").
    owed_draws_total: int = 0
    # Set the moment a draw is required and the deck is empty. The loss is
    # recorded rather than raised so both players' losses can land on the same
    # check and produce a tie instead of a race.
    decked_out: bool = False

    @property
    def in_play(self) -> list:
        """Active first, then bench - the order the rules resolve things in."""
        return ([self.active] if self.active else []) + list(self.bench)

    @property
    def mulligan_draw_number(self) -> int:
        """Which mulligan the outstanding offer is about: 1-based, 0 if none.

        The offers are answered oldest first, so this counts up as they are
        answered. It exists for the renderer - the prompt names the mulligan
        by number and a bare "3 left" cannot fill that in.
        """
        if self.owed_draws <= 0:
            return 0
        return self.owed_draws_total - self.owed_draws + 1


# --------------------------------------------------------------------------
# pending choices, temporary modifiers, the Stadium
# --------------------------------------------------------------------------

CHOICE_CARD = "card"      # options are card ids
CHOICE_SLOT = "slot"      # options are slot ids
CHOICE_OPTION = "option"  # options are opaque strings ("yes"/"no", a colour)


@dataclass
class Choice:
    """A question an in-flight effect stopped to ask. See PENDING CHOICES.

    `options` is a tuple of ids of one kind, named by `option_kind` so the
    protocol layer knows whether it is pointing the player at cards or at
    Pokemon. `zone` is where those cards currently are, which the client needs
    to know whether to open the deck, the discard, or the hand.

    minimum/maximum are clamped against the number of options on construction.
    An effect that asks for two cards from a one-card discard pile gets one,
    rather than producing a Choice no answer satisfies - a state with no legal
    action is a hung game, and a hung game is worse than a lenient card.
    """
    player: int
    prompt: str                    # stable id for the renderer, e.g. "heal"
    options: tuple = ()
    option_kind: str = CHOICE_CARD
    minimum: int = 1
    maximum: int = 1
    zone: Optional[str] = None
    detail: dict = field(default_factory=dict)

    def __post_init__(self):
        self.options = tuple(self.options)
        self.maximum = max(0, min(self.maximum, len(self.options)))
        self.minimum = max(0, min(self.minimum, self.maximum))

    def as_dict(self) -> dict:
        return {"player": self.player, "prompt": self.prompt,
                "options": list(self.options), "optionKind": self.option_kind,
                "minimum": self.minimum, "maximum": self.maximum,
                "zone": self.zone, "detail": dict(self.detail)}


@dataclass
class Pending:
    """The effect that asked, and everything needed to re-enter it.

    Deliberately pure data: apply() deep-copies the state, so anything here
    that were not copyable would take the whole engine with it.
    """
    kind: str                      # "trainer" | "ability" | "attack"
    key: str                       # registry key: archetype GUID or abilityID
    player: int                    # who is resolving (not always who answers)
    choice: Choice
    answers: list = field(default_factory=list)   # one tuple of picks each
    source: Optional[int] = None   # cid of the card being played, if any
    slot: Optional[int] = None     # source slot id, for an Ability or attack
    after: Optional[str] = None    # "attack" -> end the turn once finished
    data: dict = field(default_factory=dict)      # effect scratch


# Modifier kinds. A Modifier is a rules change with an explicit expiry, which
# is how "during your opponent's next turn" is expressed without a scheduler.
MOD_DAMAGE_DEALT = "damageDealt"      # + to base damage, before Weakness
MOD_DAMAGE_TAKEN = "damageTaken"      # - from damage, after Weakness
MOD_PREVENT_DAMAGE = "preventDamage"  # damage to this slot becomes 0
MOD_NO_RETREAT = "noRetreat"
MOD_NO_ABILITIES = "noAbilities"      # every Pokemon of `player` has none
MOD_RETREAT_COST = "retreatCost"      # + to the retreat cost, floored at 0
MOD_NO_WEAKNESS = "noWeakness"        # this slot's Weakness does not apply

# What an attack may declare its damage is not affected by. "This attack's
# damage isn't affected by Resistance" is 101 attacks on its own, and the
# three of these together are the only forms the printed text takes.
IGNORE_WEAKNESS = "weakness"
IGNORE_RESISTANCE = "resistance"
IGNORE_EFFECTS = "effects"      # ... any effect on the defending Pokemon


@dataclass
class Modifier:
    """One temporary rules change, alive while turn_number <= until_turn.

    Expiry is a turn *number*, not a countdown, because turn numbers already
    increment exactly once per player turn and a countdown would have to be
    decremented from somewhere. "Until the end of your opponent's next turn"
    is `state.turn_number + 1`; "during this turn" is `state.turn_number`.
    """
    kind: str
    until_turn: int
    player: Optional[int] = None   # whose side it applies to, if it is a side
    slot: Optional[int] = None     # which Pokemon, if it is one Pokemon
    amount: int = 0
    source: Optional[int] = None   # cid that created it, for the renderer
    detail: dict = field(default_factory=dict)


@dataclass
class StadiumInPlay:
    """The one Stadium on the board. `owner` is only who discards it."""
    card: int
    owner: int


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
    # The Stadium belongs to neither board; see ZONE_STADIUM.
    stadium: Optional[StadiumInPlay] = None
    # At most one outstanding question. While this is set, the only legal
    # action in the whole game is answering it.
    pending: Optional[Pending] = None
    modifiers: list = field(default_factory=list)
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
        """Printed HP, plus whatever is continuously adding to it.

        Fighting Fury Belt style "+40 HP" has to land here rather than at the
        knockout check, because everything that reads a Pokemon's HP - the
        knockout test, the damage Change's maxHP, the client's originalValue -
        has to agree on one number.
        """
        printed = self.pokemon(slot).max_hp
        if not self.rules.static_effects:
            return printed
        return max(0, _static(self, STATIC_MAX_HP, {"slot": slot}, printed,
                              sources=_slot_static_sources(self, slot)))

    def slots(self) -> list:
        """Every Pokemon in play, both players, Actives first."""
        return self.players[0].in_play + self.players[1].in_play

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
class PlayTrainer(Action):
    """Play an Item, a Supporter or a Stadium from hand.

    One action for three kinds because the *choice* the player makes is the
    same one - "play this card" - and everything that differs afterwards is a
    rule, not a decision. A Pokemon Tool is the exception: it names a target
    at the moment it is played, so it gets AttachTool.
    """
    card: int


@dataclass(frozen=True)
class AttachTool(Action):
    card: int          # the Tool, in hand
    slot: int          # one of your Pokemon in play


@dataclass(frozen=True)
class UseAbility(Action):
    """Activate a Pokemon's Ability. ability_id is its real abilityID GUID."""
    slot: int
    ability_id: str


@dataclass(frozen=True)
class Choose(Action):
    """Answer the outstanding Choice. See PENDING CHOICES.

    `picks` is a tuple of ids drawn from Choice.options; its length must fall
    between the Choice's minimum and maximum. An empty tuple is the answer to
    an optional choice ("you MAY reveal a Pokemon you find there").
    """
    picks: tuple = ()


@dataclass(frozen=True)
class Promote(Action):
    """Choose a new Active after the old one was knocked out."""
    slot: int


@dataclass(frozen=True)
class DrawMulligans(Action):
    """Answer ONE of the offers the opponent's mulligans entitle you to.

    Compensation is per mulligan, not a lump sum, and the original server asked
    that way: its prompt survives in the client's shipped localization DB as
    "Would you like to draw a card for mulligan {0}?" with Yes and No buttons.
    So `take` is one answer to one offer - declining this card leaves the
    remaining offers standing, and each is independently declinable.

    The same DB carries "Yes to rest ({0})" and "No to rest ({0})", so the
    original UI could also answer every remaining offer at once. That stays a
    protocol-layer shortcut - it is this action repeated - rather than a count
    on the action, because a count cannot express the numbered question.
    """
    take: bool


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
CHANGE_PLAY = "play"          # a Trainer was played; detail names its kind
CHANGE_TOOL = "tool"          # a Pokemon Tool attached to a Pokemon
CHANGE_STADIUM = "stadium"    # a Stadium came into play or was replaced
CHANGE_ABILITY = "ability"    # an Ability was activated
CHANGE_HEAL = "heal"          # damage counters removed
CHANGE_CHOICE = "choice"      # a Choice is now outstanding; detail is it
CHANGE_CHOSE = "chose"        # ... and this is the answer that resolved it
CHANGE_MODIFIER = "modifier"  # a temporary rules change started
CHANGE_REVEAL = "reveal"      # cards shown to both players; detail has "cards"


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
# continuous effects
# --------------------------------------------------------------------------
#
# Two independent mechanisms, kept apart because they expire differently.
#
# Rules.static_effects is printed text that is true for as long as the card is
# where it is: an Ability on a Pokemon in play, a Tool attached to it, the
# Stadium on the board. It is re-read every time a number is needed and never
# stored, so a Tool being discarded takes its effect with it automatically.
#
# GameState.modifiers is text that created a *temporary* change and then
# finished - "during your opponent's next turn, damage done to this Pokemon is
# reduced by 20". Those have to be remembered, and they carry their own expiry.

def _slot_static_sources(state: GameState, slot: Slot) -> list:
    """Registry keys that speak for this Pokemon: its Abilities, then Tools.

    Attacks are excluded - an attack is not continuously true - and so is the
    rest of the evolution stack, because only the top card's text is active.
    """
    keys = []
    if not _abilities_active(state, slot):
        pass
    else:
        keys += [a.ability_id for a in state.pokemon(slot).pokemon_abilities
                 if a.ability_id]
    keys += [state.cards[cid].archetype for cid in slot.tools]
    return keys


def _static(state: GameState, query: str, ctx: dict, value: int,
            sources=()) -> int:
    """Run `value` through every continuous effect that claims to speak.

    The Stadium is always consulted, because a Stadium affects the board
    rather than a Pokemon. Order between sources is not defined by the rules
    and is not defined here either; every static effect in effects.py is an
    addition or a floor, both of which commute.
    """
    registry = state.rules.static_effects
    if not registry:
        return value
    keys = list(sources)
    if state.stadium is not None:
        keys.append(state.cards[state.stadium.card].archetype)
    for key in keys:
        hook = registry.get(key)
        if hook is not None:
            value = hook(query, state, ctx, value)
    return value


def _abilities_active(state: GameState, slot: Slot) -> bool:
    """False while something (Hex Maniac) has switched this side's off."""
    if not state.modifiers:
        return True
    owner = state.owner_of(slot.top)
    return not any(m.kind == MOD_NO_ABILITIES
                   and (m.player is None or m.player == owner)
                   for m in _live_modifiers(state))


def _live_modifiers(state: GameState) -> list:
    return [m for m in state.modifiers if m.until_turn >= state.turn_number]


def _modifier_total(state: GameState, kind: str, slot: Slot = None,
                    player: int = None) -> int:
    total = 0
    for m in _live_modifiers(state):
        if m.kind != kind:
            continue
        if m.slot is not None and (slot is None or m.slot != slot.slot_id):
            continue
        if m.slot is None and m.player is not None and m.player != player:
            continue
        total += m.amount
    return total


def _has_modifier(state: GameState, kind: str, slot: Slot = None,
                  player: int = None) -> bool:
    for m in _live_modifiers(state):
        if m.kind != kind:
            continue
        if m.slot is not None:
            if slot is not None and m.slot == slot.slot_id:
                return True
            continue
        if m.player is None or m.player == player:
            return True
    return False


def _add_modifier(state: GameState, changes: list, modifier: Modifier):
    state.modifiers.append(modifier)
    changes.append(Change(CHANGE_MODIFIER, player=modifier.player,
                          slot=modifier.slot, card=modifier.source,
                          amount=modifier.amount,
                          detail={"kind": modifier.kind,
                                  "untilTurn": modifier.until_turn,
                                  **modifier.detail}))


def retreat_cost(state: GameState, slot: Slot) -> int:
    """The Pokemon's retreat cost in Energy symbols, after everything.

    Public because both legal_actions() and _do_retreat() need the same
    number, and a second copy of this arithmetic anywhere would eventually
    disagree with the first.
    """
    cost = state.pokemon(slot).retreat_cost
    cost = _static(state, STATIC_RETREAT_COST, {"slot": slot}, cost,
                   sources=_slot_static_sources(state, slot))
    cost += _modifier_total(state, MOD_RETREAT_COST, slot)
    return max(0, cost)


def _attack_damage(state: GameState, attacker_slot: Slot, defender_slot: Slot,
                   base: int, ignore=()) -> int:
    """Base damage through every layer, in the order the rules apply them.

    Additions to the attacker's damage come first (they are printed as
    "before applying Weakness and Resistance"), then Weakness and Resistance,
    then reductions on the defender (printed as "after applying Weakness and
    Resistance"). Getting that order wrong is worth 30 damage on a Weakness.

    `ignore` is what the attack's own text says its damage is not affected by;
    IGNORE_EFFECTS covers the reductions the defender put up, which is exactly
    what "isn't affected by any effects on your opponent's Active Pokemon"
    means and is why it does not also switch off Weakness.
    """
    attacker = state.pokemon(attacker_slot)
    defender = state.pokemon(defender_slot)
    owner = state.owner_of(attacker_slot.top)

    base += _static(state, STATIC_DAMAGE_DEALT,
                    {"slot": attacker_slot, "defender": defender_slot}, 0,
                    sources=_slot_static_sources(state, attacker_slot))
    base += _modifier_total(state, MOD_DAMAGE_DEALT, attacker_slot, owner)
    if base <= 0:
        return 0

    # Weakness Policy and "this Pokemon has no Weakness until ..." both land
    # here. Stripping the defender's weakness types and reusing the ordinary
    # path is what keeps Resistance from drifting out of step with it.
    no_weakness = (IGNORE_WEAKNESS in ignore
                   or _has_modifier(state, MOD_NO_WEAKNESS, defender_slot)
                   or _static(state, STATIC_NO_WEAKNESS, {"slot": defender_slot},
                              0, sources=_slot_static_sources(state, defender_slot)))
    effective = defender
    if no_weakness:
        effective = dataclasses.replace(effective, weakness_types=())
    if IGNORE_RESISTANCE in ignore:
        effective = dataclasses.replace(effective, resistance_type=None)
    dealt = damage_after_weakness(attacker, effective, base)

    if IGNORE_EFFECTS in ignore:
        return max(0, dealt)
    if _has_modifier(state, MOD_PREVENT_DAMAGE, defender_slot):
        return 0
    dealt -= _static(state, STATIC_DAMAGE_TAKEN,
                     {"slot": defender_slot, "attacker": attacker_slot}, 0,
                     sources=_slot_static_sources(state, defender_slot))
    dealt -= _modifier_total(state, MOD_DAMAGE_TAKEN, defender_slot,
                             state.owner_of(defender_slot.top))
    return max(0, dealt)


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
    # The card is named as well as the slot. Changes are rendered against the
    # FINAL state, and a Pokemon that this damage knocks out has no slot left
    # by then - so a renderer that could only look the slot up silently lost
    # the whole attack.
    changes.append(Change(CHANGE_DAMAGE, player=owner, slot=slot.slot_id,
                          card=slot.top, amount=amount, detail=info))


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


def prizes_for(state: GameState, card_id) -> int:
    """How many prizes knocking this card out is worth.

    The answer is per PRINTING, not per species: Rules.prize_values is keyed by
    archetype guid, so Mewtwo is one and Mewtwo-EX is two without the engine
    knowing what "EX" means. An unknown card, or a card the database cannot
    resolve, is worth the ordinary number rather than nothing.
    """
    rules = state.rules
    if not rules.prize_values:
        return rules.prizes_per_knockout
    card = state.card(card_id)
    if card is None:
        return rules.prizes_per_knockout
    return rules.prize_values.get(card.guid, rules.prizes_per_knockout)


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
        # Read the prize value BEFORE discarding: _discard_slot empties the
        # slot, and the card that was on top of it is what the prize count
        # comes from.
        taken = prizes_for(state, slot.top)
        _discard_slot(state, owner, slot, changes)
        for _ in range(taken):
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
# effect resolution
# --------------------------------------------------------------------------
#
# The whole pending-choice machine is these five functions. See PENDING
# CHOICES in the module docstring for the model and for the one rule an
# effect has to follow.

EFFECT_TRAINER = "trainer"
EFFECT_ABILITY = "ability"
EFFECT_ATTACK = "attack"

AFTER_ATTACK = "attack"     # ... and then the attack ends the turn


def _registry(state: GameState, kind: str) -> Mapping[str, Callable]:
    return {EFFECT_TRAINER: state.rules.trainer_effects,
            EFFECT_ABILITY: state.rules.ability_effects,
            EFFECT_ATTACK: state.rules.attack_effects}[kind]


def reveal(state, changes, player, cards, reason=""):
    """Show cards to both players.

    A rules-visible event, not a rendering flourish: "your opponent reveals
    their hand" is information the opponent is entitled to, so it belongs in
    the change log rather than being left to the protocol layer to infer.
    """
    cards = [cid for cid in cards if cid is not None]
    if not cards:
        return
    changes.append(Change(CHANGE_REVEAL, player=player,
                          detail={"cards": list(cards), "reason": reason}))


def _new_ctx(state: GameState, kind: str, key: str, player: int, **extra) -> dict:
    ctx = {"kind": kind, "key": key, "player": player, "source": None,
           "slot": None, "slot_id": None, "answers": [], "data": {}}
    ctx.update(extra)
    return ctx


def _rebuild_ctx(state: GameState, pending: Pending) -> dict:
    """Reconstruct an effect's context after the state was deep-copied.

    Everything durable lives on the Pending as ids; Slot objects are looked up
    fresh, because the ones the effect saw last time belong to a state that no
    longer exists. A slot that has since left play resolves to None rather
    than raising - an effect that targeted something now gone has to cope, and
    the alternative is a crash inside a live match.
    """
    def slot_of(slot_id):
        found = state.slot(slot_id) if slot_id is not None else None
        return found[1] if found else None

    ctx = _new_ctx(state, pending.kind, pending.key, pending.player,
                   source=pending.source, slot_id=pending.slot,
                   slot=slot_of(pending.slot),
                   answers=list(pending.answers), data=dict(pending.data))
    if pending.kind == EFFECT_ATTACK:
        attacker = slot_of(pending.data.get("attacker"))
        ctx["attacker"] = attacker
        ctx["defender"] = slot_of(pending.data.get("defender"))
        ctx["damage"] = pending.data.get("damage", 0)
        ctx["base"] = pending.data.get("base", 0)
        ctx["attack"] = (state.pokemon(attacker).attack(pending.key)
                         if attacker is not None else None)
    return ctx


def _start_effect(state: GameState, effect, ctx: dict, changes: list,
                  after=None) -> bool:
    """Call an effect once. True if it finished, False if it asked something."""
    result = effect(state, ctx, changes)
    if not isinstance(result, Choice):
        return True
    state.pending = Pending(
        kind=ctx["kind"], key=ctx["key"], player=ctx["player"], choice=result,
        answers=list(ctx["answers"]), source=ctx.get("source"),
        slot=ctx.get("slot_id"), after=after, data=dict(ctx["data"]))
    changes.append(Change(CHANGE_CHOICE, player=result.player,
                          card=ctx.get("source"), detail=result.as_dict()))
    return False


def _finish_effect(state: GameState, after, changes: list):
    """Everything that has to happen once an effect is fully resolved.

    Knockouts are checked even for a Trainer, because a Trainer that moves
    damage counters or shrinks a Pokemon's HP can knock one out, and a board
    left holding a dead Pokemon is a worse bug than an unnecessary check.
    """
    if after == AFTER_ATTACK:
        _resolve_knockouts(state, changes)
        if not _check_game_end(state, changes):
            _end_turn(state, changes)
        return
    _resolve_knockouts(state, changes)
    _check_game_end(state, changes)


def _run_effect(state: GameState, kind: str, key: str, ctx: dict,
                changes: list, after=None):
    """Start an effect and, if it does not suspend, close it out."""
    effect = _registry(state, kind).get(key)
    if effect is None or _start_effect(state, effect, ctx, changes, after):
        _finish_effect(state, after, changes)


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
    ps.supporters_this_turn = 0
    ps.stadiums_this_turn = 0
    # Both sides' allowances reset, not just the turn player's: an Ability
    # used on the opponent's turn spends this turn's use, and a Modifier that
    # ran out has to stop being consulted or it never expires at all.
    for slot in state.slots():
        slot.abilities_used.clear()
    state.modifiers = [m for m in state.modifiers
                       if m.until_turn >= state.turn_number]
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
            poison = _static(state, STATIC_POISON_DAMAGE, {"slot": slot},
                             state.rules.poison_damage,
                             sources=_slot_static_sources(state, slot))
            _apply_damage(state, slot, max(0, poison), changes,
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

    Mulligans: a hand with no Basic is revealed, shuffled back and redealt, and
    the opponent is then OFFERED one card per mulligan - one yes/no answer
    each, which is how the original server asked. The offers are answered
    before placement rather than after it, because the cards drawn may be what
    gets placed. Rules.optional_mulligan_draw False draws them all here instead
    and asks nothing.

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

    # Only the DIFFERENCE is drawn. Mulligans cancel: if both players took
    # ten, neither draws anything, and if one took five and the other two,
    # only the three extra are owed. Paying the full count both ways gave two
    # players who each mulliganed ten an eleven-card hand apiece.
    for p in (0, 1):
        extra = max(0, state.players[1 - p].mulligans - state.players[p].mulligans)
        if not extra:
            continue
        if rules.optional_mulligan_draw:
            state.players[p].owed_draws = extra
            state.players[p].owed_draws_total = extra
        else:
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
    """Who legal_actions() will offer anything to, in priority order.

    An outstanding Choice outranks everything, including a promotion: an
    effect stopped half way through and nothing else in the game may happen
    until it is finished. It also need not be the turn player who answers -
    Escape Rope asks the opponent first - so this reads the Choice's own
    player rather than assuming.
    """
    if state.over:
        return []
    if state.pending is not None:
        return [state.pending.choice.player]
    # Mulligan compensation is taken AFTER both boards are set up, and only
    # then. The rulebook is explicit: while one player is mulliganing the other
    # "sets his Active Pokemon, and as many Benched Pokemon as desired", and
    # only "when that player no longer has a mulligan" does the opponent draw.
    # A Basic among those cards "cannot replace your Active Pokemon" - the
    # Active must come from the original seven - so drawing first would let a
    # compensation card become the Active, which is exactly wrong.
    if state.phase != PHASE_SETUP or all(p.setup_done for p in state.players):
        owed = [p.index for p in state.players if p.owed_draws > 0]
        if owed:
            return owed[:1]
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


def _can_play_trainer(state: GameState, player: int, card: Card) -> bool:
    """Structural legality for an Item, a Supporter or a Stadium.

    The first test is the important one: a Trainer with no entry in
    Rules.trainer_effects is not offered at all. A Trainer whose text is not
    implemented is not a partially-working card, it is a card that silently
    does nothing, and letting a player spend their one Supporter of the turn
    on nothing is worse than leaving it in hand. This is the same call the
    engine already makes about Energy whose text it cannot read.
    """
    effect = state.rules.trainer_effects.get(card.guid)
    if effect is None:
        return False
    # An effect may carry a `playable(state, player)` guard for the rule that
    # a card you cannot do anything with cannot be played: no Potion with an
    # undamaged board, no Switch with an empty bench. Without it a player would
    # spend their one Supporter of the turn on nothing, and the AI would do it
    # every turn because the card was offered.
    guard = getattr(effect, "playable", None)
    if guard is not None and not guard(state, player):
        return False
    ps = state.players[player]
    if card.is_supporter:
        return ps.supporters_this_turn < state.rules.supporters_per_turn
    if card.is_stadium:
        if ps.stadiums_this_turn >= state.rules.stadiums_per_turn:
            return False
        # A Stadium may not replace one of the same name - otherwise a player
        # holding two copies could re-play it every turn to reset it.
        return not (state.stadium is not None
                    and state.card(state.stadium.card).name == card.name)
    return card.is_item


def _can_attach_tool(state: GameState, card: Card, slot: Slot) -> bool:
    return (card.guid in state.rules.trainer_effects
            and len(slot.tools) < state.rules.tools_per_pokemon)


def _usable_abilities(state: GameState, player: int, slot: Slot) -> list:
    """Abilities on this Pokemon that can be activated right now.

    Nothing in the card data says whether an Ability is activated or passive,
    so presence in Rules.ability_effects is the test. That means a passive
    Ability is simply never registered there, and an unimplemented one is
    never offered - which is the same "blank beats wrong" rule the Trainers
    follow above.
    """
    if not state.rules.ability_effects or not _abilities_active(state, slot):
        return []
    return [a for a in state.pokemon(slot).pokemon_abilities
            if a.ability_id in state.rules.ability_effects
            and a.ability_id not in slot.abilities_used]


def _choice_actions(state: GameState) -> list:
    """Every answer to the outstanding Choice, up to the enumeration cap.

    Combinations are produced smallest first so an optional choice always
    offers "decline" even when the cap truncates the rest - declining must
    never become impossible because the option list was long.
    """
    choice = state.pending.choice
    cap = state.rules.max_enumerated_choices
    out = []
    for size in range(choice.minimum, choice.maximum + 1):
        for combo in itertools.combinations(choice.options, size):
            out.append(Choose(choice.player, combo))
            if len(out) >= cap:
                return out
    return out


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

    if state.pending is not None:
        return _choice_actions(state)

    # Only once both boards are placed - the same gate players_to_act uses.
    # Asking sooner would let a compensation card become the Active, and the
    # rulebook is explicit that the Active comes from the original seven.
    if ps.owed_draws > 0 and (state.phase != PHASE_SETUP
                              or all(p.setup_done for p in state.players)):
        # One offer, two answers, however many are outstanding. Both are always
        # legal because either one spends exactly one offer, so the player is
        # asked owed_draws times and can decline any of them separately.
        return [DrawMulligans(player, True), DrawMulligans(player, False)]

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
        elif card.is_trainer:
            if card.is_tool:
                actions += [AttachTool(player, cid, slot.slot_id)
                            for slot in in_play
                            if _can_attach_tool(state, card, slot)]
            elif _can_play_trainer(state, player, card):
                actions.append(PlayTrainer(player, cid))

    for slot in in_play:
        actions += [UseAbility(player, slot.slot_id, ability.ability_id)
                    for ability in _usable_abilities(state, player, slot)]

    if (ps.active is not None and ps.bench
            and ps.retreats_this_turn < state.rules.retreats_per_turn
            and ASLEEP not in ps.active.conditions
            and PARALYZED not in ps.active.conditions
            and not _has_modifier(state, MOD_NO_RETREAT, ps.active, player)
            and not (state.rules.confusion_blocks_retreat
                     and CONFUSED in ps.active.conditions)):
        cost = retreat_cost(state, ps.active)
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
    if state.pending is not None and not isinstance(action, Choose):
        raise IllegalAction("a choice is outstanding: %s"
                            % state.pending.choice.prompt)

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

    _finish_setup(state, changes)


def _finish_setup(state, changes):
    """Leave setup, once nothing is still owed.

    Compensation draws sit between the last SetupDone and the prizes, so this
    is reached twice when anyone is owed: once to stop and ask, and again from
    _do_draw_mulligans when the last one is answered.
    """
    if any(p.owed_draws > 0 for p in state.players):
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

    evolve_slot(state, action.player, action.card, slot, changes)


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
    _require(not _has_modifier(state, MOD_NO_RETREAT, active, action.player),
             "this Pokemon cannot retreat right now")

    incoming = _own_slot(state, action.player, action.slot)
    _require(incoming in ps.bench, "slot %r is not on the bench" % (action.slot,))

    cost = retreat_cost(state, active)
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

    # The attacker is named by CARD as well as by slot: an attack that knocks
    # its own Pokemon out - recoil, or a mutual knockout - leaves no slot to
    # look up by the time the changes are rendered.
    changes.append(Change(CHANGE_ATTACK, player=action.player,
                          slot=attacker_slot.slot_id,
                          card=attacker_slot.top,
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

    ctx = _new_ctx(state, EFFECT_ATTACK, attack.ability_id, action.player,
                   attack=attack, attacker=attacker_slot,
                   defender=defender_slot, slot_id=attacker_slot.slot_id,
                   slot=attacker_slot)
    ctx["data"]["attacker"] = attacker_slot.slot_id
    ctx["data"]["defender"] = defender_slot.slot_id

    # EXTENSION POINT 1: what the printed "30+" / "40x" / "80-" actually
    # resolves to for this use of the attack. Runs before the damage lands,
    # because an "x" attack whose flips all came up tails does zero and there
    # is no way to un-deal 40. Coin flips it makes go in ctx["data"] so the
    # after-effect below can read the same result rather than flipping again.
    base = attack.damage
    scale = state.rules.attack_damage.get(attack.ability_id)
    if scale is not None:
        base = max(0, int(scale(state, ctx, changes)))
    ctx["base"] = base
    ctx["data"]["base"] = base

    # An attack_damage hook may also declare what its damage ignores, by
    # writing IGNORE_* strings into ctx["data"]["ignore"]. It is set there
    # rather than returned because the hook's return value is the number.
    dealt = _attack_damage(state, attacker_slot, defender_slot, base,
                           ignore=tuple(ctx["data"].get("ignore") or ()))
    _apply_damage(state, defender_slot, dealt, changes,
                  {"abilityID": attack.ability_id,
                   "baseDamage": base,
                   "printedDamage": attack.damage,
                   "weakness": bool(set(attacker.types) & set(defender.weakness_types)),
                   "resistance": bool(defender.resistance_type
                                      and defender.resistance_type in attacker.types)})
    ctx["damage"] = dealt
    ctx["data"]["damage"] = dealt

    # EXTENSION POINT 2: everything else the attack's game text says.
    _run_effect(state, EFFECT_ATTACK, attack.ability_id, ctx, changes,
                after=AFTER_ATTACK)


def _do_play_trainer(state, action, changes):
    """Play an Item, a Supporter or a Stadium.

    The card leaves the hand before its own effect runs, which matters for
    "discard 2 cards from your hand" - Ultra Ball is not one of the two. Items
    and Supporters go straight to the discard rather than waiting for the
    effect to finish; that is one step earlier than the printed rules and is
    visible only to an effect that reads its own owner's discard pile, none of
    which exist (Energy Retrieval and VS Seeker both read it, and neither can
    name itself: an Item is not a basic Energy and is not a Supporter).
    """
    ps = state.players[action.player]
    _require(state.phase == PHASE_MAIN, "not the main phase")
    card = _hand_card(state, action.player, action.card)
    _require(card.is_trainer, "%s is not a Trainer card" % card.name)
    _require(not card.is_tool,
             "%s is a Pokemon Tool: play it with AttachTool" % card.name)
    _require(card.guid in state.rules.trainer_effects,
             "%s has no implemented effect" % card.name)
    _require(_can_play_trainer(state, action.player, card),
             "%s cannot be played right now" % card.name)

    ps.hand.remove(action.card)
    changes.append(Change(CHANGE_PLAY, player=action.player, card=action.card,
                          detail={"kind": card.trainer_kind, "name": card.name}))

    if card.is_stadium:
        _place_stadium(state, action.player, action.card, changes)
        ps.stadiums_this_turn += 1
    else:
        if card.is_supporter:
            ps.supporters_this_turn += 1
        ps.discard.append(action.card)
        changes.append(Change(CHANGE_MOVE, player=action.player,
                              card=action.card, from_zone=ZONE_HAND,
                              to_zone=ZONE_DISCARD))

    ctx = _new_ctx(state, EFFECT_TRAINER, card.guid, action.player,
                   source=action.card)
    _run_effect(state, EFFECT_TRAINER, card.guid, ctx, changes)


def _place_stadium(state, player, cid, changes):
    """Put a Stadium into play, discarding whatever it replaced.

    The old Stadium goes to the discard pile of whoever PLAYED it, not of
    whoever replaced it - a Stadium never changes owner, it only changes who
    it is helping.
    """
    if state.stadium is not None:
        old = state.stadium
        state.players[old.owner].discard.append(old.card)
        changes.append(Change(CHANGE_MOVE, player=old.owner, card=old.card,
                              from_zone=ZONE_STADIUM, to_zone=ZONE_DISCARD))
    state.stadium = StadiumInPlay(card=cid, owner=player)
    changes.append(Change(CHANGE_MOVE, player=player, card=cid,
                          from_zone=ZONE_HAND, to_zone=ZONE_STADIUM))
    changes.append(Change(CHANGE_STADIUM, player=player, card=cid,
                          detail={"name": state.card(cid).name}))


def _do_attach_tool(state, action, changes):
    ps = state.players[action.player]
    _require(state.phase == PHASE_MAIN, "not the main phase")
    card = _hand_card(state, action.player, action.card)
    _require(card.is_tool, "%s is not a Pokemon Tool" % card.name)
    _require(card.guid in state.rules.trainer_effects,
             "%s has no implemented effect" % card.name)
    slot = _own_slot(state, action.player, action.slot)
    _require(len(slot.tools) < state.rules.tools_per_pokemon,
             "%s already has a Pokemon Tool attached" % state.pokemon(slot).name)

    ps.hand.remove(action.card)
    slot.tools.append(action.card)
    changes.append(Change(CHANGE_MOVE, player=action.player, card=action.card,
                          slot=slot.slot_id, from_zone=ZONE_HAND,
                          to_zone=ZONE_ACTIVE if ps.active is slot else ZONE_BENCH))
    changes.append(Change(CHANGE_TOOL, player=action.player, card=action.card,
                          slot=slot.slot_id, detail={"name": card.name}))

    # A Tool's registry entry is its ON-ATTACH effect, and most Tools have
    # none - their text is continuous and lives in Rules.static_effects. An
    # entry that is only there to make the card playable does nothing here.
    ctx = _new_ctx(state, EFFECT_TRAINER, card.guid, action.player,
                   source=action.card, slot=slot, slot_id=slot.slot_id)
    _run_effect(state, EFFECT_TRAINER, card.guid, ctx, changes)


def _do_use_ability(state, action, changes):
    _require(state.phase == PHASE_MAIN, "not the main phase")
    slot = _own_slot(state, action.player, action.slot)
    _require(_abilities_active(state, slot),
             "Abilities are switched off right now")
    ability = next((a for a in state.pokemon(slot).pokemon_abilities
                    if a.ability_id == action.ability_id), None)
    _require(ability is not None, "%s has no Ability %r"
             % (state.pokemon(slot).name, action.ability_id))
    _require(action.ability_id in state.rules.ability_effects,
             "%s is not an implemented Ability" % ability.title)
    _require(action.ability_id not in slot.abilities_used,
             "%s has already been used this turn" % ability.title)

    slot.abilities_used.add(action.ability_id)
    changes.append(Change(CHANGE_ABILITY, player=action.player,
                          slot=slot.slot_id, card=slot.top,
                          detail={"abilityID": ability.ability_id,
                                  "title": ability.title}))
    ctx = _new_ctx(state, EFFECT_ABILITY, action.ability_id, action.player,
                   slot=slot, slot_id=slot.slot_id, source=slot.top)
    _run_effect(state, EFFECT_ABILITY, action.ability_id, ctx, changes)


def _do_choose(state, action, changes):
    """Answer the outstanding Choice and let the effect run on.

    Validation is deliberately from scratch rather than "is this one of the
    actions legal_actions() offered": that list is capped, and an answer it
    declined to enumerate is still a legal answer.
    """
    pending = state.pending
    _require(pending is not None, "nothing is waiting on a choice")
    choice = pending.choice
    _require(action.player == choice.player,
             "the choice %r belongs to player %d" % (choice.prompt, choice.player))

    picks = tuple(action.picks)
    _require(len(set(picks)) == len(picks), "the same option was picked twice")
    for pick in picks:
        _require(pick in choice.options,
                 "%r is not an option for %r" % (pick, choice.prompt))
    _require(choice.minimum <= len(picks) <= choice.maximum,
             "%r takes between %d and %d picks, got %d"
             % (choice.prompt, choice.minimum, choice.maximum, len(picks)))

    changes.append(Change(CHANGE_CHOSE, player=action.player,
                          detail={"prompt": choice.prompt,
                                  "picks": list(picks),
                                  "optionKind": choice.option_kind}))

    state.pending = None
    pending.answers.append(picks)
    ctx = _rebuild_ctx(state, pending)
    effect = _registry(state, pending.kind).get(pending.key)
    if effect is None or _start_effect(state, effect, ctx, changes,
                                       pending.after):
        _finish_effect(state, pending.after, changes)


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


def _do_draw_mulligans(state, action, changes):
    """Answer one outstanding offer, yes or no.

    The decrement is unconditional: the question is asked again while
    owed_draws stands, so an answer that left the count alone - which is what
    a declined draw would naturally do - would ask it for ever. Declining
    therefore looks identical to taking from the state machine's point of
    view, and differs only in whether a card moves.
    """
    ps = state.players[action.player]
    _require(ps.owed_draws > 0, "no mulligan draw is outstanding")
    ps.owed_draws -= 1
    if action.take:
        _draw(state, action.player, 1, changes)

    # These are answered between the last SetupDone and the prizes, so the
    # last answer is what actually starts the game.
    if state.phase == PHASE_SETUP and all(p.setup_done for p in state.players):
        _finish_setup(state, changes)


_HANDLERS = {
    SetupPlaceActive: _do_setup_place_active,
    SetupPlaceBench: _do_setup_place_bench,
    SetupDone: _do_setup_done,
    PlayBasic: _do_play_basic,
    Evolve: _do_evolve,
    AttachEnergy: _do_attach_energy,
    PlayTrainer: _do_play_trainer,
    AttachTool: _do_attach_tool,
    UseAbility: _do_use_ability,
    Choose: _do_choose,
    Retreat: _do_retreat,
    Attack: _do_attack,
    Promote: _do_promote,
    Pass: _do_pass,
    DrawMulligans: _do_draw_mulligans,
}


# --------------------------------------------------------------------------
# EFFECT PRIMITIVES
# --------------------------------------------------------------------------
#
# The supported surface for an effect in Rules.trainer_effects,
# ability_effects or attack_effects. Everything here mutates the state it is
# given and appends to `changes`; effects should reach for these rather than
# poking at PlayerState lists, because these emit the Changes the protocol
# layer needs and the lists do not.

ZONE_LISTS = {ZONE_DECK: "deck", ZONE_HAND: "hand", ZONE_DISCARD: "discard",
              ZONE_PRIZES: "prizes", ZONE_LOST: "lost"}


def zone_of(state: GameState, cid: int):
    """(player, zone, list) for a loose card, or None if it is in play.

    Cards attached to or stacked on a Pokemon are not in a zone list, and
    deliberately return None: an effect that wants one of those has to go
    through the Slot, where the accounting for evolution stacks and Energy
    lives.
    """
    owner = state.owner_of(cid)
    ps = state.players[owner]
    for zone, attr in ZONE_LISTS.items():
        pile = getattr(ps, attr)
        if cid in pile:
            return owner, zone, pile
    return None


def move_card(state: GameState, cid: int, to_zone: str, changes: list,
              detail=None) -> bool:
    """Move a loose card between its owner's zones. False if it was in play.

    Appends to the destination, which for ZONE_DECK means the BOTTOM - a
    "put it on top of your deck" effect has to insert at 0 itself and say so.
    """
    found = zone_of(state, cid)
    if found is None:
        return False
    owner, from_zone, pile = found
    if from_zone == to_zone:
        return True
    pile.remove(cid)
    getattr(state.players[owner], ZONE_LISTS[to_zone]).append(cid)
    changes.append(Change(CHANGE_MOVE, player=owner, card=cid,
                          from_zone=from_zone, to_zone=to_zone,
                          detail=dict(detail or {})))
    return True


def heal(state: GameState, slot: Slot, amount: int, changes: list) -> int:
    """Remove up to `amount` damage. Returns how much actually came off."""
    healed = min(slot.damage, max(0, amount))
    if not healed:
        return 0
    slot.damage -= healed
    changes.append(Change(CHANGE_HEAL, player=state.owner_of(slot.top),
                          slot=slot.slot_id, amount=healed,
                          detail={"total": slot.damage,
                                  "maxHP": state.max_hp(slot)}))
    return healed


def discard_attached(state: GameState, slot: Slot, cids, changes: list):
    """Send attached Energy or Tools from a Pokemon to its owner's discard."""
    owner = state.owner_of(slot.top)
    ps = state.players[owner]
    zone = ZONE_ACTIVE if ps.active is slot else ZONE_BENCH
    for cid in list(cids):
        if cid in slot.energy:
            slot.energy.remove(cid)
        elif cid in slot.tools:
            slot.tools.remove(cid)
        else:
            continue
        ps.discard.append(cid)
        changes.append(Change(CHANGE_MOVE, player=owner, card=cid,
                              slot=slot.slot_id, from_zone=zone,
                              to_zone=ZONE_DISCARD))


def bench_card(state: GameState, player: int, cid: int, changes: list) -> bool:
    """Put a Basic straight onto the bench from wherever it currently is.

    Nest Ball takes one out of the deck and Revive out of the discard; both
    skip the hand entirely, which is why this is not PlayBasic.
    """
    ps = state.players[player]
    if len(ps.bench) >= state.rules.bench_size:
        return False
    found = zone_of(state, cid)
    if found is None:
        return False
    _, from_zone, pile = found
    pile.remove(cid)
    slot = _new_slot(state, cid, ps.turns_taken)
    ps.bench.append(slot)
    changes.append(Change(CHANGE_MOVE, player=player, card=cid,
                          slot=slot.slot_id, from_zone=from_zone,
                          to_zone=ZONE_BENCH))
    return True


def evolve_slot(state: GameState, player: int, cid: int, slot: Slot,
                changes: list):
    """Put an evolution card from hand onto a Pokemon in play.

    Shared with Rare Candy, which skips the stage check but does exactly this
    afterwards - the timing rules that _can_evolve_onto() enforces are the
    caller's business, and the mechanics of the stack are this function's.
    """
    ps = state.players[player]
    ps.hand.remove(cid)
    # Damage stays on the Pokemon through evolution; Special Conditions do not.
    _clear_conditions(state, slot, changes, "evolved")
    previous = slot.top
    slot.stack.append(cid)
    slot.played_on_turn = ps.turns_taken
    slot.abilities_used.clear()   # a different Pokemon, with its own allowance
    card = state.card(cid)
    changes.append(Change(CHANGE_MOVE, player=player, card=cid,
                          slot=slot.slot_id, from_zone=ZONE_HAND,
                          to_zone=ZONE_ACTIVE if ps.active is slot else ZONE_BENCH))
    changes.append(Change(CHANGE_EVOLVE, player=player, card=cid,
                          slot=slot.slot_id,
                          detail={"from": previous, "name": card.name,
                                  "damage": slot.damage,
                                  "maxHP": card.max_hp}))


def switch_active(state: GameState, player: int, slot_id: int, changes: list):
    """Promote a benched Pokemon with no retreat cost and no once-per-turn.

    This is what Switch, Escape Rope, Lysandre and every "switch" attack do.
    It is not Retreat: no Energy is paid and the turn's retreat allowance is
    untouched. Special Conditions still come off, because they come off any
    time a Pokemon leaves the Active spot.
    """
    ps = state.players[player]
    incoming = next((s for s in ps.bench if s.slot_id == slot_id), None)
    if incoming is None or ps.active is None:
        return False
    outgoing = ps.active
    _clear_conditions(state, outgoing, changes, "switched")
    ps.bench.remove(incoming)
    ps.bench.append(outgoing)
    ps.active = incoming
    changes.append(Change(CHANGE_PROMOTE, player=player, slot=incoming.slot_id,
                          card=incoming.top, from_zone=ZONE_BENCH,
                          to_zone=ZONE_ACTIVE,
                          detail={"switched": outgoing.slot_id}))
    return True


# Re-exported under names an effect author would look for. The underscored
# originals stay where they are: they are called from inside the turn machine,
# which predates the idea of an effect calling them.
flip_coin = _flip
draw_cards = _draw
shuffle_deck = _shuffle_deck
apply_damage = _apply_damage
add_condition = _add_condition
clear_conditions = _clear_conditions
add_modifier = _add_modifier


# --------------------------------------------------------------------------
# EXTENSION POINTS
# --------------------------------------------------------------------------
#
# Five registries on Rules, all empty here, all populated by effects.py:
#
#     attack_damage    abilityID     -> f(state, ctx) -> int
#     attack_effects   abilityID     -> f(state, ctx, changes) -> Choice|None
#     trainer_effects  archetype GUID-> f(state, ctx, changes) -> Choice|None
#     ability_effects  abilityID     -> f(state, ctx, changes) -> Choice|None
#     static_effects   either        -> f(query, state, ctx, value) -> value
#
# ctx is a dict; the keys every effect gets are "player", "kind", "key",
# "source" (the cid played, if any), "slot"/"slot_id" (the source Pokemon, if
# any), "answers" (a list of pick-tuples, one per Choice already answered) and
# "data" (scratch that survives suspension). An attack effect additionally gets
# "attack", "attacker", "defender", "base" and "damage".
#
# What is still missing, in the order it will hurt:
#
# 1. Triggered abilities. Everything here is either activated (a player says
#    so) or continuous (a number is read through it). Nothing fires on an
#    event - "when this Pokemon is Knocked Out", "when you attach an Energy",
#    Rocky Helmet's counter-damage. That needs the knockout and attach paths to
#    call a trigger registry, and it is the single biggest remaining gap.
#
# 2. Prize counts and prize choice. Rules.prizes_per_knockout is a flat number
#    because carddata has no verified "has a rule box" attribute; when one is
#    identified it becomes a lookup on the knocked-out Card. Cards that let a
#    player choose which prize to take can now use the Choice machinery -
#    _take_prize is the only thing that has to change.
#
# 3. Choices that are not a pick from a list: ordering the top five cards of
#    your deck, naming a card, choosing an amount. Choice.options is a flat
#    tuple and every one of those needs a different shape. CHOICE_OPTION with
#    encoded strings covers the small cases and nothing more.
#
# 4. Two outstanding choices at once. GameState.pending is one slot, so an
#    effect that would ask both players simultaneously has to ask them in
#    order. Every card in effects.py does ask in order (that is the printed
#    rule), but a card that genuinely needs both at once cannot be written.
