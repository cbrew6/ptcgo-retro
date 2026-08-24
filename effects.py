"""
Per-card behaviour for engine.py, built from the client's own English text.

engine.py knows the structural rules and nothing else: it will tell you that a
Supporter is once a turn and that a Stadium replaces the Stadium in play, and
it has no idea what Professor Sycamore does. What each card does lives here, in
the registries engine.Rules declares and leaves empty. Import this module,
call build_rules(), and hand the result to new_game().

    rules = effects.build_rules(db)
    state, changes = engine.new_game(db, decks, rules=rules)


WHY THIS IS DRIVEN BY TEXT AND NOT BY CARD NAME
-----------------------------------------------

carddata has no field saying what a card does. It has a localization KEY -
attribute 200310 for a Trainer, `gameText` inside attribute 200740 for an
attack - and the English those keys resolve to ships with the client in
StreamingAssets\\LocalizationDB-UTF16.db. 1,094 of the 1,120 Trainer printings
and 9,116 of the 12,204 attacks resolve; the rest are sets whose localization
never arrived before the servers died.

So the registries are built by *reading the card*. Each entry below is a
regular expression over the real English text plus a builder that turns the
numbers in that text into an effect. Three things fall out of that, all of
which matter more than the convenience:

  * One pattern covers every printing. "Flip a coin. If heads, this attack
    does N more damage." is 435 different attacks with 435 different
    abilityIDs, and reprints do NOT share an abilityID (7,038 distinct ids
    across 12,204 attacks; only 20 are shared between two card names).

  * A reprint whose wording changed does not silently inherit the old
    behaviour. It fails to match, gets no entry, and stays unplayable - which
    is the same call CLAUDE.md makes about substituting card art: blank beats
    wrong, because a wrong card misstates the rules during a game.

  * The numbers come from the text, not from the `damage` field. They disagree:
    Cinccino's "Does 20 damage times the number of your Benched Pokemon" has
    damage=0, while Simisear's "Flip 3 coins. This attack does 40 damage times
    the number of heads" has damage=40. Reading the text is right both times.

amountOperator was checked rather than assumed. Over the whole card pool it
takes exactly four values: "" (10,804), "+" (1,640), "x" (951) and "-" (25).
It is the printed suffix on the damage box and says only that the number is
conditional - it never says on what - so it is used as a cross-check on a
matched pattern and never as the source of an effect.


WRITING AN EFFECT
-----------------

An effect is `f(state, ctx, changes)`. It may return a Choice to suspend; see
PENDING CHOICES in engine.py for the model and for the one rule it must obey
(a call that returns a Choice must not have touched state or changes). The
`step()` helper below is the idiom for that:

    def effect(state, ctx, changes):
        target, choice = step(ctx, 0, lambda: Choice(...))
        if choice is not None:
            return choice
        ...                       # every mutation, once, at the end

step() also resolves a choice that has only one legal answer without a round
trip, so an attack that discards an Energy from a Pokemon carrying exactly one
Energy does not open a dialog with a single button in it.

An effect may carry a `.playable(state, player)` guard, which is what stops a
Potion being offered against an undamaged board.
"""

from __future__ import annotations

import dataclasses
import os
import re
import sqlite3

import engine
from engine import (
    CHOICE_CARD,
    CHOICE_SLOT,
    Choice,
    MOD_DAMAGE_DEALT,
    MOD_DAMAGE_TAKEN,
    MOD_NO_ABILITIES,
    MOD_NO_RETREAT,
    Modifier,
    STATIC_DAMAGE_DEALT,
    STATIC_DAMAGE_TAKEN,
    STATIC_MAX_HP,
    STATIC_NO_WEAKNESS,
    STATIC_RETREAT_COST,
    ZONE_DECK,
    ZONE_DISCARD,
    ZONE_HAND,
)

# --------------------------------------------------------------------------
# the client's localization database
# --------------------------------------------------------------------------
#
# The same file server.py serves to the client, read the same way. It is
# reached from here rather than imported from server.py so this module keeps
# engine.py's property of having no server dependency: nothing in the import
# graph below opens a socket.
#
# The keys inside carddata are mixed case and the table's are lowercase, which
# is not a formatting detail - looking one up unfolded silently misses and a
# missed lookup here means a card quietly loses its effect.

HERE = os.path.dirname(os.path.abspath(__file__))
LOCALIZATION_DB = os.path.join(
    os.path.dirname(HERE), "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data", "StreamingAssets",
    "LocalizationDB-UTF16.db")


def load_localization(path=None) -> dict:
    """key (lowercased) -> English string. {} if the database is not there.

    An absent database is not an error: it makes build_rules() return a Rules
    with empty registries, which is exactly the engine's default behaviour and
    lets the tests run on a machine with no game installed.
    """
    path = path or LOCALIZATION_DB
    if not os.path.exists(path):
        return {}
    con = sqlite3.connect("file:%s?mode=ro" % path.replace("\\", "/"), uri=True)
    try:
        rows = con.execute("select key, value from Lookup").fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    return {k.lower(): v for k, v in rows if k}


# Wording drifts between eras without the rule changing: "his or her" became
# "their", "show it to your opponent" became "reveal it", and the curly
# apostrophe appears in the SM sets only. Folding those here means one pattern
# per rule instead of one per printing - Escape Rope alone has two wordings of
# the same sentence, and Switch, Ultra Ball, Rare Candy and Max Potion all do.
_FOLD = [
    ("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'),
    ("his or her", "their"), ("he or she", "they"), ("him or her", "them"),
    ("show it to your opponent", "reveal it"),
    ("show them to your opponent", "reveal them"),
]


def normalize(text) -> str:
    """Collapse whitespace and fold the wordings that never meant anything."""
    if not text:
        return ""
    out = " ".join(str(text).split())
    for old, new in _FOLD:
        out = out.replace(old, new)
    return out


def trainer_text(card, loc) -> str:
    return normalize(loc.get((card.game_text_key or "").lower()))


def ability_text(ability, loc) -> str:
    key = (ability.game_text or "").strip('"').strip("$").lower()
    return normalize(loc.get(key))


# --------------------------------------------------------------------------
# writing effects
# --------------------------------------------------------------------------

def step(ctx, index, make_choice):
    """Get answer `index`, asking for it if it is not in yet.

    Returns (picks, choice). Exactly one is meaningful: when `choice` is not
    None the effect must return it immediately and do nothing else.

    A Choice with no more options than its minimum has exactly one legal
    answer, so it is answered here rather than being sent to a player who has
    no decision to make. make_choice() may also return None outright, which
    means "there is nothing to choose" and yields an empty answer.
    """
    answers = ctx["answers"]
    if index < len(answers):
        return answers[index], None
    choice = make_choice()
    if choice is None:
        answers.append(())
        return (), None
    if len(choice.options) <= choice.minimum:
        picks = tuple(choice.options)
        answers.append(picks)
        return picks, None
    return None, choice


def playable(guard):
    """Attach a "can this card do anything right now" test to an effect."""
    def decorate(effect):
        effect.playable = guard
        return effect
    return decorate


def slot_of(state, slot_id):
    found = state.slot(slot_id) if slot_id is not None else None
    return found[1] if found else None


def distinct(state, cids) -> tuple:
    """One card id per archetype, in order.

    Searching a deck for a Pokemon over sixty cards would offer sixty answers,
    most of them the same card twice. Collapsing them is the same call
    _retreat_payments() already makes about which of two identical Energy to
    discard: it is not a decision, and enumerating it floods legal_actions().
    """
    seen, out = set(), []
    for cid in cids:
        key = state.cards[cid].archetype
        if key in seen:
            continue
        seen.add(key)
        out.append(cid)
    return tuple(out)


def own_slots(state, player) -> list:
    return state.players[player].in_play


def damaged(state, player) -> list:
    return [s for s in state.players[player].in_play if s.damage]


def slot_ids(slots) -> tuple:
    return tuple(s.slot_id for s in slots)


def attached_energy(state, slots) -> tuple:
    out = []
    for slot in slots:
        out += list(slot.energy)
    return tuple(out)


def slot_holding(state, player, cid):
    for slot in state.players[player].in_play:
        if cid in slot.energy or cid in slot.tools:
            return slot
    return None


def is_basic_energy(state, cid) -> bool:
    return state.card(cid).is_basic_energy


def is_special_energy(state, cid) -> bool:
    card = state.card(cid)
    return card.is_energy and not card.is_basic_energy


def flips(state, changes, count, reason, player=None) -> int:
    return sum(1 for _ in range(count)
               if engine.flip_coin(state, changes, reason, player))


def flip_until_tails(state, changes, reason, player=None, cap=20) -> int:
    heads = 0
    while heads < cap and engine.flip_coin(state, changes, reason, player):
        heads += 1
    return heads


def shuffle_into_deck(state, player, cids, changes):
    for cid in cids:
        engine.move_card(state, cid, ZONE_DECK, changes)
    engine.shuffle_deck(state, player, changes)


def to_hand(state, cids, changes):
    for cid in cids:
        engine.move_card(state, cid, ZONE_HAND, changes,
                         detail={"revealed": True})


# --------------------------------------------------------------------------
# the pattern tables
# --------------------------------------------------------------------------
#
# Each table is a list of (compiled pattern, builder). A builder is handed the
# regex match and the Card (or Ability) it matched on, and returns the
# callable that goes in the registry - or None to decline, which is how a
# pattern that matched the words but not the situation backs out.
#
# Patterns are anchored at both ends. A card whose text is the known sentence
# PLUS another sentence is a different card, and half-implementing it would be
# the exact failure this module exists to avoid.

TRAINERS = []
ATTACK_DAMAGE = []
ATTACK_EFFECTS = []
STATICS = []          # Pokemon Tools: (on-attach effect, continuous hook)
ABILITIES = []        # activated Abilities, once per turn per Pokemon
ABILITY_STATICS = []  # continuous Abilities


def _register(table, pattern):
    def decorate(builder):
        table.append((re.compile(pattern + r"$"), builder))
        return builder
    return decorate


def trainer(pattern):
    return _register(TRAINERS, pattern)


def attack_damage(pattern):
    return _register(ATTACK_DAMAGE, pattern)


def attack_effect(pattern):
    return _register(ATTACK_EFFECTS, pattern)


def static(pattern):
    return _register(STATICS, pattern)


def ability(pattern):
    return _register(ABILITIES, pattern)


def ability_static(pattern):
    return _register(ABILITY_STATICS, pattern)


N = r"(\d+)"
POKEMON = "Pokémon"


# --------------------------------------------------------------------------
# Supporters and Items: drawing
# --------------------------------------------------------------------------

@trainer(r"Draw a card\.")
@trainer(r"Draw " + N + r" cards\.")
def _draw_n(m, card):
    count = int(m.group(1)) if m.groups() else 1

    def effect(state, ctx, changes):
        engine.draw_cards(state, ctx["player"], count, changes)
    return effect


@trainer(r"Discard your hand and draw " + N + r" cards\.")
def _discard_hand_draw(m, card):
    """Professor Sycamore, Professor Juniper."""
    count = int(m.group(1))

    def effect(state, ctx, changes):
        ps = state.players[ctx["player"]]
        for cid in list(ps.hand):
            engine.move_card(state, cid, ZONE_DISCARD, changes)
        engine.draw_cards(state, ctx["player"], count, changes)
    return effect


@trainer(r"Shuffle your hand into your deck\. Then,? draw " + N + r" cards\.")
def _shuffle_hand_draw(m, card):
    """Shauna, Professor Oak's New Theory, Cynthia."""
    count = int(m.group(1))

    def effect(state, ctx, changes):
        player = ctx["player"]
        shuffle_into_deck(state, player, list(state.players[player].hand), changes)
        engine.draw_cards(state, player, count, changes)
    return effect


@trainer(r"Draw cards until you have " + N + r" cards in your hand\.")
def _draw_up_to(m, card):
    """Bianca."""
    target = int(m.group(1))

    def effect(state, ctx, changes):
        ps = state.players[ctx["player"]]
        engine.draw_cards(state, ctx["player"], max(0, target - len(ps.hand)),
                          changes)
    return effect


@trainer(r"Draw cards until you have " + N + r" cards in your hand\. "
         r"If it's your first turn, draw cards until you have " + N
         + r" cards in your hand\.")
def _lillie(m, card):
    normal, first = int(m.group(1)), int(m.group(2))

    def effect(state, ctx, changes):
        ps = state.players[ctx["player"]]
        target = first if ps.turns_taken <= 1 else normal
        engine.draw_cards(state, ctx["player"], max(0, target - len(ps.hand)),
                          changes)
    return effect


@trainer(r"Each player shuffles their hand into their deck\. Then, each player "
         r"draws a card for each of their remaining Prize cards\.")
def _n_supporter(m, card):
    """N. Both hands go back before either player draws, which is the printed
    order and is not cosmetic: your own shuffle must not be able to change how
    many cards your opponent draws."""
    def effect(state, ctx, changes):
        for p in (0, 1):
            shuffle_into_deck(state, p, list(state.players[p].hand), changes)
        for p in (0, 1):
            engine.draw_cards(state, p, len(state.players[p].prizes), changes)
    return effect


@trainer(r"Each player shuffles their hand into their deck and draws " + N
         + r" cards\.")
def _judge(m, card):
    count = int(m.group(1))

    def effect(state, ctx, changes):
        for p in (0, 1):
            shuffle_into_deck(state, p, list(state.players[p].hand), changes)
        for p in (0, 1):
            engine.draw_cards(state, p, count, changes)
    return effect


@trainer(r"Shuffle your hand into your deck\. Then, draw a number of cards "
         r"equal to the number of cards in your opponent's hand\.")
def _copycat(m, card):
    def effect(state, ctx, changes):
        player = ctx["player"]
        count = len(state.players[1 - player].hand)
        shuffle_into_deck(state, player, list(state.players[player].hand), changes)
        engine.draw_cards(state, player, count, changes)
    return effect


@trainer(r"Draw " + N + r" cards\. Your opponent may draw a card\.")
def _cheerleaders_cheer(m, card):
    """The opponent's draw is optional on paper; taking it is never worse for
    them, and asking would cost a round trip for a decision nobody makes."""
    count = int(m.group(1))

    def effect(state, ctx, changes):
        engine.draw_cards(state, ctx["player"], count, changes)
        engine.draw_cards(state, 1 - ctx["player"], 1, changes)
    return effect


@trainer(r"Discard " + N + r" cards from your hand\. If you do, draw " + N
         + r" cards\.")
def _sophocles(m, card):
    cost, draw = int(m.group(1)), int(m.group(2))

    # The card itself is still in hand while legal_actions() is deciding, and
    # it is not one of the cards it asks you to discard - hence cost + 1.
    @playable(lambda state, player: len(state.players[player].hand) > cost)
    def effect(state, ctx, changes):
        player = ctx["player"]
        hand = tuple(state.players[player].hand)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardFromHand", options=hand,
            option_kind=CHOICE_CARD, minimum=cost, maximum=cost,
            zone=ZONE_HAND, detail={"card": card.name}))
        if choice is not None:
            return choice
        if len(picks) < cost:
            return None
        for cid in picks:
            engine.move_card(state, cid, ZONE_DISCARD, changes)
        engine.draw_cards(state, player, draw, changes)
    return effect


