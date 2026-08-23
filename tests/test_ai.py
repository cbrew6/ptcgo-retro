"""
Tests for ai.py.

Two kinds of test, testing two different things.

The behaviour tests use *synthetic* archetypes built below, in the same shape
carddata/*.json uses, with numbers picked so that the right answer is the only
answer: a 30 damage attack against a Pokemon with exactly 30 HP left, an
Energy of a colour precisely one of two Pokemon can spend. Asserting that the
AI takes a knockout is only meaningful if there is exactly one knockout.

The soak test (AiNeverMisbehavesTests) is the important one. It plays whole
games AI against AI on decks assembled out of the real card database and
checks, on every single decision of every single game, that the action came
out of legal_actions(), that the state handed to the AI came back untouched -
including the game's own random number generator - and that the game ends.
That is the property server.py depends on: an exception or an illegal action
there does not fail a test, it hangs a real player's match.

Run: python -m unittest discover -s tests
"""

import collections
import copy
import itertools
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ai  # noqa: E402
import engine  # noqa: E402


# --------------------------------------------------------------------------
# synthetic card data
# --------------------------------------------------------------------------
#
# Deliberately a private copy of the builders in test_engine.py rather than an
# import: these fixtures are shaped by what the *AI* tests need to say, and
# coupling the two files would make either one hard to change.

_ids = itertools.count(9000)


def _s(n, v):
    return {"n": n, "v": {"s": v, "t": 3}}


def _i(n, v):
    return {"n": n, "v": {"i": v, "t": 5}}


def _b(n, v):
    return {"n": n, "v": {"b": v, "t": 4}}


def _a(n, values):
    return {"n": n, "v": {"a": [{"s": x, "t": 3} for x in values], "t": 1}}


def _archetype(attrs):
    n = next(_ids)
    return {"lo": n * 2 + 1, "hi": n * 2 + 2, "attrs": attrs}


def _attack(ability_id, cost, damage, title=""):
    return {"cost": dict(cost), "damage": damage, "title": title or ability_id,
            "gameText": "", "abilityID": ability_id, "amountOperator": "",
            "abilityType": "Attack", "conditionExceptions": []}


def _pokemon(name, hp, stage="Basic", types=("Colorless",), attacks=(),
             retreat=0, evolves_from=None, abilities=()):
    attrs = [
        _s(engine.ATTR_CARD_TYPES, "Pokemon"),
        _s(engine.ATTR_CARD_NAME, name),
        _s(engine.ATTR_STAGE, stage),
        _i(engine.ATTR_HP, hp),
        _a(engine.ATTR_POKEMON_TYPES, types),
        _a(engine.ATTR_WEAKNESS_TYPES, [engine.NO_COLOR]),
        _s(engine.ATTR_RESISTANCE_TYPE, engine.NO_COLOR),
    ]
    if retreat:
        attrs.append(_i(engine.ATTR_RETREAT_COST, retreat))
    if evolves_from:
        attrs.append(_s(engine.ATTR_EVOLVES_FROM, evolves_from))
    entries = list(attacks) + list(abilities)
    if entries:
        attrs.append({"n": engine.ATTR_ABILITIES, "t": 1, "v": {"a": [
            {"s": json.dumps(a), "t": 8} for a in entries], "t": 1}})
    return _archetype(attrs)


def _power(ability_id, title=""):
    """A non-attack Ability. Attacks and Abilities share ATTR_ABILITIES in
    the real data and differ only by abilityType, so the fixture does too."""
    return {"cost": {}, "damage": 0, "title": title or ability_id,
            "gameText": "", "abilityID": ability_id, "amountOperator": "",
            "abilityType": "PokeAbility", "conditionExceptions": []}


def _trainer(name, kind="Item"):
    return _archetype([
        _s(engine.ATTR_CARD_TYPES, "TrainerCard"),
        _s(engine.ATTR_CARD_NAME, name),
        _s(engine.ATTR_TRAINER_TYPES, kind),
    ])


def _energy(name, options):
    return _archetype([
        _s(engine.ATTR_CARD_TYPES, "Energy"),
        _s(engine.ATTR_CARD_NAME, name),
        _b(engine.ATTR_IS_BASIC_ENERGY, True),
        {"n": engine.ATTR_ENERGY_PROVIDED,
         "v": {"s": json.dumps({"options": [list(o) for o in options]}),
               "t": 8}},
    ])