@trainer(r"Shuffle " + N + r" cards from your hand into your deck\. \(If you "
         r"can't shuffle " + N + r" cards into your deck, you can't play this "
         r"card\.\) Then, draw a card\.")
def _maintenance(m, card):
    cost = int(m.group(1))

    @playable(lambda state, player: len(state.players[player].hand) > cost)
    def effect(state, ctx, changes):
        player = ctx["player"]
        hand = tuple(state.players[player].hand)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="shuffleFromHand", options=hand,
            option_kind=CHOICE_CARD, minimum=cost, maximum=cost,
            zone=ZONE_HAND, detail={"card": card.name}))
        if choice is not None:
            return choice
        if len(picks) < cost:
            return None
        shuffle_into_deck(state, player, picks, changes)
        engine.draw_cards(state, player, 1, changes)
    return effect


@trainer(r"Discard a card from your hand\. If you do, look at the top " + N
         + r" cards of your deck and put 1 of them into your hand\. Shuffle "
         r"the other cards back into your deck\.")
def _mistys_determination(m, card):
    depth = int(m.group(1))

    @playable(lambda state, player: len(state.players[player].hand) > 1)
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        discard, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardFromHand", options=tuple(ps.hand),
            option_kind=CHOICE_CARD, zone=ZONE_HAND,
            detail={"card": card.name}))
        if choice is not None:
            return choice
        top = tuple(ps.deck[:depth])
        keep, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="lookAtTop", options=distinct(state, top),
            option_kind=CHOICE_CARD, zone=ZONE_DECK,
            detail={"card": card.name, "depth": depth}) if top else None)
        if choice is not None:
            return choice
        for cid in discard:
            engine.move_card(state, cid, ZONE_DISCARD, changes)
        to_hand(state, keep, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"Look at the top " + N + r" cards of your deck and put 1 of them "
         r"into your hand\. Discard the other card\.")
def _acro_bike(m, card):
    depth = int(m.group(1))

    @playable(lambda state, player: bool(state.players[player].deck))
    def effect(state, ctx, changes):
        player = ctx["player"]
        top = tuple(state.players[player].deck[:depth])
        keep, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="lookAtTop", options=top,
            option_kind=CHOICE_CARD, zone=ZONE_DECK,
            detail={"card": card.name, "depth": depth}) if top else None)
        if choice is not None:
            return choice
        to_hand(state, keep, changes)
        for cid in top:
            if cid not in keep:
                engine.move_card(state, cid, ZONE_DISCARD, changes)
    return effect


@trainer(r"Look at the top " + N + r" cards of your deck\. Choose any " + N
         + r" cards you find there and put them into your hand\. Discard the "
         r"other cards\.")
def _sages_training(m, card):
    depth, take = int(m.group(1)), int(m.group(2))

    @playable(lambda state, player: bool(state.players[player].deck))
    def effect(state, ctx, changes):
        player = ctx["player"]
        top = tuple(state.players[player].deck[:depth])
        keep, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="lookAtTop", options=top,
            option_kind=CHOICE_CARD, minimum=take, maximum=take,
            zone=ZONE_DECK, detail={"card": card.name, "depth": depth})
            if top else None)
        if choice is not None:
            return choice
        to_hand(state, keep, changes)
        for cid in top:
            if cid not in keep:
                engine.move_card(state, cid, ZONE_DISCARD, changes)
    return effect


@trainer(r"Reveal cards from the top of your deck until you reveal a Supporter "
         r"card\. Put it into your hand\. Shuffle the other cards back into "
         r"your deck\.")
def _random_receiver(m, card):
    @playable(lambda state, player: bool(state.players[player].deck))
    def effect(state, ctx, changes):
        player = ctx["player"]
        deck = state.players[player].deck
        found = next((cid for cid in deck if state.card(cid).is_supporter), None)
        if found is not None:
            to_hand(state, (found,), changes)
        engine.shuffle_deck(state, player, changes)
    return effect


# --------------------------------------------------------------------------
# searching the deck
# --------------------------------------------------------------------------
#
# All of these share a shape: a filter over the deck, a Choice from what it
# finds, then the cards move and the deck is shuffled. _search() is that
# shape, and the individual cards are the filter plus where the cards land.

def _search(card, match, destination, count=1, at_least=0, shuffle=True,
            prompt="searchDeck"):
    """Build a "search your deck for X" effect.

    `destination` is "hand" or "bench". `at_least` is the minimum number of
    picks, which is 0 for every real card - a search you are allowed to fail
    is the norm, because a deck that does not contain the card must not make
    the action illegal after the fact.
    """
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        found = distinct(state, [cid for cid in ps.deck
                                 if match(state, player, cid)])
        limit = count
        if destination == "bench":
            limit = min(limit, state.rules.bench_size - len(ps.bench))
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt=prompt, options=found,
            option_kind=CHOICE_CARD, minimum=at_least, maximum=limit,
            zone=ZONE_DECK, detail={"card": card.name}) if found and limit > 0
            else None)
        if choice is not None:
            return choice
        for cid in picks:
            if destination == "bench":
                engine.bench_card(state, player, cid, changes)
            else:
                to_hand(state, (cid,), changes)
        if shuffle:
            engine.shuffle_deck(state, player, changes)
    return effect


def _any_pokemon(state, player, cid):
    return state.card(cid).is_pokemon


def _basic_pokemon(state, player, cid):
    return state.card(cid).is_basic_pokemon


def _evolution(state, player, cid):
    return state.card(cid).is_evolution


def _basic_energy(state, player, cid):
    return is_basic_energy(state, cid)


def _any_trainer(state, player, cid):
    return state.card(cid).is_trainer


@trainer(r"Search your deck for a Basic " + POKEMON + r" and put it onto your "
         r"Bench\. Then, shuffle your deck\.")
def _nest_ball(m, card):
    effect = _search(card, _basic_pokemon, "bench")
    return playable(lambda s, p: len(s.players[p].bench) < s.rules.bench_size)(effect)


@trainer(r"Search your deck for up to " + N + r" Basic " + POKEMON
         + r", reveal them, and put them into your hand\. Shuffle your deck "
           r"afterward\.")
def _pokemon_fan_club(m, card):
    return _search(card, _basic_pokemon, "hand", count=int(m.group(1)))


@trainer(r"Search your deck for up to " + N + r" basic Energy cards, reveal "
         r"them, and put them into your hand\. Shuffle your deck afterward\.")
def _professors_letter(m, card):
    return _search(card, _basic_energy, "hand", count=int(m.group(1)))


@trainer(r"Search your deck for a basic Energy card, reveal it, and put it "
         r"into your hand\. Shuffle your deck afterward\.")
def _energy_search(m, card):
    return _search(card, _basic_energy, "hand")


@trainer(r"Search your deck for a Trainer card, reveal it, and put it into "
         r"your hand\. Shuffle your deck afterward\.")
def _skyla(m, card):
    return _search(card, _any_trainer, "hand")


@trainer(r"Search your deck for an Evolution card, reveal it, and put it into "
         r"your hand\. Shuffle your deck afterward\.")
def _elms_training_method(m, card):
    return _search(card, _evolution, "hand")


@trainer(r"Search your deck for a " + POKEMON + r" with " + N + r" HP or "
         r"less, reveal it, and put it into your hand\. Shuffle your deck "
         r"afterward\.")
def _level_ball(m, card):
    cap = int(m.group(1))
    return _search(card, lambda s, p, cid: (s.card(cid).is_pokemon
                                            and s.card(cid).max_hp <= cap),
                   "hand")


@trainer(r"Search your deck for a " + POKEMON + r" with a Retreat Cost of "
         + N + r" or more, reveal it, and put it into your hand\. Shuffle "
         r"your deck afterward\.")
def _heavy_ball(m, card):
    floor = int(m.group(1))
    return _search(card, lambda s, p, cid: (s.card(cid).is_pokemon
                                            and s.card(cid).retreat_cost >= floor),
                   "hand")


@trainer(r"Search your deck for a " + POKEMON + r" with the same name as 1 of "
         r"your " + POKEMON + r" in play, reveal it, and put it into your "
         r"hand\. Shuffle your deck afterward\.")
def _repeat_ball(m, card):
    def match(state, player, cid):
        names = {state.pokemon(s).name for s in state.players[player].in_play}
        return state.card(cid).is_pokemon and state.card(cid).name in names
    return _search(card, match, "hand")


@trainer(r"Flip a coin\. If heads, search your deck for a " + POKEMON
         + r", reveal it, and put it into your hand\. (?:Shuffle your deck "
           r"afterward|Then, shuffle your deck)\.")
def _poke_ball(m, card):
    inner = _search(card, _any_pokemon, "hand")

    def effect(state, ctx, changes):
        # The flip is made once and remembered: the effect is re-entered after
        # the search choice and must not flip a second time.
        if "heads" not in ctx["data"]:
            ctx["data"]["heads"] = engine.flip_coin(state, changes, card.name,
                                                    ctx["player"])
        if not ctx["data"]["heads"]:
            return None
        return inner(state, ctx, changes)
    return effect


@trainer(r"Flip " + N + r" coins\. For each heads, search your deck for a "
         r"Basic " + POKEMON + r", reveal it, and put it into your hand\. If "
         r"you do, shuffle your deck afterward\.")
@trainer(r"Flip " + N + r" coins\. For each heads, search your deck for an "
         r"Evolution " + POKEMON + r", reveal it, and put it into your hand\. "
         r"Then, shuffle your deck\.")
def _multi_ball(m, card):
    coins = int(m.group(1))
    want = _basic_pokemon if "Basic" in m.group(0) else _evolution

    def effect(state, ctx, changes):
        player = ctx["player"]
        if "heads" not in ctx["data"]:
            ctx["data"]["heads"] = flips(state, changes, coins, card.name, player)
        heads = ctx["data"]["heads"]
        if not heads:
            return None
        return _search(card, want, "hand", count=heads)(state, ctx, changes)
    return effect


@trainer(r"Discard " + N + r" cards from your hand\. (?:\(If you can't "
         r"discard " + N + r" cards, you can't play this card\.\) |If you do, )"
         r"[Ss]earch your deck for a " + POKEMON + r", reveal it, and put it "
         r"into your hand\. (?:Shuffle your deck afterward|Then, shuffle your "
         r"deck)\.")
def _ultra_ball(m, card):
    cost = int(m.group(1))

    @playable(lambda state, player: len(state.players[player].hand) > cost)
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        # The Ultra Ball itself is already out of hand (see _do_play_trainer),
        # so it can never be one of the two cards it asks you to discard -
        # but it IS still there when the playable guard above counts, which is
        # why that guard wants cost + 1.
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardFromHand", options=tuple(ps.hand),
            option_kind=CHOICE_CARD, minimum=cost, maximum=cost,
            zone=ZONE_HAND, detail={"card": card.name}))
        if choice is not None:
            return choice
        if len(picks) < cost:
            return None
        found = distinct(state, [cid for cid in ps.deck
                                 if state.card(cid).is_pokemon])
        take, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="searchDeck", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=1, zone=ZONE_DECK,
            detail={"card": card.name}) if found else None)
        if choice is not None:
            return choice
        for cid in picks:
            engine.move_card(state, cid, ZONE_DISCARD, changes)
        to_hand(state, take, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"Look at the top " + N + r" cards of your deck\. You may reveal a "
         + POKEMON + r" you find there and put it into your hand\. Shuffle "
         r"the other cards back into your deck\.")
def _great_ball(m, card):
    depth = int(m.group(1))

    @playable(lambda state, player: bool(state.players[player].deck))
    def effect(state, ctx, changes):
        player = ctx["player"]
        top = state.players[player].deck[:depth]
        found = distinct(state, [cid for cid in top
                                 if state.card(cid).is_pokemon])
        take, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="lookAtTop", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=1, zone=ZONE_DECK,
            detail={"card": card.name, "depth": depth}) if found else None)
        if choice is not None:
            return choice
        to_hand(state, take, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"Look at the top " + N + r" cards of your deck\. You may reveal a "
         r"Trainer card you find there \(except for Trainers' Mail\) and put "
         r"it into your hand\. Shuffle the other cards back into your deck\.")
def _trainers_mail(m, card):
    depth = int(m.group(1))

    @playable(lambda state, player: bool(state.players[player].deck))
    def effect(state, ctx, changes):
        player = ctx["player"]
        top = state.players[player].deck[:depth]
        found = distinct(state, [cid for cid in top
                                 if state.card(cid).is_trainer
                                 and state.card(cid).name != card.name])
        take, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="lookAtTop", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=1, zone=ZONE_DECK,
            detail={"card": card.name, "depth": depth}) if found else None)
        if choice is not None:
            return choice
        to_hand(state, take, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"Look at the top " + N + r" cards of your deck\. Choose as many "
         r"Energy cards as you like, reveal them, and put them into your "
         r"hand\. Shuffle the other cards back into your deck\.")
def _interviewers_questions(m, card):
    depth = int(m.group(1))

    @playable(lambda state, player: bool(state.players[player].deck))
    def effect(state, ctx, changes):
        player = ctx["player"]
        top = state.players[player].deck[:depth]
        found = tuple(cid for cid in top if state.card(cid).is_energy)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="lookAtTop", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=len(found),
            zone=ZONE_DECK, detail={"card": card.name, "depth": depth})
            if found else None)
        if choice is not None:
            return choice
        to_hand(state, picks, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"Reveal a " + POKEMON + r" in your hand and put it on top of your "
         r"deck\. If you do, search your deck for a " + POKEMON + r", reveal "
         r"it, and put it into your hand\. Shuffle your deck afterward\.")
@trainer(r"Choose 1 " + POKEMON + r" in your hand, reveal it, and put it on "
         r"top of your deck\. If you do, search your deck for a " + POKEMON
         + r", reveal it, and put it into your hand\. Shuffle your deck "
           r"afterward\.")