ARCHETYPES = {
    # A solid opener: a cheap attack it can actually power.
    "Bruiser": _pokemon("Bruiser", 100, types=("Fighting",), retreat=1, attacks=[
        _attack("jab", {"Colorless": 1}, 30),
        _attack("slam", {"Colorless": 3}, 50)]),
    # The 30 HP filler the AI is supposed to leave in hand.
    "Filler": _pokemon("Filler", 30, attacks=[
        _attack("tackle", {"Colorless": 3}, 10)]),
    "Dummy": _pokemon("Dummy", 60, attacks=[_attack("poke", {"Colorless": 1}, 10)]),
    "Wall": _pokemon("Wall", 90, retreat=2),          # no attacks at all
    # Two Pokemon that need different colours, for the attachment test.
    "Firebug": _pokemon("Firebug", 80, types=("Fire",), retreat=1, attacks=[
        _attack("flare", {"Fire": 2}, 60)]),
    "Squirt": _pokemon("Squirt", 70, types=("Water",), retreat=1, attacks=[
        _attack("splash", {"Water": 1}, 20)]),
    # An evolution line where the Stage 1 is plainly better, and one where it
    # would cost the Active this turn's attack.
    "Pup": _pokemon("Pup", 60, attacks=[_attack("nip", {"Colorless": 1}, 20)]),
    "Hound": _pokemon("Hound", 100, stage="Stage1", evolves_from="Pup", attacks=[
        _attack("bite", {"Colorless": 1}, 50)]),
    "Slug": _pokemon("Slug", 60, types=("Water",), attacks=[
        _attack("smack", {"Water": 1}, 30)]),
    "Snail": _pokemon("Snail", 120, stage="Stage1", evolves_from="Slug",
                      types=("Water",), attacks=[
                          _attack("crush", {"Water": 4}, 90)]),
    # A Pokemon carrying an Ability as well as an attack, and two Trainers
    # whose worth is set by the registry each test hands the engine.
    "Mole": _pokemon("Mole", 90, retreat=1,
                     attacks=[_attack("dig", {"Colorless": 1}, 30)],
                     abilities=[_power("burrow", "Burrow")]),
    "Gadget": _trainer("Gadget"),
    "Widget": _trainer("Widget"),
    "WaterEnergy": _energy("WaterEnergy", [("Water",)]),
    "FireEnergy": _energy("FireEnergy", [("Fire",)]),
    "PlainEnergy": _energy("PlainEnergy", [("Colorless",)]),
}

DB = engine.CardDB.from_archetypes(ARCHETYPES.values())
GUID = {name: engine.archetype_guid(a) for name, a in ARCHETYPES.items()}


class _NoShuffle(random.Random):
    """A generator that leaves decks in the order the test wrote them.

    Coin flips always come up heads, which nothing in these tests depends on -
    no in-scope attack flips - but pinning it keeps the fixtures readable.
    """

    def shuffle(self, seq, *args, **kwargs):
        return None

    def random(self):
        return 0.0


# --------------------------------------------------------------------------
# fixture helpers
# --------------------------------------------------------------------------

def _deck(hand_names, filler="PlainEnergy"):
    """A 60 card deck whose first seven cards are the hand this test wants."""
    cards = [GUID[n] for n in hand_names]
    return cards + [GUID[filler]] * (60 - len(cards))


def _find(state, player, name):
    for cid in state.players[player].hand:
        if state.card(cid).name == name:
            return cid
    raise AssertionError("%s is not in player %d's hand" % (name, player))


def _place(state, player, active, bench=()):
    state, _ = engine.apply(state, engine.SetupPlaceActive(
        player, _find(state, player, active)))
    for name in bench:
        state, _ = engine.apply(state, engine.SetupPlaceBench(
            player, _find(state, player, name)))
    state, _ = engine.apply(state, engine.SetupDone(player))
    return state


def _start(hand0, hand1, active0, active1, bench0=(), bench1=(),
           first_player=0, rules=engine.DEFAULT_RULES):
    """A game in the main phase with both boards placed exactly as asked.

    `rules` is how a test supplies the effect registries a card needs; the
    default is the stock empty ones, under which no Trainer is playable at
    all and every existing test below reads exactly as it did.
    """
    state, _ = engine.new_game(DB, [_deck(hand0), _deck(hand1)],
                               rng=_NoShuffle(), first_player=first_player,
                               rules=rules)
    boards = {0: (active0, bench0), 1: (active1, bench1)}
    for player in (first_player, 1 - first_player):
        state = _place(state, player, *boards[player])
    return state


def _attach(state, slot, name, count=1):
    """Put Energy on a slot directly.

    The engine allows one attachment per turn, so building a powered-up board
    through legal actions would take a dozen turns of Pass and bury what the
    test is actually about. The result is a state the engine would accept.
    """
    owner = state.owner_of(slot.top)
    for _ in range(count):
        cid = engine._new_card(state, GUID[name], owner)
        slot.energy.append(cid)
    return state


def _to_turn_of(state, player):
    """Pass until it is `player`'s turn and they are allowed to attack."""
    for _ in range(8):
        if (state.to_move == player and not state.pending_promotions
                and engine._can_attack_now(state, player)):
            return state
        actor = engine.players_to_act(state)[0]
        state, _ = engine.apply(state, engine.Pass(actor))
    raise AssertionError("never reached an attacking turn for player %d" % player)