def _pokemon_communication(m, card):
    def has_pokemon(state, player):
        return any(state.card(c).is_pokemon for c in state.players[player].hand)

    @playable(has_pokemon)
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        give = tuple(c for c in ps.hand if state.card(c).is_pokemon)
        offered, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="fromHand", options=give,
            option_kind=CHOICE_CARD, zone=ZONE_HAND,
            detail={"card": card.name}) if give else None)
        if choice is not None:
            return choice
        if not offered:
            return None
        # The deck is read before the offered card joins it, so the search
        # cannot hand back the very card just placed on top.
        found = distinct(state, [c for c in ps.deck if state.card(c).is_pokemon])
        take, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="searchDeck", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=1, zone=ZONE_DECK,
            detail={"card": card.name}) if found else None)
        if choice is not None:
            return choice
        engine.move_card(state, offered[0], ZONE_DECK, changes)
        to_hand(state, take, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"Search your deck for a card that evolves from 1 of your " + POKEMON
         + r" and put it onto that " + POKEMON + r"\. \(This counts as "
           r"evolving that " + POKEMON + r"\.\) Shuffle your deck afterward\. "
           r"You can't use this card during your first turn or on a " + POKEMON
         + r" that was put into play this turn\.")
def _evosoda(m, card):
    def ready(state, player):
        ps = state.players[player]
        if ps.turns_taken <= 1:
            return False
        return any(_evolvable(state, player, slot) for slot in ps.in_play)

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        targets = [s for s in ps.in_play if _evolvable(state, player, s)]
        chosen, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="evolveTarget", options=slot_ids(targets),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if targets else None)
        if choice is not None:
            return choice
        if not chosen:
            return None
        slot = slot_of(state, chosen[0])
        if slot is None:
            return None
        name = state.pokemon(slot).name
        found = distinct(state, [c for c in ps.deck
                                 if state.card(c).evolves_from == name])
        take, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="searchDeck", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=1, zone=ZONE_DECK,
            detail={"card": card.name}) if found else None)
        if choice is not None:
            return choice
        for cid in take:
            engine.move_card(state, cid, ZONE_HAND, changes)
            engine.evolve_slot(state, player, cid, slot, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


def _evolvable(state, player, slot):
    """Is there anything in the deck that evolves from what is on this slot?"""
    if slot.played_on_turn >= state.players[player].turns_taken:
        return False
    name = state.pokemon(slot).name
    return any(state.card(c).evolves_from == name
               for c in state.players[player].deck)


@trainer(r"Choose 1 of your Basic " + POKEMON + r" in play\. If you have a "
         r"Stage 2 card in your hand that evolves from that " + POKEMON
         + r", put that card on(?:to)? the Basic " + POKEMON
         + r"(?: to evolve it)?\.?\s*(?:\(This counts as evolving that "
         + POKEMON + r"\.\))? You can't use this card during your first turn "
           r"or on a Basic " + POKEMON + r" that was put into play this turn\.")
def _rare_candy(m, card):
    def candidates(state, player):
        ps = state.players[player]
        if ps.turns_taken <= 1:
            return []
        out = []
        for slot in ps.in_play:
            if slot.played_on_turn >= ps.turns_taken:
                continue
            top = state.pokemon(slot)
            if not top.is_basic_pokemon:
                continue
            if any(_stage2_from(state, c, top.name) for c in ps.hand):
                out.append(slot)
        return out

    @playable(lambda state, player: bool(candidates(state, player)))
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        targets = candidates(state, player)
        chosen, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="evolveTarget", options=slot_ids(targets),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if targets else None)
        if choice is not None:
            return choice
        if not chosen:
            return None
        slot = slot_of(state, chosen[0])
        if slot is None:
            return None
        name = state.pokemon(slot).name
        options = distinct(state, [c for c in ps.hand
                                   if _stage2_from(state, c, name)])
        take, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="fromHand", options=options,
            option_kind=CHOICE_CARD, zone=ZONE_HAND,
            detail={"card": card.name}) if options else None)
        if choice is not None:
            return choice
        for cid in take:
            engine.evolve_slot(state, player, cid, slot, changes)
    return effect


def _stage2_from(state, cid, basic_name):
    """A Stage 2 whose whole line starts at this Basic.

    Rare Candy skips the Stage 1, so the match is on the Stage 1's own
    evolves_from - which means the card database has to contain that Stage 1.
    It always does: a Stage 2 that exists has its line printed in some set.
    """
    card = state.card(cid)
    if not card.is_pokemon or card.stage != "Stage2" or not card.evolves_from:
        return False
    return any(mid.evolves_from == basic_name
               for mid in state.db.by_name(card.evolves_from))


# --------------------------------------------------------------------------
# the discard pile
# --------------------------------------------------------------------------

def _from_discard(card, match, count, destination=ZONE_HAND, exact=False,
                  shuffle=False, prompt="fromDiscard"):
    def ready(state, player):
        return any(match(state, player, cid)
                   for cid in state.players[player].discard)

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        found = tuple(cid for cid in state.players[player].discard
                      if match(state, player, cid))
        limit = min(count, len(found))
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt=prompt, options=found,
            option_kind=CHOICE_CARD, minimum=limit if exact else 0,
            maximum=limit, zone=ZONE_DISCARD, detail={"card": card.name})
            if found else None)
        if choice is not None:
            return choice
        if destination == ZONE_DECK:
            shuffle_into_deck(state, player, picks, changes)
        else:
            to_hand(state, picks, changes)
        if shuffle and destination != ZONE_DECK:
            engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"Put " + N + r" basic Energy cards from your discard pile into your "
         r"hand\.")
def _energy_retrieval(m, card):
    return _from_discard(card, lambda s, p, cid: is_basic_energy(s, cid),
                         int(m.group(1)), exact=True)


@trainer(r"Search your discard pile for " + N + r" basic Energy cards, reveal "
         r"them, and put them into your hand\.")
def _fisherman(m, card):
    return _from_discard(card, lambda s, p, cid: is_basic_energy(s, cid),
                         int(m.group(1)), exact=True)


@trainer(r"Put a Supporter card from your discard pile into your hand\.")
def _vs_seeker(m, card):
    return _from_discard(card, lambda s, p, cid: s.card(cid).is_supporter, 1,
                         exact=True)


@trainer(r"Shuffle " + N + r" basic Energy cards from your discard pile into "
         r"your deck\.")
def _energy_recycler(m, card):
    return _from_discard(card, lambda s, p, cid: is_basic_energy(s, cid),
                         int(m.group(1)), destination=ZONE_DECK, exact=True)


@trainer(r"Shuffle " + N + r" Special Energy cards from your discard pile into "
         r"your deck\.")
def _special_charge(m, card):
    return _from_discard(card, lambda s, p, cid: is_special_energy(s, cid),
                         int(m.group(1)), destination=ZONE_DECK, exact=True)


@trainer(r"Shuffle " + N + r" in any combination of " + POKEMON + r" and basic "
         r"Energy cards from your discard pile (?:back )?into your deck\.")
def _super_rod(m, card):
    def match(state, player, cid):
        return state.card(cid).is_pokemon or is_basic_energy(state, cid)
    return _from_discard(card, match, int(m.group(1)), destination=ZONE_DECK,
                         exact=True)


@trainer(r"Put a Basic " + POKEMON + r" from your discard pile onto your "
         r"Bench\.")
def _revive(m, card):
    def ready(state, player):
        return (len(state.players[player].bench) < state.rules.bench_size
                and any(state.card(c).is_basic_pokemon
                        for c in state.players[player].discard))

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        found = distinct(state, [c for c in state.players[player].discard
                                 if state.card(c).is_basic_pokemon])
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="fromDiscard", options=found,
            option_kind=CHOICE_CARD, zone=ZONE_DISCARD,
            detail={"card": card.name}) if found else None)
        if choice is not None:
            return choice
        for cid in picks:
            engine.bench_card(state, player, cid, changes)
    return effect


# --------------------------------------------------------------------------
# healing, conditions, switching
# --------------------------------------------------------------------------

@trainer(r"Heal " + N + r" damage from 1 of your " + POKEMON + r"\.")
def _potion(m, card):
    amount = int(m.group(1))

    @playable(lambda state, player: bool(damaged(state, player)))
    def effect(state, ctx, changes):
        player = ctx["player"]
        targets = damaged(state, player)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="healTarget", options=slot_ids(targets),
            option_kind=CHOICE_SLOT,
            detail={"card": card.name, "amount": amount}) if targets else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            slot = slot_of(state, slot_id)
            if slot is not None:
                engine.heal(state, slot, amount, changes)
    return effect


@trainer(r"Heal " + N + r" damage from 1 of your " + POKEMON + r"\. If you do, "
         r"discard an Energy attached to that " + POKEMON + r"\.")
def _super_potion(m, card):
    amount = int(m.group(1))

    @playable(lambda state, player: any(s.damage and s.energy
                                        for s in own_slots(state, player)))
    def effect(state, ctx, changes):
        player = ctx["player"]
        targets = [s for s in own_slots(state, player) if s.damage and s.energy]
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="healTarget", options=slot_ids(targets),
            option_kind=CHOICE_SLOT,
            detail={"card": card.name, "amount": amount}) if targets else None)
        if choice is not None:
            return choice
        if not picks:
            return None
        slot = slot_of(state, picks[0])
        if slot is None:
            return None
        energy, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="discardEnergy", options=tuple(slot.energy),
            option_kind=CHOICE_CARD, detail={"card": card.name})
            if slot.energy else None)
        if choice is not None:
            return choice
        engine.heal(state, slot, amount, changes)
        engine.discard_attached(state, slot, energy, changes)
    return effect


@trainer(r"Heal all damage from 1 of your " + POKEMON + r"\. (?:Then, discard "
         r"all Energy attached to|If you do, discard all Energy (?:attached to|"
         r"from)) that " + POKEMON + r"\.")
def _max_potion(m, card):
    @playable(lambda state, player: bool(damaged(state, player)))
    def effect(state, ctx, changes):
        player = ctx["player"]
        targets = damaged(state, player)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="healTarget", options=slot_ids(targets),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if targets else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            slot = slot_of(state, slot_id)
            if slot is None:
                continue
            engine.heal(state, slot, slot.damage, changes)
            engine.discard_attached(state, slot, list(slot.energy), changes)
    return effect


@trainer(r"Remove all Special Conditions from your Active " + POKEMON + r"\.")
def _full_heal(m, card):
    @playable(lambda state, player: bool(state.players[player].active
                                         and state.players[player].active.conditions))
    def effect(state, ctx, changes):
        active = state.players[ctx["player"]].active
        if active is not None:
            engine.clear_conditions(state, active, changes, card.name)
    return effect


@trainer(r"Switch (?:your|1 of your) Active " + POKEMON + r" with 1 of your "
         r"Benched " + POKEMON + r"\.")
def _switch(m, card):
    @playable(lambda state, player: bool(state.players[player].bench
                                         and state.players[player].active))
    def effect(state, ctx, changes):
        player = ctx["player"]
        bench = state.players[player].bench
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="switchTo", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if bench else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            engine.switch_active(state, player, slot_id, changes)
    return effect


@trainer(r"Each player switches their Active " + POKEMON + r" with 1 of their "
         r"Benched " + POKEMON + r"\.? ?\(?Your opponent switches first\.?\)? ?"
         r"\(?If a player does not have a Benched " + POKEMON + r", (?:they|"
         r"that player) doesn't switch " + POKEMON + r"\.\)?")
def _escape_rope(m, card):
    """Both players switch, opponent first.

    Every question is asked before any switch happens, which is what lets two
    Choices live in one effect: the second player's bench cannot have changed
    between being offered and being used, because nothing has moved yet.
    """
    def ready(state, player):
        return any(state.players[p].bench and state.players[p].active
                   for p in (0, 1))

    @playable(ready)
    def effect(state, ctx, changes):
        me = ctx["player"]
        asks = [p for p in (1 - me, me)
                if state.players[p].bench and state.players[p].active]
        picks = []
        for index, who in enumerate(asks):
            answer, choice = step(ctx, index, lambda who=who: Choice(
                player=who, prompt="switchTo",
                options=slot_ids(state.players[who].bench),
                option_kind=CHOICE_SLOT, detail={"card": card.name}))
            if choice is not None:
                return choice
            picks.append((who, answer))
        for who, answer in picks:
            for slot_id in answer:
                engine.switch_active(state, who, slot_id, changes)
    return effect


@trainer(r"Switch 1 of your opponent's Benched " + POKEMON + r" with their "
         r"Active " + POKEMON + r"\.")
def _lysandre(m, card):
    @playable(lambda state, player: bool(state.players[1 - player].bench))
    def effect(state, ctx, changes):
        player = ctx["player"]
        them = 1 - player
        bench = state.players[them].bench
        # The card says "switch 1 of your opponent's", so the choice is the
        # player's own even though the Pokemon moved belongs to the opponent.
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="gustTarget", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if bench else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            engine.switch_active(state, them, slot_id, changes)
    return effect


@trainer(r"Flip a coin\. If heads, switch 1 of your opponent's Benched "
         + POKEMON + r" with (?:their|his or her) Active " + POKEMON + r"\.")
def _pokemon_catcher(m, card):
    @playable(lambda state, player: bool(state.players[1 - player].bench))
    def effect(state, ctx, changes):
        player = ctx["player"]
        them = 1 - player
        if "heads" not in ctx["data"]:
            ctx["data"]["heads"] = engine.flip_coin(state, changes, card.name,
                                                    player)
        if not ctx["data"]["heads"]:
            return None
        bench = state.players[them].bench
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="gustTarget", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if bench else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            engine.switch_active(state, them, slot_id, changes)
    return effect


@trainer(r"Switch 1 of your opponent's Benched " + POKEMON + r" with their "
         r"Active " + POKEMON + r"\. If you do, switch your Active " + POKEMON
         + r" with 1 of your Benched " + POKEMON + r"\.")
def _guzma(m, card):
    @playable(lambda state, player: bool(state.players[1 - player].bench))
    def effect(state, ctx, changes):
        player = ctx["player"]
        them = 1 - player
        theirs, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="gustTarget",
            options=slot_ids(state.players[them].bench),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if state.players[them].bench else None)
        if choice is not None:
            return choice
        if not theirs:
            return None
        mine, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="switchTo",
            options=slot_ids(state.players[player].bench),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if state.players[player].bench else None)
        if choice is not None:
            return choice
        for slot_id in theirs:
            engine.switch_active(state, them, slot_id, changes)
        for slot_id in mine:
            engine.switch_active(state, player, slot_id, changes)
    return effect


@trainer(r"Flip a coin\. If heads, (?:put|return) 1 of your " + POKEMON
         + r" and all cards attached to it (?:into|to) your hand\.")