def _snapshot(state):
    """Everything about a state the AI could possibly disturb.

    Includes the generator: consuming the game's randomness inside choose()
    would silently change every future coin flip and shuffle.
    """
    def slots(ps):
        return tuple((s.slot_id, tuple(s.stack), s.damage, tuple(s.energy),
                      tuple(s.tools), tuple(sorted(s.conditions)),
                      s.played_on_turn, tuple(sorted(s.abilities_used)))
                     for s in ps.in_play)

    return (state.phase, state.to_move, state.turn_number, state.winner,
            tuple(state.pending_promotions), state.after_promotions,
            state.rng.getstate(), repr(state.pending), repr(state.modifiers),
            state.stadium.card if state.stadium else None,
            tuple((tuple(p.deck), tuple(p.hand), tuple(p.discard),
                   tuple(p.prizes), slots(p), p.energy_attached_this_turn,
                   p.retreats_this_turn, p.supporters_this_turn,
                   p.stadiums_this_turn, p.setup_done)
                  for p in state.players))


# --------------------------------------------------------------------------
# the forced and the obvious
# --------------------------------------------------------------------------

class ForcedChoiceTests(unittest.TestCase):

    def test_promotes_after_a_knockout_instead_of_stalling(self):
        # Player 1's Active is one jab from death and its bench holds a 30 HP
        # filler and a real attacker - in that order, so that taking the first
        # legal Promote would be the wrong answer.
        state = _start(["Bruiser"], ["Dummy", "Bruiser", "Filler"],
                       "Bruiser", "Dummy", bench1=("Filler", "Bruiser"))
        _attach(state, state.players[0].active, "PlainEnergy")
        state.players[1].active.damage = 30
        state = _to_turn_of(state, 0)

        state, _ = engine.apply(state, engine.Attack(0, "jab"))
        self.assertEqual(engine.players_to_act(state), [1],
                         "the knockout should have left player 1 owing a promotion")

        action = ai.choose(state, 1)
        self.assertIsInstance(action, engine.Promote)
        promoted = state.slot(action.slot)[1]
        self.assertEqual(state.pokemon(promoted).name, "Bruiser",
                         "promoted the filler over the attacker")
        # And it is really promoted, not just proposed.
        state, _ = engine.apply(state, action)
        self.assertEqual(state.pokemon(state.players[1].active).name, "Bruiser")

    def test_takes_the_knockout_with_the_cheapest_attack_that_gets_there(self):
        # Both attacks are lethal; jab costs one Energy, slam costs three.
        state = _start(["Bruiser"], ["Dummy"], "Bruiser", "Dummy")
        _attach(state, state.players[0].active, "PlainEnergy", 3)
        state.players[1].active.damage = 30          # 30 HP left, jab does 30
        state = _to_turn_of(state, 0)
        state.players[0].hand.clear()                # nothing else to do

        action = ai.choose(state, 0)
        self.assertEqual(action, engine.Attack(0, "jab"))

        # ... and the engine agrees it was a knockout.
        after, _ = engine.apply(state, action)
        self.assertIsNone(after.slot(state.players[1].active.slot_id))

    def test_attacks_for_the_most_damage_when_nothing_dies(self):
        state = _start(["Bruiser"], ["Dummy"], "Bruiser", "Dummy")
        _attach(state, state.players[0].active, "PlainEnergy", 3)
        state = _to_turn_of(state, 0)
        state.players[0].hand.clear()

        self.assertEqual(ai.choose(state, 0), engine.Attack(0, "slam"))


# --------------------------------------------------------------------------
# resource decisions
# --------------------------------------------------------------------------