def _super_scoop_up(m, card):
    @playable(lambda state, player: bool(own_slots(state, player)))
    def effect(state, ctx, changes):
        player = ctx["player"]
        if "heads" not in ctx["data"]:
            ctx["data"]["heads"] = engine.flip_coin(state, changes, card.name,
                                                    player)
        if not ctx["data"]["heads"]:
            return None
        targets = own_slots(state, player)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="scoopTarget", options=slot_ids(targets),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if targets else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            _scoop(state, player, slot_id, changes)
    return effect


def _scoop(state, player, slot_id, changes):
    """Whole Pokemon back to hand: stack, Energy and Tools together.

    Only legal on a Pokemon that leaves a board behind - taking up your only
    Active loses you the game on the spot, and no printed card intends that.
    """
    ps = state.players[player]
    slot = slot_of(state, slot_id)
    if slot is None or len(ps.in_play) <= 1:
        return False
    from_zone = engine.ZONE_ACTIVE if ps.active is slot else engine.ZONE_BENCH
    for cid in slot.cards:
        ps.hand.append(cid)
        changes.append(engine.Change(engine.CHANGE_MOVE, player=player,
                                     card=cid, slot=slot.slot_id,
                                     from_zone=from_zone, to_zone=ZONE_HAND))
    if ps.active is slot:
        ps.active = None
        if player not in state.pending_promotions:
            state.pending_promotions.append(player)
    else:
        ps.bench.remove(slot)
    return True


# --------------------------------------------------------------------------
# attacking the opponent's board
# --------------------------------------------------------------------------

@trainer(r"Discard an Energy attached to your opponent's Active " + POKEMON
         + r"\.")
def _team_flare_grunt(m, card):
    @playable(lambda state, player: bool(state.players[1 - player].active
                                         and state.players[1 - player].active.energy))
    def effect(state, ctx, changes):
        player = ctx["player"]
        active = state.players[1 - player].active
        if active is None or not active.energy:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardEnergy", options=tuple(active.energy),
            option_kind=CHOICE_CARD, detail={"card": card.name}))
        if choice is not None:
            return choice
        engine.discard_attached(state, active, picks, changes)
    return effect


def _hammer(card, match, flip):
    def ready(state, player):
        return any(any(match(state, cid) for cid in slot.energy)
                   for slot in own_slots(state, 1 - player))

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        if flip and "heads" not in ctx["data"]:
            ctx["data"]["heads"] = engine.flip_coin(state, changes, card.name,
                                                    player)
        if flip and not ctx["data"]["heads"]:
            return None
        options = tuple(cid for slot in own_slots(state, 1 - player)
                        for cid in slot.energy if match(state, cid))
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardEnergy", options=options,
            option_kind=CHOICE_CARD, detail={"card": card.name})
            if options else None)
        if choice is not None:
            return choice
        for cid in picks:
            slot = slot_holding(state, 1 - player, cid)
            if slot is not None:
                engine.discard_attached(state, slot, (cid,), changes)
    return effect


@trainer(r"Flip a coin\. If heads, discard an Energy (?:attached to|from) 1 of "
         r"your opponent's " + POKEMON + r"\.")
def _crushing_hammer(m, card):
    return _hammer(card, lambda s, cid: True, flip=True)


@trainer(r"Discard a Special Energy (?:attached to|from) 1 of your opponent's "
         + POKEMON + r"\.")
def _enhanced_hammer(m, card):
    return _hammer(card, is_special_energy, flip=False)


@trainer(r"Your opponent reveals their hand and shuffles all Item cards found "
         r"there into their deck\. Then, draw a number of cards equal to the "
         r"number of Item cards your opponent shuffled into their deck\.")
def _ghetsis(m, card):
    def effect(state, ctx, changes):
        player = ctx["player"]
        them = 1 - player
        items = [c for c in state.players[them].hand if state.card(c).is_item]
        if items:
            shuffle_into_deck(state, them, items, changes)
        engine.draw_cards(state, player, len(items), changes)
    return effect


@trainer(r"Until the end of your opponent's next turn, each " + POKEMON
         + r" in play, in each player's hand, and in each player's discard "
           r"pile has no Abilities\. \(This includes cards that come into play "
           r"on that turn\.\)")
def _hex_maniac(m, card):
    def effect(state, ctx, changes):
        engine.add_modifier(state, changes, Modifier(
            kind=MOD_NO_ABILITIES, until_turn=state.turn_number + 1,
            player=None, source=ctx.get("source"),
            detail={"card": card.name}))
    return effect


@trainer(r"Move a basic Energy (?:card attached to|from) 1 of your " + POKEMON
         + r" to another of your " + POKEMON + r"\.")
def _energy_switch(m, card):
    def ready(state, player):
        slots = own_slots(state, player)
        return (len(slots) > 1
                and any(any(is_basic_energy(state, c) for c in s.energy)
                        for s in slots))

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        slots = own_slots(state, player)
        movable = tuple(c for s in slots for c in s.energy
                        if is_basic_energy(state, c))
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="moveEnergy", options=movable,
            option_kind=CHOICE_CARD, detail={"card": card.name})
            if movable else None)
        if choice is not None:
            return choice
        if not picks:
            return None
        source = slot_holding(state, player, picks[0])
        if source is None:
            return None
        targets = [s for s in slots if s is not source]
        dest, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="energyTarget", options=slot_ids(targets),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if targets else None)
        if choice is not None:
            return choice
        if not dest:
            return None
        target = slot_of(state, dest[0])
        if target is None:
            return None
        source.energy.remove(picks[0])
        target.energy.append(picks[0])
        changes.append(engine.Change(engine.CHANGE_ATTACH, player=player,
                                     card=picks[0], slot=target.slot_id,
                                     detail={"movedFrom": source.slot_id}))
    return effect


@trainer(r"Look at the top " + N + r" cards of your deck and attach a basic "
         r"Energy card you find there to a Basic " + POKEMON + r" on your "
         r"Bench\. Shuffle the other cards back into your deck\.")
def _max_elixir(m, card):
    depth = int(m.group(1))

    @playable(lambda state, player: bool(state.players[player].deck))
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        top = ps.deck[:depth]
        energy = distinct(state, [c for c in top if is_basic_energy(state, c)])
        bench = [s for s in ps.bench if state.pokemon(s).is_basic_pokemon]
        pick, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="lookAtTop", options=energy,
            option_kind=CHOICE_CARD, minimum=0, maximum=1, zone=ZONE_DECK,
            detail={"card": card.name, "depth": depth})
            if energy and bench else None)
        if choice is not None:
            return choice
        if not pick:
            engine.shuffle_deck(state, player, changes)
            return None
        dest, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="energyTarget", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"card": card.name}))
        if choice is not None:
            return choice
        target = slot_of(state, dest[0]) if dest else None
        if target is not None:
            ps.deck.remove(pick[0])
            target.energy.append(pick[0])
            changes.append(engine.Change(
                engine.CHANGE_MOVE, player=player, card=pick[0],
                slot=target.slot_id, from_zone=ZONE_DECK,
                to_zone=engine.ZONE_BENCH))
            changes.append(engine.Change(engine.CHANGE_ATTACH, player=player,
                                         card=pick[0], slot=target.slot_id))
        engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"During this turn, your " + POKEMON + r"'s attacks do " + N
         + r" more damage to the Active " + POKEMON + r" \(before applying "
           r"Weakness and Resistance\)\.")
def _pluspower(m, card):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        engine.add_modifier(state, changes, Modifier(
            kind=MOD_DAMAGE_DEALT, until_turn=state.turn_number,
            player=ctx["player"], amount=amount, source=ctx.get("source"),
            detail={"card": card.name}))
    return effect


# --------------------------------------------------------------------------
# Pokemon Tools - continuous, so they live in static_effects
# --------------------------------------------------------------------------
#
# A Tool still needs an entry in trainer_effects or it cannot be played at
# all; _tool() supplies an on-attach effect that does nothing and pairs it
# with the continuous one. That pairing is why the builder returns a tuple.

def _tool(static_hook):
    def nothing(state, ctx, changes):
        return None
    return nothing, static_hook


@static(r"The " + POKEMON + r" this card is attached to has no Retreat Cost\.")
def _float_stone(m, card):
    def hook(query, state, ctx, value):
        return 0 if query == STATIC_RETREAT_COST else value
    return _tool(hook)


@static(r"The " + POKEMON + r" this card is attached to has no Weakness\.")
def _weakness_policy(m, card):
    def hook(query, state, ctx, value):
        return 1 if query == STATIC_NO_WEAKNESS else value
    return _tool(hook)


@static(r"If the " + POKEMON + r" this card is attached to is a Basic "
        + POKEMON + r", any damage done to this " + POKEMON + r" by attacks is "
        r"reduced by " + N + r" \(after applying Weakness and Resistance\)\.")
def _eviolite(m, card):
    amount = int(m.group(1))

    def hook(query, state, ctx, value):
        if query != STATIC_DAMAGE_TAKEN:
            return value
        slot = ctx.get("slot")
        if slot is None or not state.pokemon(slot).is_basic_pokemon:
            return value
        return value + amount
    return _tool(hook)


@trainer(r"Heal " + N + r" damage from 1 of your " + POKEMON + r" that has "
         r"any \{([A-Z])\} Energy attached to it\.")
def _colour_heal(m, card):
    """Fairy Drop. The colour requirement is the whole card."""
    amount, code = int(m.group(1)), m.group(2)
    if SYMBOLS.get(code) is None:
        return None

    def targets(state, player):
        return [s for s in damaged(state, player)
                if _count_symbol(state, s, code)]

    @playable(lambda state, player: bool(targets(state, player)))
    def effect(state, ctx, changes):
        player = ctx["player"]
        found = targets(state, player)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="healTarget", options=slot_ids(found),
            option_kind=CHOICE_SLOT,
            detail={"card": card.name, "amount": amount}) if found else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            slot = slot_of(state, slot_id)
            if slot is not None:
                engine.heal(state, slot, amount, changes)
    return effect


@trainer(r"Switch your Active " + POKEMON + r" with 1 of your Benched "
         + POKEMON + r"\. If you do, heal " + N + r" damage from the "
         + POKEMON + r" you moved to your Bench\.")
def _switch_and_heal(m, card):
    """Olympia. The heal lands on the Pokemon that just LEFT the Active."""
    amount = int(m.group(1))

    @playable(lambda state, player: bool(state.players[player].bench
                                         and state.players[player].active))
    def effect(state, ctx, changes):
        player = ctx["player"]
        bench = state.players[player].bench
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="switchTo", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if bench else None)
        if choice is not None:
            return choice
        if not picks:
            return None
        # Remember which slot is retiring before the switch, because after it
        # the "old Active" is just another benched Pokemon.
        retiring = state.players[player].active
        retiring_id = retiring.slot_id if retiring is not None else None
        engine.switch_active(state, player, picks[0], changes)
        moved = slot_of(state, retiring_id) if retiring_id is not None else None
        if moved is not None:
            engine.heal(state, moved, amount, changes)
    return effect


@trainer(r"Draw " + N + r" cards\. During this turn, your " + POKEMON
         + r"'s attacks do " + N + r" more damage to your opponent's Active "
         + POKEMON + r" \(before applying Weakness and Resistance\)\.")
def _draw_and_pluspower(m, card):
    """Professor Kukui. Two things this module already does, in one card."""
    count, amount = int(m.group(1)), int(m.group(2))

    def effect(state, ctx, changes):
        player = ctx["player"]
        engine.draw_cards(state, player, count, changes)
        engine.add_modifier(state, changes, Modifier(
            kind=MOD_DAMAGE_DEALT, until_turn=state.turn_number,
            player=player, amount=amount, source=ctx.get("source"),
            detail={"card": card.name}))
    return effect


@trainer(r"Each player shuffles their hand into their deck and flips a coin\. "
         r"If heads, that player draws " + N + r" cards\. If tails, "
         r"(?:they|that player) draws? " + N + r" cards\.")
def _shuffle_and_flip_draw(m, card):
    """Ilima. Each player flips their OWN coin, starting with the caster."""
    heads, tails = int(m.group(1)), int(m.group(2))

    def effect(state, ctx, changes):
        player = ctx["player"]
        for p in (player, 1 - player):
            shuffle_into_deck(state, p, list(state.players[p].hand), changes)
            won = flips(state, changes, 1, card.name, p)
            engine.draw_cards(state, p, heads if won else tails, changes)
    return effect


@trainer(r"Discard " + N + r" cards from your hand\. If you do, discard an "
         r"Energy from 1 of your opponent's " + POKEMON + r"\.")
def _discard_then_hammer(m, card):
    """Plumeria. The cost is paid first, and only then does the Energy go."""
    cost = int(m.group(1))

    def ready(state, player):
        return (len(state.players[player].hand) > cost
                and any(slot.energy for slot in own_slots(state, 1 - player)))

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardFromHand", options=tuple(ps.hand),
            option_kind=CHOICE_CARD, minimum=cost, maximum=cost,
            zone=ZONE_HAND, detail={"card": card.name}))
        if choice is not None:
            return choice
        if len(picks) < cost:
            return None
        options = tuple(cid for slot in own_slots(state, 1 - player)
                        for cid in slot.energy)
        take, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="discardEnergy", options=options,
            option_kind=CHOICE_CARD, detail={"card": card.name})
            if options else None)
        if choice is not None:
            return choice
        for cid in picks:
            engine.move_card(state, cid, ZONE_DISCARD, changes)
        for cid in take:
            slot = slot_holding(state, 1 - player, cid)
            if slot is not None:
                engine.discard_attached(state, slot, (cid,), changes)
    return effect


@trainer(r"Flip a coin\. If heads, draw " + N + r" cards\. If tails, "
         r"(?:they |you )?draw " + N + r" cards\.")
def _flip_draw_either_way(m, card):
    """Emcee's Chatter. Both branches draw, so this never does nothing."""
    heads, tails = int(m.group(1)), int(m.group(2))

    def effect(state, ctx, changes):
        player = ctx["player"]
        won = flips(state, changes, 1, card.name, player)
        engine.draw_cards(state, player, heads if won else tails, changes)
    return effect


@trainer(r"Draw a card for each of your opponent's Benched Basic " + POKEMON
         + r"\.")
def _draw_per_opponent_basic(m, card):
    """Lass's Special. Counts BASICS on the bench, not the whole bench."""
    def count(state, player):
        return sum(1 for s in state.players[1 - player].bench
                   if state.pokemon(s).is_basic_pokemon)

    @playable(lambda state, player: count(state, player) > 0)
    def effect(state, ctx, changes):
        player = ctx["player"]
        engine.draw_cards(state, player, count(state, player), changes)
    return effect


@trainer(r"Shuffle your hand into your deck\. Then, draw a number of cards "
         r"equal to the number of Benched " + POKEMON + r" \(both yours and "
         r"your opponent's\)\.")