class ResourceTests(unittest.TestCase):

    def test_energy_goes_to_the_pokemon_that_can_spend_it(self):
        # Firebug is Active and needs two Fire; Squirt is benched and needs
        # one Water. The one card in hand is a Water Energy, so the Active -
        # which would normally win the tiebreak - is the wrong answer.
        state = _start(["Firebug", "Squirt"], ["Dummy"],
                       "Firebug", "Dummy", bench0=("Squirt",))
        state = _to_turn_of(state, 0)
        state.players[0].hand.clear()
        water = engine._new_card(state, GUID["WaterEnergy"], 0)
        state.players[0].hand.append(water)

        action = ai.choose(state, 0)
        self.assertIsInstance(action, engine.AttachEnergy)
        self.assertEqual(action.card, water)
        self.assertEqual(action.slot, state.players[0].bench[0].slot_id,
                         "attached Water to a Pokemon whose only attack is Fire")

    def test_a_useless_energy_is_not_attached_at_all(self):
        # Nothing in play can ever spend Water, so the attachment is skipped
        # and the AI gets on with the game rather than wasting the card.
        state = _start(["Firebug"], ["Dummy"], "Firebug", "Dummy")
        state = _to_turn_of(state, 0)
        state.players[0].hand.clear()
        state.players[0].hand.append(engine._new_card(state, GUID["WaterEnergy"], 0))

        action = ai.choose(state, 0)
        self.assertNotIsInstance(action, engine.AttachEnergy)

    def test_evolves_when_the_evolution_is_strictly_better(self):
        state = _start(["Pup", "Hound"], ["Dummy"], "Pup", "Dummy")
        _attach(state, state.players[0].active, "PlainEnergy")
        state = _to_turn_of(state, 0)
        # Leave only the evolution in hand so the attach rung stays quiet.
        state.players[0].hand = [c for c in state.players[0].hand
                                 if state.card(c).name == "Hound"]
        self.assertTrue(state.players[0].hand, "fixture lost its Hound")

        action = ai.choose(state, 0)
        self.assertIsInstance(action, engine.Evolve)
        self.assertEqual(action.slot, state.players[0].active.slot_id)

    def test_does_not_evolve_the_active_out_of_an_attack_it_could_use(self):
        # Slug can attack this turn for 30; Snail needs four Water and could
        # not attack at all. Bigger is not better when it costs the turn.
        state = _start(["Slug", "Snail"], ["Dummy"], "Slug", "Dummy")
        _attach(state, state.players[0].active, "WaterEnergy")
        state = _to_turn_of(state, 0)
        state.players[0].hand = [c for c in state.players[0].hand
                                 if state.card(c).name == "Snail"]

        action = ai.choose(state, 0)
        self.assertEqual(action, engine.Attack(0, "smack"))

    def test_retreats_when_the_active_cannot_act_and_the_bench_can(self):
        # Firebug is Active holding a lone Water Energy - enough to pay its
        # retreat cost, useless for its Fire attack. Squirt is benched and
        # powered up.
        state = _start(["Firebug", "Squirt"], ["Dummy"],
                       "Firebug", "Dummy", bench0=("Squirt",))
        _attach(state, state.players[0].active, "WaterEnergy")
        _attach(state, state.players[0].bench[0], "WaterEnergy")
        state = _to_turn_of(state, 0)
        state.players[0].hand.clear()

        action = ai.choose(state, 0)
        self.assertIsInstance(action, engine.Retreat)
        self.assertEqual(action.slot, state.players[0].bench[0].slot_id)

    def test_stands_and_fights_when_the_bench_is_no_better(self):
        state = _start(["Bruiser", "Filler"], ["Dummy"],
                       "Bruiser", "Dummy", bench0=("Filler",))
        _attach(state, state.players[0].active, "PlainEnergy")
        state = _to_turn_of(state, 0)
        state.players[0].hand.clear()

        self.assertNotIsInstance(ai.choose(state, 0), engine.Retreat)


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

class SetupTests(unittest.TestCase):

    def test_leads_with_an_attacker_rather_than_a_filler(self):
        state, _ = engine.new_game(
            DB, [_deck(["Filler", "Bruiser", "Wall"]), _deck(["Dummy"])],
            rng=_NoShuffle(), first_player=0)

        action = ai.choose(state, 0)
        self.assertIsInstance(action, engine.SetupPlaceActive)
        self.assertEqual(state.card(action.card).name, "Bruiser",
                         "opened on something other than the best attacker")

    def test_benches_a_couple_of_basics_then_stops(self):
        hand = ["Bruiser", "Filler", "Filler", "Filler", "Filler"]
        state, _ = engine.new_game(DB, [_deck(hand), _deck(["Dummy"])],
                                   rng=_NoShuffle(), first_player=0)
        for _ in range(10):
            action = ai.choose(state, 0)
            self.assertIn(action, engine.legal_actions(state, 0))
            if isinstance(action, engine.SetupDone):
                break
            state, _ = engine.apply(state, action)
        else:
            self.fail("the AI never finished setup")

        self.assertIsNotNone(state.players[0].active)
        self.assertEqual(len(state.players[0].bench), ai.SETUP_BENCH_TARGET)


# --------------------------------------------------------------------------
# the contract with the server
# --------------------------------------------------------------------------

class ContractTests(unittest.TestCase):

    def _mid_game_state(self):
        state = _start(["Bruiser", "Filler"], ["Dummy", "Bruiser"],
                       "Bruiser", "Dummy", bench0=("Filler",),
                       bench1=("Bruiser",))
        _attach(state, state.players[0].active, "PlainEnergy", 2)
        return _to_turn_of(state, 0)

    def test_same_state_and_same_seed_give_the_same_action(self):
        state = self._mid_game_state()
        first = ai.choose(state, 0, rng=random.Random(7))
        for _ in range(5):
            self.assertEqual(ai.choose(state, 0, rng=random.Random(7)), first)
        # And with no generator at all it is still reproducible.
        self.assertEqual(ai.choose(state, 0), ai.choose(state, 0))

    def test_it_does_not_touch_the_state_it_is_given(self):
        state = self._mid_game_state()
        before = _snapshot(state)
        for _ in range(5):
            ai.choose(state, 0, rng=random.Random(3))
        self.assertEqual(_snapshot(state), before)

    def test_it_passes_rather_than_raising_when_asked_out_of_turn(self):
        state = self._mid_game_state()
        action = ai.choose(state, 1)          # not player 1's turn at all
        self.assertIsNotNone(action)
        self.assertIsInstance(action, engine.Pass)

    def test_a_broken_heuristic_still_produces_a_legal_action(self):
        # The safety net is the whole point of the module: a bug in the
        # ladder must degrade to a legal move, not reach the server.
        state = self._mid_game_state()
        original = ai._decide
        try:
            ai._decide = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
            action, reason = ai.choose_with_reason(state, 0)
        finally:
            ai._decide = original
        self.assertIn(action, engine.legal_actions(state, 0))
        self.assertIn("fallback", reason)

    def test_an_invented_action_is_discarded(self):
        # A heuristic that returns something legal_actions() never offered is
        # not trusted; the fallback takes over.
        state = self._mid_game_state()
        original = ai._decide
        try:
            ai._decide = lambda *a, **k: (engine.Attack(0, "no-such-attack"),
                                          "nonsense")
            action, reason = ai.choose_with_reason(state, 0)
        finally:
            ai._decide = original
        self.assertIn(action, engine.legal_actions(state, 0))
        self.assertIn("fallback", reason)


# --------------------------------------------------------------------------
# whole games on the real card database
# --------------------------------------------------------------------------

CARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "carddata")

# Colours with a Basic Energy behind them, which is what a deck can be built
# around; Colorless is excluded because no Pokemon costs it exclusively.
DECK_TYPES = ("Fire", "Water", "Grass", "Lightning", "Psychic", "Fighting",
              "Darkness", "Metal")

# --------------------------------------------------------------------------
# Trainers, Abilities and pending choices
# --------------------------------------------------------------------------
#
# The AI cannot read a card: its text is a callable in a registry on Rules.
# So it judges one by playing it on a copy of the board and comparing the two
# positions. These tests build tiny registries whose worth is obvious, and
# check the AI reaches the obvious conclusion.

def _draw_effect(count):
    def effect(state, ctx, changes):
        engine._draw(state, ctx["player"], count, changes)
    return effect


def _nothing(state, ctx, changes):
    return None


def _asking(prompt, options, kind=engine.CHOICE_SLOT, player=None):
    """An effect that asks one question and records the answer."""
    seen = []

    def effect(state, ctx, changes):
        if not ctx["answers"]:
            return engine.Choice(
                player=ctx["player"] if player is None else player,
                prompt=prompt, options=options(state), option_kind=kind)
        seen.append(ctx["answers"][0])
    effect.seen = seen
    return effect


class CardPlayTests(unittest.TestCase):
    """Whether the AI plays a card at all, and which one."""

    def board(self, rules, hand=("Gadget",)):
        state = _start(list(hand) + ["Bruiser", "Bruiser"],
                       ["Bruiser", "Bruiser"], "Bruiser", "Bruiser",
                       rules=rules)
        return _to_turn_of(state, 0)

    def test_a_card_that_helps_is_played(self):
        rules = engine.Rules(trainer_effects={GUID["Gadget"]: _draw_effect(3)})
        state = self.board(rules)
        cid = _find(state, 0, "Gadget")
        self.assertEqual(ai.choose(state, 0), engine.PlayTrainer(0, cid))

    def test_a_card_that_buys_nothing_stays_in_hand(self):
        # Playing it costs the card and gains nothing, so the position after
        # is strictly worse than the position before.
        rules = engine.Rules(trainer_effects={GUID["Gadget"]: _nothing})
        state = self.board(rules)
        self.assertIn(engine.PlayTrainer(0, _find(state, 0, "Gadget")),
                      engine.legal_actions(state, 0))
        self.assertNotIsInstance(ai.choose(state, 0), engine.PlayTrainer)

    def test_the_better_of_two_cards_is_the_one_played(self):
        rules = engine.Rules(trainer_effects={
            GUID["Gadget"]: _draw_effect(1),
            GUID["Widget"]: _draw_effect(4)})
        state = self.board(rules, hand=("Gadget", "Widget"))
        self.assertEqual(ai.choose(state, 0),
                         engine.PlayTrainer(0, _find(state, 0, "Widget")))

    def test_a_free_ability_is_used(self):
        rules = engine.Rules(ability_effects={"burrow": _draw_effect(2)})
        state = _start(["Mole", "Bruiser"], ["Bruiser", "Bruiser"],
                       "Mole", "Bruiser", rules=rules)
        state = _to_turn_of(state, 0)
        slot = state.players[0].active
        self.assertEqual(ai.choose(state, 0),
                         engine.UseAbility(0, slot.slot_id, "burrow"))

    def test_an_ability_is_not_used_twice_in_one_turn(self):
        rules = engine.Rules(ability_effects={"burrow": _draw_effect(2)})
        state = _start(["Mole", "Bruiser"], ["Bruiser", "Bruiser"],
                       "Mole", "Bruiser", rules=rules)
        state = _to_turn_of(state, 0)
        state, _ = engine.apply(state, ai.choose(state, 0))
        self.assertNotIsInstance(ai.choose(state, 0), engine.UseAbility)