def _colress(m, card):
    def effect(state, ctx, changes):
        player = ctx["player"]
        count = len(state.players[0].bench) + len(state.players[1].bench)
        shuffle_into_deck(state, player, list(state.players[player].hand),
                          changes)
        engine.draw_cards(state, player, count, changes)
    return effect


@trainer(r"Each player counts the cards in their hand, shuffles those cards "
         r"into their deck, then draws that many cards\.")
def _wicke(m, card):
    """Wicke. Both hands are counted before either is shuffled away.

    Order is easy to get wrong here. Shuffling the first player's hand in
    before counting the second changes nothing today, but counting both up
    front is what the card says and costs nothing to do.
    """
    def effect(state, ctx, changes):
        player = ctx["player"]
        counts = {p: len(state.players[p].hand) for p in (0, 1)}
        for p in (player, 1 - player):
            shuffle_into_deck(state, p, list(state.players[p].hand), changes)
            engine.draw_cards(state, p, counts[p], changes)
    return effect


@trainer(r"Search your discard pile for " + N + r" basic Energy cards, reveal "
         r"them, and shuffle them into your deck\.")
def _energy_returner(m, card):
    count = int(m.group(1))

    @playable(lambda state, player: any(
        is_basic_energy(state, c) for c in state.players[player].discard))
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        options = tuple(c for c in ps.discard if is_basic_energy(state, c))
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="searchDiscard", options=options,
            option_kind=CHOICE_CARD, minimum=0, maximum=count,
            zone=ZONE_DISCARD, detail={"card": card.name}) if options else None)
        if choice is not None:
            return choice
        shuffle_into_deck(state, player, list(picks), changes)
    return effect


@trainer(r"Discard " + N + r" cards from your hand\. \(If you can't discard "
         + N + r" cards, you can't play this card\.\) Search your deck for a "
         r"card and put it into your hand\. Shuffle your deck afterward\.")
def _computer_search(m, card):
    """Computer Search. Ultra Ball's shape, except that it finds ANY card."""
    cost = int(m.group(1))

    @playable(lambda state, player: len(state.players[player].hand) > cost)
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardFromHand", options=tuple(ps.hand),
            option_kind=CHOICE_CARD, minimum=cost, maximum=cost,
            zone=ZONE_HAND, detail={"card": card.name}))
        if choice is not None:
            return choice
        if len(picks) < cost:
            return None
        found = distinct(state, list(ps.deck))
        take, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="searchDeck", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=1, zone=ZONE_DECK,
            detail={"card": card.name}) if found else None)
        if choice is not None:
            return choice
        for cid in picks:
            engine.move_card(state, cid, ZONE_DISCARD, changes)
        to_hand(state, take, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


@trainer(r"During this turn, your " + POKEMON + r"'s attacks do " + N
         + r" more damage to the Active " + POKEMON + r" for each Prize card "
           r"your opponent has taken \(before applying Weakness and "
           r"Resistance\)\.")
def _iris(m, card):
    """Iris. Fixed when played, which is when the count is what it will be.

    Nothing between playing a Supporter and attacking can change how many
    prizes the opponent has taken, so reading it here rather than at damage
    time is the same number by every route a turn can actually take.
    """
    per = int(m.group(1))

    def effect(state, ctx, changes):
        player = ctx["player"]
        taken = state.rules.prize_count - len(state.players[1 - player].prizes)
        if taken <= 0:
            return None
        engine.add_modifier(state, changes, Modifier(
            kind=MOD_DAMAGE_DEALT, until_turn=state.turn_number,
            player=player, amount=per * taken, source=ctx.get("source"),
            detail={"card": card.name}))
    return effect


@trainer(r"Choose a " + POKEMON + r" Tool or Special Energy card attached to "
         r"a " + POKEMON + r" in play \(yours or your opponent's\) and discard "
         r"it\.")
def _xerosic(m, card):
    """Xerosic. Either side of the board, Tools and Special Energy alike."""
    def attached(state):
        out = []
        for p in (0, 1):
            for slot in state.players[p].in_play:
                out += list(slot.tools)
                out += [c for c in slot.energy if is_special_energy(state, c)]
        return tuple(out)

    @playable(lambda state, player: bool(attached(state)))
    def effect(state, ctx, changes):
        player = ctx["player"]
        options = attached(state)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardAttached", options=options,
            option_kind=CHOICE_CARD, detail={"card": card.name})
            if options else None)
        if choice is not None:
            return choice
        for cid in picks:
            engine.move_card(state, cid, ZONE_DISCARD, changes)
    return effect


@trainer(r"Move an Energy from 1 of your Benched " + POKEMON + r" to your "
         r"Active " + POKEMON + r"\.")
def _multi_switch(m, card):
    """Multi Switch. Any Energy, and only bench -> Active."""
    def ready(state, player):
        ps = state.players[player]
        return bool(ps.active) and any(s.energy for s in ps.bench)

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        movable = tuple(c for s in ps.bench for c in s.energy)
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="moveEnergy", options=movable,
            option_kind=CHOICE_CARD, detail={"card": card.name})
            if movable else None)
        if choice is not None:
            return choice
        if not picks or ps.active is None:
            return None
        source = slot_holding(state, player, picks[0])
        if source is None or source is ps.active:
            return None
        source.energy.remove(picks[0])
        ps.active.energy.append(picks[0])
        changes.append(engine.Change(engine.CHANGE_ATTACH, player=player,
                                     card=picks[0], slot=ps.active.slot_id,
                                     detail={"movedFrom": source.slot_id}))
    return effect


# --------------------------------------------------------------------------
# Tools and Stadiums that only add a number
# --------------------------------------------------------------------------
#
# All of these are continuous, so they are @static and their on-play half does
# nothing. A Stadium goes through the same door as a Tool: _static() always
# consults the Stadium in play, so the hook is asked about every Pokemon on
# the board rather than only the one it is attached to.

def _is_active(state, slot) -> bool:
    if slot is None:
        return False
    return any(state.players[p].active is slot for p in (0, 1))


@static(r"The " + POKEMON + r" this card is attached to gets \+" + N + r" HP\.")
def _giant_cape(m, card):
    amount = int(m.group(1))

    def hook(query, state, ctx, value):
        return value + amount if query == STATIC_MAX_HP else value
    return _tool(hook)


@static(r"Each " + POKEMON + r" that has any \{([A-Z])\} Energy attached to it "
        r"\(both yours and your opponent's\) has no Weakness\.")
def _shadow_circle(m, card):
    """Shadow Circle. Fairy Garden's shape, aimed at Weakness instead."""
    code = m.group(1)
    if SYMBOLS.get(code) is None:
        return None

    def hook(query, state, ctx, value):
        if query != STATIC_NO_WEAKNESS:
            return value
        slot = ctx.get("slot")
        if slot is None or not _count_symbol(state, slot, code):
            return value
        return 1
    return _tool(hook)


@static(r"The attacks of the " + POKEMON + r" this card is attached to do " + N
        + r" more damage to your opponent's Active " + POKEMON
        + r" \(before applying Weakness and Resistance\)\.")
def _muscle_band(m, card):
    """Muscle Band. Only against the ACTIVE, which is the whole restriction.

    The hook is reached only through the attacker's own sources, so the one
    thing left to check is that the Pokemon being hit is an Active - a snipe
    at the bench gets nothing.
    """
    amount = int(m.group(1))

    def hook(query, state, ctx, value):
        if query != STATIC_DAMAGE_DEALT:
            return value
        if not _is_active(state, ctx.get("defender")):
            return value
        return value + amount
    return _tool(hook)


@static(r"The attacks of the " + POKEMON + r" this card is attached to do " + N
        + r" more damage to your opponent's Active " + POKEMON + r"-GX or "
          r"Active " + POKEMON + r"-EX \(before applying Weakness and "
          r"Resistance\)\.")
def _choice_band(m, card):
    """Choice Band. "Pokemon-GX or Pokemon-EX" is exactly the rule-box set.

    prize_value() is the same judgement the prize count is built on, and it
    returns 2 for precisely the EX and GX printings, so asking it here is
    asking the one question the card asks rather than a proxy for it.
    """
    amount = int(m.group(1))

    def hook(query, state, ctx, value):
        if query != STATIC_DAMAGE_DEALT:
            return value
        defender = ctx.get("defender")
        if not _is_active(state, defender):
            return value
        if prize_value(state.pokemon(defender)) < 2:
            return value
        return value + amount
    return _tool(hook)


@static(r"The Stage 1 " + POKEMON + r" this card is attached to gets \+" + N
        + r" HP\.")
def _bodybuilding_dumbbells(m, card):
    amount = int(m.group(1))

    def hook(query, state, ctx, value):
        if query != STATIC_MAX_HP:
            return value
        slot = ctx.get("slot")
        if slot is None or state.pokemon(slot).stage != "Stage1":
            return value
        return value + amount
    return _tool(hook)


@static(r"Each Stage 1 and Stage 2 " + POKEMON + r" in play \(both yours and "
        r"your opponent's\) gets \+" + N + r" HP\.")
def _training_center(m, card):
    """Training Center. A Stadium, so it is asked about both players' slots."""
    amount = int(m.group(1))

    def hook(query, state, ctx, value):
        if query != STATIC_MAX_HP:
            return value
        slot = ctx.get("slot")
        if slot is None or state.pokemon(slot).stage not in ("Stage1", "Stage2"):
            return value
        return value + amount
    return _tool(hook)


@static(r"Each \{([A-Z])\} " + POKEMON + r" in play \(both yours and your "
        r"opponent's\) gets \+" + N + r" HP\.")
def _type_gym(m, card):
    """Aspertia City Gym and its colour-swapped siblings."""
    want = SYMBOLS.get(m.group(1))
    amount = int(m.group(2))
    if want is None:
        return None

    def hook(query, state, ctx, value):
        if query != STATIC_MAX_HP:
            return value
        slot = ctx.get("slot")
        if slot is None or want not in state.pokemon(slot).types:
            return value
        return value + amount
    return _tool(hook)


@static(r"Each " + POKEMON + r" that has any \{([A-Z])\} Energy attached to it "
        r"\(both yours and your opponent's\) has no Retreat Cost\.")
def _fairy_garden(m, card):
    """Fairy Garden. Retreat is free for anything carrying that colour."""
    code = m.group(1)
    if SYMBOLS.get(code) is None:
        return None

    def hook(query, state, ctx, value):
        if query != STATIC_RETREAT_COST:
            return value
        slot = ctx.get("slot")
        if slot is None or not _count_symbol(state, slot, code):
            return value
        return 0
    return _tool(hook)


# --------------------------------------------------------------------------
# attacks: how much damage
# --------------------------------------------------------------------------
#
# These return the base damage for one use of the attack, BEFORE Weakness and
# Resistance. They run before the damage lands (see EXTENSION POINT 1 in
# engine.py) and must not return a Choice.
#
# Numbers come from the text and not from the `damage` field, because the two
# disagree: an "x" attack sometimes carries the per-unit amount in `damage`
# and sometimes carries zero.

@attack_damage(r"Flip a coin\. If heads, this attack does " + N
               + r" more damage\.")
def _flip_for_more(m, ability):
    extra = int(m.group(1))

    def damage(state, ctx, changes):
        heads = _flip_count(state, ctx, 1, ability, changes)
        return ctx["attack"].damage + (extra if heads else 0)
    return damage


@attack_damage(r"Flip " + N + r" coins\. This attack does " + N
               + r" damage times the number of heads\.")
def _flip_times(m, ability):
    coins, each = int(m.group(1)), int(m.group(2))

    def damage(state, ctx, changes):
        return each * _flip_count(state, ctx, coins, ability, changes)
    return damage


@attack_damage(r"Flip " + N + r" coins\. This attack does " + N
               + r" (?:more damage for each heads|damage for each heads)\.")
def _flip_more_each(m, ability):
    coins, each = int(m.group(1)), int(m.group(2))
    more = "more damage" in m.group(0)

    def damage(state, ctx, changes):
        heads = _flip_count(state, ctx, coins, ability, changes)
        base = ctx["attack"].damage if more else 0
        return base + each * heads
    return damage


@attack_damage(r"Flip a coin until you get tails\. This attack does " + N
               + r" (?:more )?damage times the number of heads\.")
@attack_damage(r"Flip a coin until you get tails\. This attack does " + N
               + r" more damage for each heads\.")
def _flip_until_tails(m, ability):
    each = int(m.group(1))
    more = "more" in m.group(0)

    def damage(state, ctx, changes):
        if "heads" not in ctx["data"]:
            ctx["data"]["heads"] = flip_until_tails(
                state, changes, ability.title, ctx["player"])
        heads = ctx["data"]["heads"]
        base = ctx["attack"].damage if more else 0
        return base + each * heads
    return damage


@attack_damage(r"Flip a coin\. If tails, this attack does nothing\.")
def _flip_or_nothing(m, ability):
    def damage(state, ctx, changes):
        heads = _flip_count(state, ctx, 1, ability, changes)
        return ctx["attack"].damage if heads else 0
    return damage


@attack_damage(r"Flip " + N + r" coins\. If either of them is tails, this "
               r"attack does nothing\.")
def _flip_both_or_nothing(m, ability):
    coins = int(m.group(1))

    def damage(state, ctx, changes):
        heads = _flip_count(state, ctx, coins, ability, changes)
        return ctx["attack"].damage if heads == coins else 0
    return damage


@attack_damage(r"Does " + N + r" more damage for each damage counter on this "
               + POKEMON + r"\.")
def _more_per_counter(m, ability):
    each = int(m.group(1))

    def damage(state, ctx, changes):
        counters = ctx["attacker"].damage // 10
        return ctx["attack"].damage + each * counters
    return damage


@attack_damage(r"Does " + N + r" damage minus " + N + r" damage for each "
               r"damage counter on this " + POKEMON + r"\.")
def _less_per_counter(m, ability):
    base, each = int(m.group(1)), int(m.group(2))

    def damage(state, ctx, changes):
        return max(0, base - each * (ctx["attacker"].damage // 10))
    return damage


@attack_damage(r"Does " + N + r" damage minus " + N + r" damage for each "
               r"\{C\} in the Defending " + POKEMON + r"'s Retreat Cost\.")
def _less_per_retreat(m, ability):
    base, each = int(m.group(1)), int(m.group(2))

    def damage(state, ctx, changes):
        defender = ctx["defender"]
        cost = engine.retreat_cost(state, defender) if defender else 0
        return max(0, base - each * cost)
    return damage


@attack_damage(r"Does " + N + r" more damage for each of your opponent's "
               r"Benched " + POKEMON + r"\.")
def _more_per_their_bench(m, ability):
    each = int(m.group(1))

    def damage(state, ctx, changes):
        them = state.players[1 - ctx["player"]]
        return ctx["attack"].damage + each * len(them.bench)
    return damage


@attack_effect(r"Your opponent reveals (?:his or her|their) hand\.")
def _reveal_their_hand(m, ability):
    def effect(state, ctx, changes):
        them = 1 - ctx["player"]
        engine.reveal(state, changes, them, state.players[them].hand,
                      reason="attack")
    return effect


@attack_damage(r"Does " + N + r" more damage for each Energy attached to the "
               r"Defending " + POKEMON + r"\.")
def _more_per_defender_energy(m, ability):
    each = int(m.group(1))

    def damage(state, ctx, changes):
        defender = ctx["defender"]
        return ctx["attack"].damage + each * (len(defender.energy) if defender else 0)
    return damage


@attack_damage(r"Does " + N + r" more damage for each \{(\w)\} Energy attached "
               r"to this " + POKEMON + r"\.")
def _more_per_own_energy(m, ability):
    each, symbol = int(m.group(1)), m.group(2)

    def damage(state, ctx, changes):
        return ctx["attack"].damage + each * _count_symbol(state, ctx["attacker"],
                                                           symbol)
    return damage


@attack_damage(r"Does " + N + r" damage times the number of your Benched "
               + POKEMON + r"\.")
def _times_bench(m, ability):
    each = int(m.group(1))

    def damage(state, ctx, changes):
        return each * len(state.players[ctx["player"]].bench)
    return damage


@attack_damage(r"Does " + N + r" more damage for each Energy attached to this "
               + POKEMON + r"\.")
def _more_per_any_energy(m, ability):
    each = int(m.group(1))

    def damage(state, ctx, changes):
        return ctx["attack"].damage + each * len(ctx["attacker"].energy)
    return damage


@attack_damage(r"If this " + POKEMON + r" has a " + POKEMON + r" Tool card "
               r"attached to it, this attack does " + N + r" more damage\.")
def _more_with_tool(m, ability):
    extra = int(m.group(1))

    def damage(state, ctx, changes):
        return ctx["attack"].damage + (extra if ctx["attacker"].tools else 0)
    return damage


# Colour codes as they appear inside game text: {R} Fire, {W} Water, and so on.
SYMBOLS = {"G": "Grass", "R": "Fire", "W": "Water", "L": "Lightning",
           "P": "Psychic", "F": "Fighting", "D": "Darkness", "M": "Metal",
           "Y": "Fairy", "N": "Dragon", "C": "Colorless"}


def _count_symbol(state, slot, code) -> int:
    want = SYMBOLS.get(code)
    if want is None or slot is None:
        return 0
    total = 0
    for cid in slot.energy:
        for option in state.card(cid).energy_options:
            if want in option:
                total += list(option).count(want)
                break
    return total


def _flip_count(state, ctx, coins, ability, changes) -> int:
    """Flip once per use and remember it in ctx["data"].

    The damage hook and the after-effect are two separate calls on the same
    attack, and both may want the result. Flipping twice would make "if heads
    this does 10 more damage and the Defending Pokemon is now Paralyzed" roll
    the coin twice for one printed flip.
    """
    if "heads" not in ctx["data"]:
        ctx["data"]["heads"] = flips(state, changes, coins,
                                     ability.title, ctx["player"])
    return ctx["data"]["heads"]


# --------------------------------------------------------------------------
# attacks: everything else the text says
# --------------------------------------------------------------------------

CONDITIONS = {"Asleep": engine.ASLEEP, "Burned": engine.BURNED,
              "Confused": engine.CONFUSED, "Paralyzed": engine.PARALYZED,
              "Poisoned": engine.POISONED}


@attack_effect(r"(?:Your opponent's Active|The Defending) " + POKEMON
               + r" is now (\w+)\.")
def _inflict(m, ability):
    condition = CONDITIONS.get(m.group(1))
    if condition is None:
        return None

    def effect(state, ctx, changes):
        if ctx["defender"] is not None:
            engine.add_condition(state, ctx["defender"], condition, changes)
    return effect


@attack_effect(r"Flip a coin\. If heads, (?:your opponent's Active|the "
               r"Defending) " + POKEMON + r" is now (\w+)\.")
def _flip_inflict(m, ability):
    condition = CONDITIONS.get(m.group(1))
    if condition is None:
        return None

    def effect(state, ctx, changes):
        if ctx["defender"] is None:
            return None
        if engine.flip_coin(state, changes, ability.title, ctx["player"]):
            engine.add_condition(state, ctx["defender"], condition, changes)
    return effect


@attack_effect(r"This " + POKEMON + r" does " + N + r" damage to itself\.")
def _self_damage(m, ability):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        engine.apply_damage(state, ctx["attacker"], amount, changes,
                            {"source": "recoil"})
    return effect


@attack_effect(r"Flip a coin\. If tails, this " + POKEMON + r" does " + N
               + r" damage to itself\.")
def _flip_self_damage(m, ability):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        if not engine.flip_coin(state, changes, ability.title, ctx["player"]):
            engine.apply_damage(state, ctx["attacker"], amount, changes,
                                {"source": "recoil"})
    return effect


@attack_effect(r"Heal " + N + r" damage from this " + POKEMON + r"\.")
def _self_heal(m, ability):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        engine.heal(state, ctx["attacker"], amount, changes)
    return effect


@attack_effect(r"Heal from this " + POKEMON + r" the same amount of damage you "
               r"did to your opponent's Active " + POKEMON + r"\.")
def _drain(m, ability):
    def effect(state, ctx, changes):
        engine.heal(state, ctx["attacker"], ctx.get("damage") or 0, changes)
    return effect


@attack_effect(r"Discard all Energy attached to this " + POKEMON + r"\.")
def _discard_all_own_energy(m, ability):
    def effect(state, ctx, changes):
        slot = ctx["attacker"]
        engine.discard_attached(state, slot, list(slot.energy), changes)
    return effect


@attack_effect(r"Discard an Energy attached to this " + POKEMON + r"\.")
def _discard_own_energy(m, ability):
    def effect(state, ctx, changes):
        slot = ctx["attacker"]
        if slot is None or not slot.energy:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="discardEnergy",
            options=tuple(slot.energy), option_kind=CHOICE_CARD,
            detail={"attack": ability.title}))
        if choice is not None:
            return choice
        engine.discard_attached(state, slot, picks, changes)
    return effect


@attack_effect(r"Discard a \{(\w)\} Energy attached to this " + POKEMON + r"\.")
def _discard_own_coloured_energy(m, ability):
    symbol = SYMBOLS.get(m.group(1))
    if symbol is None:
        return None

    def effect(state, ctx, changes):
        slot = ctx["attacker"]
        if slot is None:
            return None
        options = tuple(cid for cid in slot.energy
                        if any(symbol in o
                               for o in state.card(cid).energy_options))
        if not options:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="discardEnergy", options=options,
            option_kind=CHOICE_CARD, detail={"attack": ability.title}))
        if choice is not None:
            return choice
        engine.discard_attached(state, slot, picks, changes)
    return effect


@attack_effect(r"Flip a coin\. If heads, discard an Energy attached to "
               r"(?:your opponent's Active|the Defending) " + POKEMON + r"\.")
def _flip_discard_defender_energy(m, ability):
    def effect(state, ctx, changes):
        if "heads" not in ctx["data"]:
            ctx["data"]["heads"] = engine.flip_coin(state, changes,
                                                    ability.title, ctx["player"])
        slot = ctx["defender"]
        if not ctx["data"]["heads"] or slot is None or not slot.energy:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="discardEnergy",
            options=tuple(slot.energy), option_kind=CHOICE_CARD,
            detail={"attack": ability.title}))
        if choice is not None:
            return choice
        engine.discard_attached(state, slot, picks, changes)
    return effect


@attack_effect(r"The Defending " + POKEMON + r" can't retreat during your "
               r"opponent's next turn\.")
def _no_retreat(m, ability):
    def effect(state, ctx, changes):
        if ctx["defender"] is None:
            return None
        engine.add_modifier(state, changes, Modifier(
            kind=MOD_NO_RETREAT, until_turn=state.turn_number + 1,
            slot=ctx["defender"].slot_id, detail={"attack": ability.title}))
    return effect


@attack_effect(r"During your opponent's next turn, any damage done to this "
               + POKEMON + r" by attacks is reduced by " + N + r" \(after "
               r"applying Weakness and Resistance\)\.")
def _reduce_incoming(m, ability):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        engine.add_modifier(state, changes, Modifier(
            kind=MOD_DAMAGE_TAKEN, until_turn=state.turn_number + 1,
            slot=ctx["attacker"].slot_id, amount=amount,
            detail={"attack": ability.title}))
    return effect


@attack_effect(r"Flip a coin\. If heads, prevent all (?:effects of attacks, "
               r"including damage,|damage) done to this " + POKEMON
               + r" by attacks during your opponent's next turn\.")
@attack_effect(r"Flip a coin\. If heads, prevent all damage done to this "
               + POKEMON + r" by attacks during your opponent's next turn\.")
def _flip_protect(m, ability):
    def effect(state, ctx, changes):
        if not engine.flip_coin(state, changes, ability.title, ctx["player"]):
            return None
        engine.add_modifier(state, changes, Modifier(
            kind=engine.MOD_PREVENT_DAMAGE, until_turn=state.turn_number + 1,
            slot=ctx["attacker"].slot_id, detail={"attack": ability.title}))
    return effect


@attack_effect(r"Switch this " + POKEMON + r" with 1 of your Benched "
               + POKEMON + r"\.")
def _self_switch(m, ability):
    def effect(state, ctx, changes):
        player = ctx["player"]
        bench = state.players[player].bench
        if not bench or state.players[player].active is not ctx["attacker"]:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="switchTo", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"attack": ability.title}))
        if choice is not None:
            return choice
        for slot_id in picks:
            engine.switch_active(state, player, slot_id, changes)
    return effect


@attack_effect(r"Your opponent switches (?:the Defending|their Active) "
               + POKEMON + r" with 1 of their Benched " + POKEMON + r"\.")
def _opponent_switches(m, ability):
    def effect(state, ctx, changes):
        them = 1 - ctx["player"]
        bench = state.players[them].bench
        if not bench:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=them, prompt="switchTo", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"attack": ability.title}))
        if choice is not None:
            return choice
        for slot_id in picks:
            engine.switch_active(state, them, slot_id, changes)
    return effect


@attack_effect(r"This attack does " + N + r" damage to " + N + r" of your "
               r"opponent's Benched " + POKEMON + r"\. \(Don't apply Weakness "
               r"and Resistance for Benched " + POKEMON + r"\.\)")
@attack_effect(r"Does " + N + r" damage to " + N + r" of your opponent's "
               r"Benched " + POKEMON + r"\. \(Don't apply Weakness and "
               r"Resistance for Benched " + POKEMON + r"\.\)")
def _snipe_bench(m, ability):
    amount, count = int(m.group(1)), int(m.group(2))

    def effect(state, ctx, changes):
        them = 1 - ctx["player"]
        bench = state.players[them].bench
        if not bench:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="snipeTarget",
            options=slot_ids(bench), option_kind=CHOICE_SLOT,
            minimum=min(count, len(bench)), maximum=min(count, len(bench)),
            detail={"attack": ability.title, "amount": amount}))
        if choice is not None:
            return choice
        for slot_id in picks:
            slot = slot_of(state, slot_id)
            if slot is not None:
                engine.apply_damage(state, slot, amount, changes,
                                    {"source": "bench"})
    return effect


@attack_effect(r"This attack does " + N + r" damage to each of your opponent's "
               + POKEMON + r"\. \(Don't apply Weakness and Resistance for "
               r"Benched " + POKEMON + r"\.\)")
def _spread_all_theirs(m, ability):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        # The Active already took the attack's own damage; this sentence adds
        # the same number to every Pokemon they have, Active included, so the
        # Active is skipped here only when it is the attack's own target.
        for slot in state.players[1 - ctx["player"]].bench:
            engine.apply_damage(state, slot, amount, changes,
                                {"source": "spread"})
    return effect


@attack_effect(r"This attack does " + N + r" damage to each of your Benched "
               + POKEMON + r"\. \(Don't apply Weakness and Resistance for "
               r"Benched " + POKEMON + r"\.\)")
def _spread_own_bench(m, ability):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        for slot in state.players[ctx["player"]].bench:
            engine.apply_damage(state, slot, amount, changes,
                                {"source": "spread"})
    return effect


@attack_effect(r"Draw a card\.")
@attack_effect(r"Draw " + N + r" cards\.")
def _attack_draw(m, ability):
    count = int(m.group(1)) if m.groups() else 1

    def effect(state, ctx, changes):
        engine.draw_cards(state, ctx["player"], count, changes)
    return effect


@attack_effect(r"Discard the top card of your opponent's deck\.")
def _mill_one(m, ability):
    def effect(state, ctx, changes):
        _mill(state, 1 - ctx["player"], 1, changes)
    return effect


@attack_effect(r"Discard the top " + N + r" cards of your deck\.")
def _mill_self(m, ability):
    count = int(m.group(1))

    def effect(state, ctx, changes):
        _mill(state, ctx["player"], count, changes)
    return effect


def _mill(state, player, count, changes):
    deck = state.players[player].deck
    for cid in list(deck[:count]):
        engine.move_card(state, cid, ZONE_DISCARD, changes)


@attack_effect(r"This " + POKEMON + r" can't attack during your next turn\.")
def _cannot_attack_next_turn(m, ability):
    """Modelled as a whole-turn effect the engine does not yet enforce.

    engine.py has no "this Pokemon may not attack" gate - _can_attack_now()
    tests conditions and the first-turn rule and nothing else - so this is
    recorded as a Modifier for the protocol layer to show and is NOT enforced.
    Registering it anyway is deliberate: the attack's damage and its other
    clauses are correct, and the alternative is leaving the whole attack
    inert. The gap is listed in EXTENSION POINTS.
    """
    def effect(state, ctx, changes):
        engine.add_modifier(state, changes, Modifier(
            kind="cannotAttack", until_turn=state.turn_number + 2,
            slot=ctx["attacker"].slot_id, detail={"attack": ability.title,
                                                  "enforced": False}))
    return effect


@attack_damage(r"This attack's damage isn't affected by Resistance\.")
@attack_damage(r"This attack's damage isn't affected by Weakness or "
               r"Resistance\.")
@attack_damage(r"This attack's damage isn't affected by Weakness, Resistance, "
               r"or any other effects on your opponent's Active " + POKEMON
               + r"\.")
@attack_damage(r"This attack's damage isn't affected by any effects on "
               r"(?:the Defending|your opponent's Active) " + POKEMON + r"\.")