class PendingChoiceTests(unittest.TestCase):
    """The AI has to answer a Choice, and answer it legally."""

    def ask(self, effect):
        rules = engine.Rules(trainer_effects={GUID["Gadget"]: effect})
        state = _start(["Gadget", "Bruiser", "Dummy"],
                       ["Bruiser", "Dummy"], "Bruiser", "Bruiser",
                       bench0=("Dummy",), bench1=("Dummy",), rules=rules)
        state = _to_turn_of(state, 0)
        state, _ = engine.apply(
            state, engine.PlayTrainer(0, _find(state, 0, "Gadget")))
        self.assertIsNotNone(state.pending)
        return state

    def test_an_unrecognised_prompt_is_still_answered_legally(self):
        state = self.ask(_asking(
            "somePromptNobodyHasHeardOf",
            lambda s: tuple(x.slot_id for x in s.players[0].in_play)))
        legal = engine.legal_actions(state, 0)
        action = ai.choose(state, 0)
        # An unknown prompt makes every answer score alike; what has to hold
        # is that the AI answers, and that apply() accepts the answer.
        self.assertIn(action, legal)
        state, _ = engine.apply(state, action)
        self.assertIsNone(state.pending)

    def test_a_heal_is_pointed_at_the_most_damaged_pokemon(self):
        def options(state):
            return tuple(x.slot_id for x in state.players[0].in_play)

        rules = engine.Rules(trainer_effects={
            GUID["Gadget"]: _asking("healTarget", options)})
        state = _start(["Gadget", "Bruiser", "Dummy"], ["Bruiser", "Dummy"],
                       "Bruiser", "Bruiser", bench0=("Dummy",), rules=rules)
        state = _to_turn_of(state, 0)
        state.players[0].active.damage = 10
        hurt = state.players[0].bench[0]
        hurt.damage = 20
        state, _ = engine.apply(
            state, engine.PlayTrainer(0, _find(state, 0, "Gadget")))
        self.assertEqual(ai.choose(state, 0), engine.Choose(0, (hurt.slot_id,)))

    def test_a_choice_that_belongs_to_the_opponent_routes_to_them(self):
        def options(state):
            return tuple(x.slot_id for x in state.players[1].bench)

        rules = engine.Rules(trainer_effects={
            GUID["Gadget"]: _asking("switchTo", options, player=1)})
        state = _start(["Gadget", "Bruiser"], ["Bruiser", "Dummy", "Filler"],
                       "Bruiser", "Bruiser", bench1=("Dummy", "Filler"),
                       rules=rules)
        state = _to_turn_of(state, 0)
        state, _ = engine.apply(
            state, engine.PlayTrainer(0, _find(state, 0, "Gadget")))

        self.assertEqual(engine.players_to_act(state), [1])
        self.assertEqual(engine.legal_actions(state, 0), [])
        action = ai.choose(state, 1)
        self.assertIn(action, engine.legal_actions(state, 1))

    def test_choosing_never_disturbs_the_state_it_was_shown(self):
        state = self.ask(_asking(
            "searchDeck", lambda s: tuple(s.players[0].deck[:4]),
            kind=engine.CHOICE_CARD))
        before = _snapshot(state)
        ai.choose(state, 0, rng=random.Random(1))
        self.assertEqual(_snapshot(state), before)


SOAK_GAMES = int(os.environ.get("PTCGO_AI_SOAK_GAMES", "60"))