def _ignores(m, ability):
    """"Isn't affected by ..." - 130-odd attacks between the four wordings.

    Registered as a damage hook because that is the only place the pipeline
    can be told before it runs; the number it returns is the printed one,
    unchanged.
    """
    text = m.group(0)
    ignore = []
    if "Weakness" in text:
        ignore.append(engine.IGNORE_WEAKNESS)
    if "Resistance" in text:
        ignore.append(engine.IGNORE_RESISTANCE)
    if "effects" in text:
        ignore.append(engine.IGNORE_EFFECTS)

    def damage(state, ctx, changes):
        ctx["data"]["ignore"] = ignore
        return ctx["attack"].damage
    return damage


@attack_damage(r"This attack does " + N + r" more damage for each damage "
               r"counter on your opponent's Active " + POKEMON + r"\.")
def _more_per_defender_counter(m, ability):
    each = int(m.group(1))

    def damage(state, ctx, changes):
        defender = ctx["defender"]
        return ctx["attack"].damage + each * ((defender.damage // 10)
                                              if defender else 0)
    return damage


@attack_damage(r"This attack does " + N + r" more damage for each Energy "
               r"attached to your opponent's Active " + POKEMON + r"\.")
def _more_per_their_energy(m, ability):
    each = int(m.group(1))

    def damage(state, ctx, changes):
        defender = ctx["defender"]
        return ctx["attack"].damage + each * (len(defender.energy) if defender
                                              else 0)
    return damage


@attack_damage(r"Flip a coin\. If heads, this attack does " + N + r" more "
               r"damage\. If tails, this " + POKEMON + r" does " + N
               + r" damage to itself\.")
def _flip_more_or_recoil(m, ability):
    extra, recoil = int(m.group(1)), int(m.group(2))

    def damage(state, ctx, changes):
        heads = _flip_count(state, ctx, 1, ability, changes)
        ctx["data"]["recoil"] = 0 if heads else recoil
        return ctx["attack"].damage + (extra if heads else 0)
    return damage


@attack_effect(r"Flip a coin\. If heads, this attack does " + N + r" more "
               r"damage\. If tails, this " + POKEMON + r" does " + N
               + r" damage to itself\.")
def _flip_more_or_recoil_effect(m, ability):
    def effect(state, ctx, changes):
        recoil = ctx["data"].get("recoil") or 0
        if recoil:
            engine.apply_damage(state, ctx["attacker"], recoil, changes,
                                {"source": "recoil"})
    return effect


@attack_effect(r"Flip a coin\. If heads, prevent all effects of attacks, "
               r"including damage, done to this " + POKEMON + r" during your "
               r"opponent's next turn\.")
def _flip_protect_all(m, ability):
    def effect(state, ctx, changes):
        if not engine.flip_coin(state, changes, ability.title, ctx["player"]):
            return None
        engine.add_modifier(state, changes, Modifier(
            kind=engine.MOD_PREVENT_DAMAGE, until_turn=state.turn_number + 1,
            slot=ctx["attacker"].slot_id, detail={"attack": ability.title}))
    return effect


@attack_effect(r"During your opponent's next turn, this " + POKEMON + r" has "
               r"no Weakness\.")
def _no_weakness_next_turn(m, ability):
    def effect(state, ctx, changes):
        engine.add_modifier(state, changes, Modifier(
            kind=engine.MOD_NO_WEAKNESS, until_turn=state.turn_number + 1,
            slot=ctx["attacker"].slot_id, detail={"attack": ability.title}))
    return effect


@attack_effect(r"During your opponent's next turn, any damage done by attacks "
               r"from the Defending " + POKEMON + r" is reduced by " + N
               + r" \(before applying Weakness and Resistance\)\.")
def _weaken_defender(m, ability):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        if ctx["defender"] is None:
            return None
        # A negative "damage dealt" on the defender's own slot: the same
        # modifier the attacker's PlusPower uses, pointed the other way.
        engine.add_modifier(state, changes, Modifier(
            kind=MOD_DAMAGE_DEALT, until_turn=state.turn_number + 1,
            slot=ctx["defender"].slot_id, amount=-amount,
            detail={"attack": ability.title}))
    return effect


@attack_effect(r"Heal from this " + POKEMON + r" the same amount of damage you "
               r"did to the Defending " + POKEMON + r"\.")
def _drain_defending(m, ability):
    def effect(state, ctx, changes):
        engine.heal(state, ctx["attacker"], ctx.get("damage") or 0, changes)
    return effect


@attack_effect(r"(?:This attack does|Does) " + N + r" damage to " + N + r" of "
               r"your opponent's " + POKEMON + r"\. \(Don't apply Weakness and "
               r"Resistance for Benched " + POKEMON + r"\.\)")
def _snipe_any(m, ability):
    """Any of their Pokemon, Active included - the Active is a legal target
    here and takes the damage a second time, which is what the card says."""
    amount, count = int(m.group(1)), int(m.group(2))

    def effect(state, ctx, changes):
        them = 1 - ctx["player"]
        targets = state.players[them].in_play
        if not targets:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="snipeTarget",
            options=slot_ids(targets), option_kind=CHOICE_SLOT,
            minimum=min(count, len(targets)), maximum=min(count, len(targets)),
            detail={"attack": ability.title, "amount": amount}))
        if choice is not None:
            return choice
        for slot_id in picks:
            slot = slot_of(state, slot_id)
            if slot is not None:
                engine.apply_damage(state, slot, amount, changes,
                                    {"source": "snipe"})
    return effect


@attack_effect(r"(?:This attack does|Does) " + N + r" damage to each of your "
               r"opponent's Benched " + POKEMON + r"\. \(Don't apply Weakness "
               r"and Resistance for Benched " + POKEMON + r"\.\)")
def _spread_their_bench(m, ability):
    amount = int(m.group(1))

    def effect(state, ctx, changes):
        for slot in state.players[1 - ctx["player"]].bench:
            engine.apply_damage(state, slot, amount, changes,
                                {"source": "spread"})
    return effect


@attack_effect(r"You may switch this " + POKEMON + r" with 1 of your Benched "
               + POKEMON + r"\.")
def _may_self_switch(m, ability):
    def effect(state, ctx, changes):
        player = ctx["player"]
        bench = state.players[player].bench
        if not bench or state.players[player].active is not ctx["attacker"]:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="switchTo", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, minimum=0, maximum=1,
            detail={"attack": ability.title}))
        if choice is not None:
            return choice
        for slot_id in picks:
            engine.switch_active(state, player, slot_id, changes)
    return effect


@attack_effect(r"Switch (?:1 of your opponent's Benched " + POKEMON
               + r" with their Active|the Defending " + POKEMON + r" with 1 of "
               r"your opponent's Benched) " + POKEMON + r"\.")
def _gust(m, ability):
    def effect(state, ctx, changes):
        player = ctx["player"]
        them = 1 - player
        bench = state.players[them].bench
        if not bench:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="gustTarget", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"attack": ability.title}))
        if choice is not None:
            return choice
        for slot_id in picks:
            engine.switch_active(state, them, slot_id, changes)
    return effect


@attack_effect(r"Discard an Energy from (?:your opponent's Active|the "
               r"Defending) " + POKEMON + r"\.")
@attack_effect(r"Discard an Energy attached to (?:your opponent's Active|the "
               r"Defending) " + POKEMON + r"\.")
def _discard_defender_energy(m, ability):
    def effect(state, ctx, changes):
        slot = ctx["defender"]
        if slot is None or not slot.energy:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="discardEnergy",
            options=tuple(slot.energy), option_kind=CHOICE_CARD,
            detail={"attack": ability.title}))
        if choice is not None:
            return choice
        engine.discard_attached(state, slot, picks, changes)
    return effect


@attack_effect(r"Flip a coin\. If heads, discard an Energy from (?:your "
               r"opponent's Active|the Defending) " + POKEMON + r"\.")
def _flip_discard_defender_energy_from(m, ability):
    return _flip_discard_defender_energy(m, ability)


@attack_effect(r"Discard a Special Energy attached to (?:your opponent's "
               r"Active|the Defending) " + POKEMON + r"\.")
def _discard_defender_special(m, ability):
    def effect(state, ctx, changes):
        slot = ctx["defender"]
        if slot is None:
            return None
        options = tuple(cid for cid in slot.energy
                        if is_special_energy(state, cid))
        if not options:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="discardEnergy", options=options,
            option_kind=CHOICE_CARD, detail={"attack": ability.title}))
        if choice is not None:
            return choice
        engine.discard_attached(state, slot, picks, changes)
    return effect


@attack_effect(r"Flip a coin\. If tails, discard an Energy attached to this "
               + POKEMON + r"\.")
def _flip_tails_discard_own(m, ability):
    def effect(state, ctx, changes):
        if engine.flip_coin(state, changes, ability.title, ctx["player"]):
            return None
        slot = ctx["attacker"]
        if slot is None or not slot.energy:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="discardEnergy",
            options=tuple(slot.energy), option_kind=CHOICE_CARD,
            detail={"attack": ability.title}))
        if choice is not None:
            return choice
        engine.discard_attached(state, slot, picks, changes)
    return effect


@attack_effect(r"Discard " + N + r" \{(\w)\} Energy attached to this " + POKEMON
               + r"\.")
def _discard_n_coloured(m, ability):
    count, symbol = int(m.group(1)), SYMBOLS.get(m.group(2))
    if symbol is None:
        return None

    def effect(state, ctx, changes):
        slot = ctx["attacker"]
        if slot is None:
            return None
        options = tuple(cid for cid in slot.energy
                        if any(symbol in o
                               for o in state.card(cid).energy_options))
        if len(options) < count:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=ctx["player"], prompt="discardEnergy", options=options,
            option_kind=CHOICE_CARD, minimum=count, maximum=count,
            detail={"attack": ability.title}))
        if choice is not None:
            return choice
        engine.discard_attached(state, slot, picks, changes)
    return effect


@attack_effect(r"Move an Energy from this " + POKEMON + r" to 1 of your "
               r"Benched " + POKEMON + r"\.")
def _move_own_energy(m, ability):
    def effect(state, ctx, changes):
        player = ctx["player"]
        slot = ctx["attacker"]
        bench = state.players[player].bench
        if slot is None or not slot.energy or not bench:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="moveEnergy", options=tuple(slot.energy),
            option_kind=CHOICE_CARD, detail={"attack": ability.title}))
        if choice is not None:
            return choice
        dest, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="energyTarget", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"attack": ability.title}))
        if choice is not None:
            return choice
        target = slot_of(state, dest[0]) if dest else None
        if target is None or not picks:
            return None
        slot.energy.remove(picks[0])
        target.energy.append(picks[0])
        changes.append(engine.Change(engine.CHANGE_ATTACH, player=player,
                                     card=picks[0], slot=target.slot_id,
                                     detail={"movedFrom": slot.slot_id}))
    return effect


@attack_effect(r"Attach a basic Energy card from your discard pile to 1 of "
               r"your Benched " + POKEMON + r"\.")
def _attach_from_discard(m, ability):
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        found = distinct(state, [c for c in ps.discard
                                 if is_basic_energy(state, c)])
        bench = ps.bench
        if not found or not bench:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="fromDiscard", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=1, zone=ZONE_DISCARD,
            detail={"attack": ability.title}))
        if choice is not None:
            return choice
        if not picks:
            return None
        dest, choice = step(ctx, 1, lambda: Choice(
            player=player, prompt="energyTarget", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"attack": ability.title}))
        if choice is not None:
            return choice
        target = slot_of(state, dest[0]) if dest else None
        if target is None:
            return None
        ps.discard.remove(picks[0])
        target.energy.append(picks[0])
        changes.append(engine.Change(engine.CHANGE_MOVE, player=player,
                                     card=picks[0], slot=target.slot_id,
                                     from_zone=ZONE_DISCARD,
                                     to_zone=engine.ZONE_BENCH))
        changes.append(engine.Change(engine.CHANGE_ATTACH, player=player,
                                     card=picks[0], slot=target.slot_id))
    return effect


@attack_effect(r"Search your deck for (?:a|up to " + N + r"|" + N + r") Basic "
               + POKEMON + r" and put (?:it|them) onto your Bench\. (?:Shuffle "
               r"your deck afterward|Then, shuffle your deck)\.")
def _attack_bench_search(m, ability):
    want = int(m.group(1) or m.group(2) or 1) if m.groups() else 1

    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        room = state.rules.bench_size - len(ps.bench)
        found = distinct(state, [c for c in ps.deck
                                 if state.card(c).is_basic_pokemon])
        limit = min(want, room, len(found))
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="searchDeck", options=found,
            option_kind=CHOICE_CARD, minimum=0, maximum=limit, zone=ZONE_DECK,
            detail={"attack": ability.title}) if limit > 0 else None)
        if choice is not None:
            return choice
        for cid in picks:
            engine.bench_card(state, player, cid, changes)
        engine.shuffle_deck(state, player, changes)
    return effect


@attack_effect(r"Draw cards until you have " + N + r" cards in your hand\.")
def _attack_draw_up_to(m, ability):
    target = int(m.group(1))

    def effect(state, ctx, changes):
        ps = state.players[ctx["player"]]
        engine.draw_cards(state, ctx["player"], max(0, target - len(ps.hand)),
                          changes)
    return effect


@attack_effect(r"Discard a random card from your opponent's hand\.")
def _discard_random(m, ability):
    def effect(state, ctx, changes):
        them = 1 - ctx["player"]
        hand = state.players[them].hand
        if not hand:
            return None
        engine.move_card(state, state.rng.choice(list(hand)), ZONE_DISCARD,
                         changes, detail={"random": True})
    return effect


@attack_effect(r"Choose a random card from your opponent's hand\. Your "
               r"opponent reveals that card and shuffles it into their deck\.")
def _bounce_random(m, ability):
    def effect(state, ctx, changes):
        them = 1 - ctx["player"]
        hand = state.players[them].hand
        if not hand:
            return None
        engine.move_card(state, state.rng.choice(list(hand)), ZONE_DECK,
                         changes, detail={"random": True, "revealed": True})
        engine.shuffle_deck(state, them, changes)
    return effect


@attack_effect(r"Discard any Stadium card in play\.")
def _discard_stadium(m, ability):
    def effect(state, ctx, changes):
        _remove_stadium(state, changes)
    return effect


def _remove_stadium(state, changes):
    if state.stadium is None:
        return False
    old = state.stadium
    state.players[old.owner].discard.append(old.card)
    changes.append(engine.Change(engine.CHANGE_MOVE, player=old.owner,
                                 card=old.card, from_zone=engine.ZONE_STADIUM,
                                 to_zone=ZONE_DISCARD))
    state.stadium = None
    changes.append(engine.Change(engine.CHANGE_STADIUM, card=None,
                                 detail={"removed": True}))
    return True