@unittest.skipUnless(os.path.isdir(CARD_DIR), "carddata/ not present")
class AiNeverMisbehavesTests(unittest.TestCase):
    """Play whole games and check the invariants server.py relies on."""

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)
        cls.decks = [cls.build_deck(cls.db, random.Random(100 + i))
                     for i in range(8)]

    @staticmethod
    def build_deck(db, rng):
        """A single-colour deck of real cards: three lines plus Energy.

        Not a reproduction of a printed theme deck - those are half Trainers,
        which the engine cannot play - but the same shape of decision: a few
        Basics with reachable attacks, Stage 1s to evolve into, and enough
        Energy to power them.
        """
        colour = rng.choice(DECK_TYPES)

        def colours(attack):
            return {t for t, n in attack.cost.items()
                    if n > 0 and t != engine.COLORLESS}

        def playable(card):
            return (card.attacks
                    and all(colours(a) <= {colour} for a in card.attacks)
                    and any(sum(n for n in a.cost.values() if n > 0) <= 3
                            for a in card.attacks))

        energy = sorted(c.guid for c in db if c.is_energy and c.is_basic_energy
                        and c.energy_options == ((colour,),))[0]
        basics = sorted((c for c in db if c.is_basic_pokemon and c.max_hp >= 50
                         and playable(c)), key=lambda c: c.guid)
        deck = []
        for basic in rng.sample(basics, 3):
            deck += [basic.guid] * 4
            evolutions = sorted((c for c in db if c.is_evolution
                                 and c.stage == "Stage1"
                                 and c.evolves_from == basic.name
                                 and playable(c)), key=lambda c: c.guid)
            if evolutions:
                deck += [rng.choice(evolutions).guid] * 3
        deck += [energy] * (60 - len(deck))
        return deck

    def test_whole_games_never_produce_an_illegal_action(self):
        moves = 0
        finished = 0
        for game in range(SOAK_GAMES):
            picker = random.Random(500 + game)
            decks = [list(self.decks[picker.randrange(len(self.decks))]),
                     list(self.decks[picker.randrange(len(self.decks))])]
            state, _ = engine.new_game(self.db, decks, seed=game)
            rng = random.Random(game)

            for step in range(4000):
                if state.over:
                    finished += 1
                    break
                actors = engine.players_to_act(state)
                self.assertTrue(actors,
                                "game %d step %d: nobody may act but the game "
                                "is not over" % (game, step))
                player = actors[0]
                legal = engine.legal_actions(state, player)
                before = _snapshot(state)

                action = ai.choose(state, player, rng=rng)

                self.assertIsNotNone(action, "game %d step %d: chose None"
                                     % (game, step))
                self.assertEqual(action.player, player,
                                 "game %d step %d: acted as the wrong player"
                                 % (game, step))
                self.assertIn(action, legal,
                              "game %d step %d: %r is not legal"
                              % (game, step, action))
                self.assertEqual(_snapshot(state), before,
                                 "game %d step %d: choose() mutated the state"
                                 % (game, step))

                state, _ = engine.apply(state, action)
                moves += 1
            else:
                self.fail("game %d did not finish in 4000 actions" % game)

        self.assertEqual(finished, SOAK_GAMES)
        # A game that ends in three moves is a broken fixture, not a fast win.
        self.assertGreater(moves / SOAK_GAMES, 10)

    @staticmethod
    def build_junk_deck(db, rng):
        """A deck of arbitrary real cards, coherent in no way whatsoever.

        The tidy decks above never show the AI a special Energy that pays for
        nothing, a Stage 2 line, a zero-damage attack or a cost in three
        colours at once. This one is nothing but those: it is a bad deck on
        purpose, because "the AI must not crash" has to hold for card
        combinations nobody designed.
        """
        basics = sorted((c for c in db if c.is_basic_pokemon), key=lambda c: c.guid)
        evolutions = sorted((c for c in db if c.is_evolution), key=lambda c: c.guid)
        energy = sorted((c for c in db if c.is_energy), key=lambda c: c.guid)

        deck = [rng.choice(basics).guid for _ in range(rng.randint(8, 20))]
        deck += [rng.choice(evolutions).guid for _ in range(rng.randint(0, 12))]
        deck += [rng.choice(energy).guid for _ in range(60 - len(deck))]
        return deck

    def test_it_survives_decks_that_make_no_sense(self):
        games = max(20, SOAK_GAMES // 3)
        for game in range(games):
            rng = random.Random(700 + game)
            decks = [self.build_junk_deck(self.db, rng),
                     self.build_junk_deck(self.db, rng)]
            state, _ = engine.new_game(self.db, decks, seed=game)

            for step in range(4000):
                if state.over:
                    break
                player = engine.players_to_act(state)[0]
                legal = engine.legal_actions(state, player)
                action = ai.choose(state, player, rng=rng)
                self.assertIn(action, legal, "junk game %d step %d: %r is not legal"
                              % (game, step, action))
                state, _ = engine.apply(state, action)
            else:
                self.fail("junk game %d did not finish in 4000 actions" % game)

    def test_it_beats_a_player_that_moves_at_random(self):
        """Not a strength target, a sanity check: the heuristics do something.

        A ladder that had no effect would land near 50%. Anything above about
        two thirds means the ordering is real; the measured figure is ~90%.
        """
        wins = 0
        games = max(20, SOAK_GAMES // 3)
        for game in range(games):
            picker = random.Random(900 + game)
            decks = [list(self.decks[picker.randrange(len(self.decks))]),
                     list(self.decks[picker.randrange(len(self.decks))])]
            ai_side = game % 2                       # alternate seats
            state, _ = engine.new_game(self.db, decks, seed=game)
            rng = random.Random(game)

            while not state.over:
                player = engine.players_to_act(state)[0]
                if player == ai_side:
                    action = ai.choose(state, player, rng=rng)
                else:
                    legal = engine.legal_actions(state, player)
                    action = rng.choice(legal) if legal else engine.Pass(player)
                state, _ = engine.apply(state, action)
            if state.winner == ai_side:
                wins += 1
        self.assertGreater(wins / games, 0.66,
                           "the AI is no better than random: %d/%d"
                           % (wins, games))


@unittest.skipUnless(os.path.isdir(CARD_DIR), "carddata/ not present")
class AiWithRealCardTextTests(unittest.TestCase):
    """The same invariants, but with every card's text switched on.

    This is the configuration server.py will actually run, and it is a
    different program from the one above: Trainers are playable, effects
    suspend the game on a Choice, and an attack's damage is computed by a
    hook rather than read off the card. Every one of those is a new way for
    choose() to hand back something apply() will refuse.

    Skipped without the client's LocalizationDB, which lives in the game
    install rather than in this repo - with no text, effects.build_rules()
    returns empty registries and this would silently retest the case above.
    """

    @classmethod
    def setUpClass(cls):
        import effects
        cls.effects = effects
        cls.db = engine.CardDB.from_directory(CARD_DIR)
        cls.loc = effects.load_localization()
        if not cls.loc:
            raise unittest.SkipTest("the client's LocalizationDB is not here")
        cls.rules = effects.build_rules(cls.db, loc=cls.loc)
        cls.deck = cls.build_deck(cls.db, cls.rules)

    @staticmethod
    def build_deck(db, rules):
        """A deck shaped like one somebody would actually build.

        An evolution line, enough Energy to power it, and four copies each of
        the Trainers most decks are held together with - so the Trainer, the
        Choice and the search paths are all on the critical path of the test
        rather than reached by luck.
        """
        oshawott = next(c for c in db.by_name("Oshawott") if c.set_code == "BW1")
        dewott = next(c for c in db.by_name("Dewott") if c.set_code == "BW1")
        water = next(c for c in db if c.name == "WaterEnergy"
                     and c.is_basic_energy)
        deck = [oshawott.guid] * 8 + [dewott.guid] * 4 + [water.guid] * 20

        wanted = ["Potion", "Switch", "UltraBall", "GreatBall", "NestBall",
                  "ProfessorSycamore", "N", "EscapeRope", "PokemonCatcher",
                  "FloatStone"]
        for name in wanted:
            found = [c for c in db.by_name(name)
                     if c.guid in rules.trainer_effects]
            if found:
                deck += [found[0].guid] * 3
        return (deck + [water.guid] * 60)[:60]

    def test_whole_games_never_produce_an_illegal_action(self):
        played = collections.Counter()
        for game in range(max(8, SOAK_GAMES // 6)):
            state, _ = engine.new_game(self.db, [list(self.deck),
                                                 list(self.deck)],
                                       seed=game, rules=self.rules)
            rng = random.Random(game)
            for step in range(4000):
                if state.over:
                    break
                actors = engine.players_to_act(state)
                self.assertTrue(actors, "game %d step %d: nobody may act"
                                % (game, step))
                player = actors[0]
                legal = engine.legal_actions(state, player)
                self.assertTrue(legal, "game %d step %d: nothing is legal (%s)"
                                % (game, step,
                                   state.pending.choice.prompt
                                   if state.pending else state.phase))
                before = _snapshot(state)
                action = ai.choose(state, player, rng=rng)
                self.assertIn(action, legal,
                              "game %d step %d: %r is not legal"
                              % (game, step, action))
                self.assertEqual(_snapshot(state), before,
                                 "game %d step %d: choose() mutated the state"
                                 % (game, step))
                played[type(action).__name__] += 1
                state, _ = engine.apply(state, action)
            else:
                self.fail("game %d did not finish in 4000 actions" % game)
            self.assertTrue(state.over)

        # The test is only worth anything if the new code paths were reached.
        self.assertGreater(played["PlayTrainer"], 0, "no Trainer was ever played")
        self.assertGreater(played["Choose"], 0, "no Choice was ever answered")

    def test_no_card_ever_goes_missing(self):
        for game in range(4):
            state, _ = engine.new_game(self.db, [list(self.deck),
                                                 list(self.deck)],
                                       seed=200 + game, rules=self.rules)
            rng = random.Random(game)
            while not state.over:
                player = engine.players_to_act(state)[0]
                state, _ = engine.apply(state, ai.choose(state, player, rng=rng))
                found = 1 if state.stadium is not None else 0
                for side in state.players:
                    found += (len(side.deck) + len(side.hand) + len(side.discard)
                              + len(side.prizes) + len(side.lost))
                    for slot in side.in_play:
                        found += len(slot.cards)
                self.assertEqual(found, len(state.cards),
                                 "game %d: a card is unaccounted for" % game)


if __name__ == "__main__":
    unittest.main()