# --------------------------------------------------------------------------
# more Trainers
# --------------------------------------------------------------------------

@trainer(r"Search your deck for a " + POKEMON + r", reveal it, and put it into "
         r"your hand\. Shuffle your deck afterward\.")
def _generic_pokemon_search(m, card):
    return _search(card, _any_pokemon, "hand")


@trainer(r"Flip a coin until you get tails\. For each heads, draw a card\.")
def _flip_draw(m, card):
    def effect(state, ctx, changes):
        heads = flip_until_tails(state, changes, card.name, ctx["player"])
        engine.draw_cards(state, ctx["player"], heads, changes)
    return effect


@trainer(r"Flip a coin\. If heads, draw " + N + r" cards\.")
def _flip_draw_n(m, card):
    count = int(m.group(1))

    def effect(state, ctx, changes):
        if engine.flip_coin(state, changes, card.name, ctx["player"]):
            engine.draw_cards(state, ctx["player"], count, changes)
    return effect


@trainer(r"Draw cards until you have the same number of cards in your hand as "
         r"your opponent\.")
def _match_opponent_hand(m, card):
    def effect(state, ctx, changes):
        player = ctx["player"]
        target = len(state.players[1 - player].hand)
        have = len(state.players[player].hand)
        engine.draw_cards(state, player, max(0, target - have), changes)
    return effect


@trainer(r"Your opponent shuffles their hand into their deck and draws " + N
         + r" cards\.")
def _opponent_shuffle_draw(m, card):
    count = int(m.group(1))

    def effect(state, ctx, changes):
        them = 1 - ctx["player"]
        shuffle_into_deck(state, them, list(state.players[them].hand), changes)
        engine.draw_cards(state, them, count, changes)
    return effect


@trainer(r"Your opponent switches their Active " + POKEMON + r" with 1 of "
         r"their Benched " + POKEMON + r"\.")
def _opponent_chooses_switch(m, card):
    @playable(lambda state, player: bool(state.players[1 - player].bench))
    def effect(state, ctx, changes):
        them = 1 - ctx["player"]
        bench = state.players[them].bench
        picks, choice = step(ctx, 0, lambda: Choice(
            player=them, prompt="switchTo", options=slot_ids(bench),
            option_kind=CHOICE_SLOT, detail={"card": card.name})
            if bench else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            engine.switch_active(state, them, slot_id, changes)
    return effect


@trainer(r"Put a " + POKEMON + r" from your discard pile on top of your deck\.")
def _pokemon_to_deck_top(m, card):
    def ready(state, player):
        return any(state.card(c).is_pokemon
                   for c in state.players[player].discard)

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        ps = state.players[player]
        found = distinct(state, [c for c in ps.discard
                                 if state.card(c).is_pokemon])
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="fromDiscard", options=found,
            option_kind=CHOICE_CARD, zone=ZONE_DISCARD,
            detail={"card": card.name}) if found else None)
        if choice is not None:
            return choice
        for cid in picks:
            ps.discard.remove(cid)
            ps.deck.insert(0, cid)
            changes.append(engine.Change(engine.CHANGE_MOVE, player=player,
                                         card=cid, from_zone=ZONE_DISCARD,
                                         to_zone=ZONE_DECK,
                                         detail={"onTop": True}))
    return effect


@trainer(r"Heal " + N + r" damage and remove all Special Conditions from 1 of "
         r"your " + POKEMON + r"\.")
def _pokemon_center_lady(m, card):
    amount = int(m.group(1))

    def ready(state, player):
        return any(s.damage or s.conditions for s in own_slots(state, player))

    @playable(ready)
    def effect(state, ctx, changes):
        player = ctx["player"]
        targets = [s for s in own_slots(state, player)
                   if s.damage or s.conditions]
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="healTarget", options=slot_ids(targets),
            option_kind=CHOICE_SLOT,
            detail={"card": card.name, "amount": amount}) if targets else None)
        if choice is not None:
            return choice
        for slot_id in picks:
            slot = slot_of(state, slot_id)
            if slot is None:
                continue
            engine.heal(state, slot, amount, changes)
            engine.clear_conditions(state, slot, changes, card.name)
    return effect


# --------------------------------------------------------------------------
# Pokemon Abilities
# --------------------------------------------------------------------------
#
# Two kinds, and the card data does not distinguish them: an Ability is
# "activated" precisely because it has an entry in Rules.ability_effects, and
# "continuous" precisely because it has one in Rules.static_effects. Nothing
# in attribute 200740 says which - abilityType is PokeAbility for both - so
# the English text is the only evidence there is.
#
# "Once during your turn (before your attack)" is the printed marker for an
# activated Ability and is what every pattern here keys on. The engine only
# offers UseAbility during the main phase and clears the per-Pokemon
# allowance at the start of every turn, so "once during your turn" falls out
# of Rules.ability_uses_per_turn with nothing further to enforce.

@ability(r"Once during your turn \(before your attack\), you may draw cards "
         r"until you have " + N + r" cards in your hand\.")
def _ability_draw_to(m, ab):
    target = int(m.group(1))

    def effect(state, ctx, changes):
        ps = state.players[ctx["player"]]
        engine.draw_cards(state, ctx["player"], max(0, target - len(ps.hand)),
                          changes)
    return effect


@ability(r"Once during your turn \(before your attack\), you may draw a card\.")
@ability(r"Once during your turn \(before your attack\), you may draw " + N
         + r" cards\.")
def _ability_draw(m, ab):
    count = int(m.group(1)) if m.groups() else 1

    def effect(state, ctx, changes):
        engine.draw_cards(state, ctx["player"], count, changes)
    return effect


@ability(r"Once during your turn \(before your attack\), you may discard a "
         r"card from your hand\. If you do, draw " + N + r" cards\.")
def _ability_discard_draw(m, ab):
    count = int(m.group(1))

    def effect(state, ctx, changes):
        player = ctx["player"]
        hand = tuple(state.players[player].hand)
        if not hand:
            return None
        picks, choice = step(ctx, 0, lambda: Choice(
            player=player, prompt="discardFromHand", options=hand,
            option_kind=CHOICE_CARD, zone=ZONE_HAND,
            detail={"ability": ab.title}))
        if choice is not None:
            return choice
        for cid in picks:
            engine.move_card(state, cid, ZONE_DISCARD, changes)
        engine.draw_cards(state, player, count, changes)
    return effect


@ability(r"Once during your turn \(before your attack\), if this " + POKEMON
         + r" is on your Bench, you may switch this " + POKEMON + r" with your "
           r"Active " + POKEMON + r"\.")
def _ability_self_promote(m, ab):
    def effect(state, ctx, changes):
        player = ctx["player"]
        slot = ctx["slot"]
        if slot is None or slot not in state.players[player].bench:
            return None
        engine.switch_active(state, player, slot.slot_id, changes)
    return effect


@ability_static(r"Any damage done to this " + POKEMON + r" by attacks is "
                r"reduced by " + N + r" \(after applying Weakness and "
                r"Resistance\)\.")
@ability_static(r"This " + POKEMON + r" takes " + N + r" less damage from "
                r"attacks \(after applying Weakness and Resistance\)\.")
def _ability_damage_reduction(m, ab):
    amount = int(m.group(1))

    def hook(query, state, ctx, value):
        if query != STATIC_DAMAGE_TAKEN:
            return value
        return value + amount
    return hook


@ability_static(r"If there is any Stadium card in play, this " + POKEMON
                + r" has no Retreat Cost\.")
def _ability_stadium_free_retreat(m, ab):
    def hook(query, state, ctx, value):
        if query != STATIC_RETREAT_COST or state.stadium is None:
            return value
        return 0
    return hook


@ability_static(r"This " + POKEMON + r" has no Retreat Cost\.")
def _ability_free_retreat(m, ab):
    def hook(query, state, ctx, value):
        return 0 if query == STATIC_RETREAT_COST else value
    return hook


@ability_static(r"The Retreat Cost of this " + POKEMON + r" is " + N
                + r" less\.")
def _ability_cheaper_retreat(m, ab):
    amount = int(m.group(1))

    def hook(query, state, ctx, value):
        return value - amount if query == STATIC_RETREAT_COST else value
    return hook


# --------------------------------------------------------------------------
# building the Rules
# --------------------------------------------------------------------------

def _match(table, text, card):
    for pattern, builder in table:
        m = pattern.match(text)
        if m:
            built = builder(m, card)
            if built is not None:
                return built
    return None


# Prizes. A Pokemon with a rule box is worth more than one prize, and nothing
# in carddata says which cards have one: there is no such attribute, and rarity
# does not separate them (RareUltra covers both a full-art Pokemon-EX and an
# ordinary full-art). What DOES separate them is the printed name, because the
# rule box is part of the name - a Pokemon-EX is stored as "MewtwoEX" and a
# Pokemon-GX as "TapuKokoGX".
#
# Checked against the pool rather than assumed: every one of the 295 cards
# whose rarity is RareHoloEX or RareHoloGX carries the suffix, none is missed,
# and the cards the suffix finds that those rarities do not are promos and
# full-arts of the same cards. See tests/test_effects.py.
#
# Deliberately NOT here:
#   BREAK   an evolution, worth one prize like the card underneath it.
#   LEGEND  worth two, but a LEGEND is two halves and the engine cannot put
#           one into play at all, so a value would never be read.
#   TAG TEAM GX  worth three. Named "...GX" like any other GX and not
#           separable by name, but every one of them is SM9 or later and no
#           such set has card data yet. Revisit with the SM9+ pool.
PRIZE_SUFFIXES = (("EX", 2), ("GX", 2))


def prize_value(card) -> int:
    """Prizes for knocking this printing out, or 1 for an ordinary Pokemon.

    The lowercase letter before the suffix is load-bearing: it is what keeps a
    name that merely ENDS in those letters from being read as a rule box.
    """
    name = card.name or ""
    for suffix, prizes in PRIZE_SUFFIXES:
        if len(name) > len(suffix) and name.endswith(suffix)                 and name[-len(suffix) - 1].islower():
            return prizes
    return 1


def build_rules(db, loc=None, base=None, localization_path=None):
    """A Rules with every card this module can read filled in.

    `db` is the CardDB the game will be played with; the registries are keyed
    by ids drawn from it, so a Rules built against one database must not be
    used with another.

    With no localization database available every registry comes back empty,
    which is exactly engine.DEFAULT_RULES - a machine with no game installed
    still runs the whole rules test suite, it just cannot play any card text.
    """
    base = base or engine.DEFAULT_RULES
    loc = load_localization(localization_path) if loc is None else loc

    trainers, statics = {}, {}
    damage, effects = {}, {}
    abilities = {}
    prizes = {}

    for card in db:
        if card.is_trainer:
            text = trainer_text(card, loc)
            if not text:
                continue
            built = _match(STATICS, text, card)
            if built is not None:
                on_play, hook = built
                trainers[card.guid] = on_play
                statics[card.guid] = hook
                continue
            built = _match(TRAINERS, text, card)
            if built is not None:
                trainers[card.guid] = built
            continue

        if not card.is_pokemon:
            continue
        worth = prize_value(card)
        if worth != 1:
            prizes[card.guid] = worth
        for entry in card.abilities:
            if not entry.ability_id:
                continue
            text = ability_text(entry, loc)
            if not text:
                continue
            if entry.is_attack:
                built = _match(ATTACK_DAMAGE, text, entry)
                if built is not None:
                    damage[entry.ability_id] = built
                built = _match(ATTACK_EFFECTS, text, entry)
                if built is not None:
                    effects[entry.ability_id] = built
                continue
            built = _match(ABILITIES, text, entry)
            if built is not None:
                abilities[entry.ability_id] = built
                continue
            built = _match(ABILITY_STATICS, text, entry)
            if built is not None:
                statics[entry.ability_id] = built

    # Merged onto whatever `base` already carries rather than replacing it, so
    # a caller can hand-write the cards the table cannot read and still get
    # everything it can.
    def merge(existing, found):
        out = dict(found)
        out.update(existing or {})
        return out

    return dataclasses.replace(
        base,
        trainer_effects=merge(base.trainer_effects, trainers),
        static_effects=merge(base.static_effects, statics),
        attack_damage=merge(base.attack_damage, damage),
        attack_effects=merge(base.attack_effects, effects),
        ability_effects=merge(base.ability_effects, abilities),
        prize_values=merge(base.prize_values, prizes))


_CACHE = {}


def rules_for(db, base=None):
    """build_rules(), memoised on the database. What a server should call.

    Building the registries walks every card and runs every pattern over
    every sentence - about 70ms - which is nothing once but real if it
    happens per match. The cache is keyed on the CardDB's identity because
    that is the thing the keys are drawn from.
    """
    key = (id(db), id(base))
    if key not in _CACHE:
        _CACHE[key] = build_rules(db, base=base)
    return _CACHE[key]


def coverage(db, loc=None, localization_path=None) -> dict:
    """How much of the card pool build_rules() actually understands.

    Reported rather than asserted: the number is a fact about the pattern
    table and the shipped localization, and it will move whenever either does.
    """
    loc = load_localization(localization_path) if loc is None else loc
    rules = build_rules(db, loc=loc)
    trainers = [c for c in db if c.is_trainer]
    attacks = [a for c in db if c.is_pokemon for a in c.attacks if a.ability_id]
    powers = [a for c in db if c.is_pokemon
              for a in c.pokemon_abilities if a.ability_id]
    done = set(rules.attack_damage) | set(rules.attack_effects)
    # Counted in PRINTINGS, not in ids. An abilityID is per-printing for
    # attacks (7,038 distinct across 12,204) but is shared between reprints of
    # a non-attack Ability, so counting ids would flatter one and understate
    # the other. What a player cares about is how many cards in their deck work.
    return {
        "trainers": len(trainers),
        "trainersWithText": sum(1 for c in trainers if trainer_text(c, loc)),
        "trainersImplemented": len(rules.trainer_effects),
        "trainerNames": len({c.name for c in trainers
                             if c.guid in rules.trainer_effects}),
        "attacks": len(attacks),
        "attacksWithText": sum(1 for a in attacks if ability_text(a, loc)),
        "attacksImplemented": sum(1 for a in attacks if a.ability_id in done),
        "abilities": len(powers),
        "abilitiesActivated": sum(1 for a in powers
                                  if a.ability_id in rules.ability_effects),
        "abilitiesStatic": sum(1 for a in powers
                               if a.ability_id in rules.static_effects),
        "tools": sum(1 for c in trainers if c.guid in rules.static_effects),
    }
