"""
Rules tests for engine.py.

Two kinds of test live here and they are testing different things.

Most of the file uses *synthetic* archetypes built by _pokemon()/_energy()
below. They are written in exactly the shape carddata/*.json uses - the same
attribute ids, the same {"s"/"i"/"a"} value envelopes, the same JSON-string
abilities - but their numbers are chosen so a wrong answer is unmistakable
(a 60 HP Pokemon taking exactly 60, a x2 weakness on a 30 damage attack).
Testing weakness arithmetic against a real card means the test passes as long
as the card is unchanged, which is not the property we care about.

The rest (RealCardDataTests) loads carddata/ and checks the parser against
cards whose printed values are known, because the synthetic fixtures cannot
prove that attribute 200490 really is HP.

Run: python -m unittest discover -s tests
"""

import copy
import itertools
import json
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402


# --------------------------------------------------------------------------
# synthetic card data
# --------------------------------------------------------------------------

_ids = itertools.count(1)


def _s(n, v):
    return {"n": n, "v": {"s": v, "t": 3}}


def _i(n, v):
    return {"n": n, "v": {"i": v, "t": 5}}


def _b(n, v):
    return {"n": n, "v": {"b": v, "t": 4}}


def _a(n, values):
    return {"n": n, "v": {"a": [{"s": x, "t": 3} for x in values], "t": 1}}


def _archetype(attrs):
    """Wrap attributes in the {lo, hi, attrs} envelope the export produces."""
    n = next(_ids)
    return {"lo": n * 2 + 1, "hi": n * 2 + 2, "attrs": attrs}


def _pokemon(name, hp, stage="Basic", types=("Colorless",), attacks=(),
             retreat=0, weakness=None, weakness_op="x", weakness_amount=2,
             resistance=None, resistance_amount=20, evolves_from=None,
             abilities=()):
    attrs = [
        _s(engine.ATTR_CARD_TYPES, "Pokemon"),
        _s(engine.ATTR_CARD_NAME, name),
        _s(engine.ATTR_STAGE, stage),
        _i(engine.ATTR_HP, hp),
        _a(engine.ATTR_POKEMON_TYPES, types),
        _a(engine.ATTR_WEAKNESS_TYPES, [weakness] if weakness else [engine.NO_COLOR]),
        _s(engine.ATTR_WEAKNESS_OPERATOR, weakness_op),
        _s(engine.ATTR_RESISTANCE_TYPE, resistance or engine.NO_COLOR),
    ]
    # A zero int attribute is written with no "i" key at all in the real
    # export, so zero retreat / zero amounts are expressed by omission here
    # too - otherwise the tests would never exercise _int()'s default.
    if retreat:
        attrs.append(_i(engine.ATTR_RETREAT_COST, retreat))
    if weakness:
        attrs.append(_i(engine.ATTR_WEAKNESS_AMOUNT, weakness_amount))
    if resistance:
        attrs.append(_s(engine.ATTR_RESISTANCE_OPERATOR, "-"))
        attrs.append(_i(engine.ATTR_RESISTANCE_AMOUNT, resistance_amount))
    if evolves_from:
        attrs.append(_s(engine.ATTR_EVOLVES_FROM, evolves_from))
    # Attacks and Abilities share ATTR_ABILITIES in the real data - they are
    # told apart only by abilityType - so the fixture builds one array too.
    entries = list(attacks) + list(abilities)
    if entries:
        attrs.append({"n": engine.ATTR_ABILITIES, "t": 1, "v": {"a": [
            {"s": json.dumps(a), "t": 8} for a in entries], "t": 1}})
    return _archetype(attrs)


def _attack(ability_id, cost, damage, title="", game_text="", operator=""):
    return {"cost": dict(cost), "damage": damage, "title": title or ability_id,
            "gameText": game_text, "abilityID": ability_id,
            "amountOperator": operator, "abilityType": "Attack",
            "conditionExceptions": []}


def _energy(name, options, basic=True):
    attrs = [
        _s(engine.ATTR_CARD_TYPES, "Energy"),
        _s(engine.ATTR_CARD_NAME, name),
        {"n": engine.ATTR_ENERGY_PROVIDED,
         "v": {"s": json.dumps({"options": [list(o) for o in options]}), "t": 8}},
    ]
    if basic:
        attrs.append(_b(engine.ATTR_IS_BASIC_ENERGY, True))
    return _archetype(attrs)


def _power(ability_id, title="", game_text="", kind="PokeAbility"):
    """A non-attack Ability, in the shape ATTR_ABILITIES really uses."""
    return {"cost": {}, "damage": 0, "title": title or ability_id,
            "gameText": game_text, "abilityID": ability_id,
            "amountOperator": "", "abilityType": kind,
            "conditionExceptions": []}


def _trainer(name, kind="Item", game_text=""):
    attrs = [
        _s(engine.ATTR_CARD_TYPES, "TrainerCard"),
        _s(engine.ATTR_CARD_NAME, name),
        _s(engine.ATTR_TRAINER_TYPES, kind),
    ]
    if game_text:
        # Stored the same way the real export stores it: a JSON string
        # wrapping a $$$-delimited localization key, never English.
        attrs.append({"n": engine.ATTR_GAME_TEXT,
                      "v": {"s": json.dumps("$$$%s$$$" % game_text), "t": 8}})
    return _archetype(attrs)


# Attack ids are readable strings rather than GUIDs so a failure message says
# which attack; the engine never parses them, it only carries them.
TACKLE = "atk-tackle"
CHOMP = "atk-chomp"
MEGACHOMP = "atk-megachomp"
FLARE = "atk-flare"
SMASH = "atk-smash"
ZAP = "atk-zap"
NOTHING = "atk-nothing"
DIG = "abl-dig"        # a PokeAbility, not an attack

ARCHETYPES = {
    "Pipsqueak": _pokemon("Pipsqueak", 60, retreat=1, weakness="Fighting",
                          attacks=[_attack(TACKLE, {"Colorless": 1}, 10)]),
    "Bigmouth": _pokemon("Bigmouth", 90, stage="Stage1", evolves_from="Pipsqueak",
                         retreat=2, weakness="Fighting",
                         attacks=[_attack(CHOMP, {"Colorless": 2}, 60)]),
    "Hugemouth": _pokemon("Hugemouth", 140, stage="Stage2", evolves_from="Bigmouth",
                          retreat=3,
                          attacks=[_attack(MEGACHOMP, {"Colorless": 3}, 90)]),
    "Emberling": _pokemon("Emberling", 70, types=("Fire",), retreat=1,
                          weakness="Water",
                          attacks=[_attack(FLARE, {"Fire": 1, "Colorless": 1}, 30,
                                           game_text="$$$Flare.GameText$$$")]),
    # Retreat cost 0 by omission, and the only card with a Resistance.
    "Rockjaw": _pokemon("Rockjaw", 80, types=("Fighting",), weakness="Psychic",
                        resistance="Lightning",
                        attacks=[_attack(SMASH, {"Fighting": 2}, 50)]),
    # The one "+" weakness in the whole card pool has this shape.
    "Sparky": _pokemon("Sparky", 50, types=("Lightning",), retreat=1,
                       weakness="Fighting", weakness_op="+", weakness_amount=10,
                       attacks=[_attack(ZAP, {"Lightning": 1}, 40)]),
    "Blobfish": _pokemon("Blobfish", 200, types=("Water",), retreat=4,
                         attacks=[_attack(NOTHING, {}, 0)]),
    "FireEnergy": _energy("FireEnergy", [("Fire",)]),
    "FightingEnergy": _energy("FightingEnergy", [("Fighting",)]),
    "LightningEnergy": _energy("LightningEnergy", [("Lightning",)]),
    "WaterEnergy": _energy("WaterEnergy", [("Water",)]),
    "PsychicEnergy": _energy("PsychicEnergy", [("Psychic",)]),
    "DoubleColorless": _energy("DoubleColorless", [("Colorless", "Colorless")],
                               basic=False),
    "Rainbow": _energy("Rainbow", [("Fire",), ("Water",), ("Lightning",),
                                   ("Fighting",), ("Psychic",)], basic=False),
    # A special Energy whose text is unimplemented provides nothing.
    "Dud": _energy("Dud", [()], basic=False),
    # Big enough to survive every test attack, with both a Weakness and a
    # Resistance, so the two can be told apart in one assertion.
    "Whale": _pokemon("Whale", 200, types=("Water",), retreat=4,
                      weakness="Fighting", resistance="Lightning",
                      attacks=[_attack(NOTHING, {}, 0)]),
    "Potion": _trainer("Potion", game_text="test.potion.gametext"),
    "Cheerful": _trainer("Cheerful", kind="Supporter"),
    "Arena": _trainer("Arena", kind="Stadium"),
    "OtherArena": _trainer("OtherArena", kind="Stadium"),
    "Bandana": _trainer("Bandana", kind="PokemonTool"),
    # A Pokemon carrying both an attack and a non-attack Ability, which is
    # the shape that proves legal_actions() tells the two apart.
    "Burrower": _pokemon("Burrower", 70, retreat=2,
                         attacks=[_attack(TACKLE, {"Colorless": 1}, 10)],
                         abilities=[_power(DIG, "Dig")]),
    "Digger": _pokemon("Digger", 110, stage="Stage1", evolves_from="Burrower",
                       retreat=2,
                       attacks=[_attack(CHOMP, {"Colorless": 2}, 60)],
                       abilities=[_power(DIG, "Dig")]),
}

DB = engine.CardDB.from_archetypes(ARCHETYPES.values())
GUID = {name: engine.archetype_guid(a) for name, a in ARCHETYPES.items()}


# --------------------------------------------------------------------------
# test scaffolding
# --------------------------------------------------------------------------

HEADS, TAILS = 0.0, 0.9   # engine._flip: heads is random() < 0.5


class ScriptedRandom(random.Random):
    """A generator with no surprises: scripted flips and no shuffling.

    shuffle() is a no-op so a test can state the exact deck order it wants,
    and random() walks a script so a test can say "this flip is tails".
    Exhausting the script yields heads for ever, which keeps a test that only
    cares about one flip from having to pad the rest.
    """

    def __init__(self, flips=()):
        super().__init__(0)
        self.script = list(flips)

    def random(self):
        return self.script.pop(0) if self.script else HEADS

    def shuffle(self, seq, *args):
        return None

    def __deepcopy__(self, memo):
        # random.Random.__reduce__ rebuilds only the generator's internal
        # state, which would silently drop the script every time apply()
        # copies the state and turn every flip into heads. A plain
        # random.Random needs no such help - its state does survive - but a
        # subclass with attributes of its own does.
        return ScriptedRandom(self.script)


def make_state(rules=None, flips=(), turn=3, to_move=0, first_player=0,
               turns_taken=(2, 2), prizes=6, deck=8):
    """A mid-game board with nothing on it yet.

    Defaults put us on turn 3 with both players two turns in, which is past
    every first-turn restriction; tests that care about turn 1 say so.
    """
    state = engine.GameState(
        db=DB, rules=rules or engine.DEFAULT_RULES, rng=ScriptedRandom(flips),
        players=[engine.PlayerState(index=0), engine.PlayerState(index=1)])
    state.phase = engine.PHASE_MAIN
    state.first_player = first_player
    state.turn_number = turn
    state.to_move = to_move
    for p in (0, 1):
        ps = state.players[p]
        ps.setup_done = True
        ps.turns_taken = turns_taken[p]
        ps.prizes = [engine._new_card(state, GUID["FireEnergy"], p)
                     for _ in range(prizes)]
        ps.deck = [engine._new_card(state, GUID["FireEnergy"], p)
                   for _ in range(deck)]
    return state


def to_hand(state, player, name):
    cid = engine._new_card(state, GUID[name], player)
    state.players[player].hand.append(cid)
    return cid


def place(state, player, name, where="active", energy=(), damage=0,
          played_on_turn=1, conditions=()):
    cid = engine._new_card(state, GUID[name], player)
    slot = engine._new_slot(state, cid, played_on_turn)
    slot.damage = damage
    slot.conditions = set(conditions)
    for e in energy:
        slot.energy.append(engine._new_card(state, GUID[e], player))
    ps = state.players[player]
    if where == "active":
        ps.active = slot
    else:
        ps.bench.append(slot)
    return slot


def kinds(changes, kind):
    return [c for c in changes if c.kind == kind]


def actions_of(state, player, cls):
    return [a for a in engine.legal_actions(state, player) if isinstance(a, cls)]


# --------------------------------------------------------------------------
# card data parsing
# --------------------------------------------------------------------------

class CardParsingTests(unittest.TestCase):

    def test_attributes_read_off_the_archetype(self):
        card = DB.get(GUID["Bigmouth"])
        self.assertEqual(card.name, "Bigmouth")
        self.assertEqual(card.stage, "Stage1")
        self.assertEqual(card.max_hp, 90)
        self.assertEqual(card.evolves_from, "Pipsqueak")
        self.assertEqual(card.retreat_cost, 2)
        self.assertTrue(card.is_pokemon)
        self.assertTrue(card.is_evolution)
        self.assertFalse(card.is_basic_pokemon)

    def test_zero_valued_int_attribute_is_absent_not_missing(self):
        # Rockjaw has no ATTR_RETREAT_COST at all, exactly like a real
        # free-retreat card; reading that as "unknown" would break retreat.
        self.assertEqual(DB.get(GUID["Rockjaw"]).retreat_cost, 0)

    def test_nocolor_normalises_to_no_weakness_or_resistance(self):
        card = DB.get(GUID["Blobfish"])
        self.assertEqual(card.weakness_types, ())
        self.assertIsNone(card.resistance_type)

    def test_abilities_are_parsed_out_of_their_json_strings(self):
        card = DB.get(GUID["Emberling"])
        self.assertEqual(len(card.attacks), 1)
        attack = card.attacks[0]
        self.assertEqual(attack.ability_id, FLARE)
        self.assertEqual(attack.cost, {"Fire": 1, "Colorless": 1})
        self.assertEqual(attack.damage, 30)
        self.assertIs(card.attack(FLARE), attack)
        self.assertIsNone(card.attack("no-such-attack"))
        # Game text is present but not implemented, and the engine says so.
        self.assertTrue(attack.has_unimplemented_text)

    def test_energy_options_and_symbol_count(self):
        self.assertEqual(DB.get(GUID["FireEnergy"]).energy_options, (("Fire",),))
        self.assertEqual(DB.get(GUID["FireEnergy"]).energy_units, 1)
        self.assertEqual(DB.get(GUID["DoubleColorless"]).energy_units, 2)
        self.assertEqual(DB.get(GUID["Dud"]).energy_units, 0)
        self.assertTrue(DB.get(GUID["Rainbow"]).is_energy)
        self.assertFalse(DB.get(GUID["Rainbow"]).is_pokemon)

    def test_trainers_are_recognised_but_never_playable(self):
        state = make_state()
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        cid = to_hand(state, 0, "Potion")
        self.assertTrue(DB.get(GUID["Potion"]).is_trainer)
        self.assertFalse(any(getattr(a, "card", None) == cid
                             for a in engine.legal_actions(state, 0)))


# --------------------------------------------------------------------------
# setup
# --------------------------------------------------------------------------

class SetupTests(unittest.TestCase):

    def deck(self, *spec):
        """[("Pipsqueak", 2), ("FireEnergy", 28)] -> a flat list of GUIDs."""
        out = []
        for name, count in spec:
            out += [GUID[name]] * count
        return out

    def test_deal_seven_then_six_prizes_after_placement(self):
        deck = self.deck(("Pipsqueak", 4), ("FireEnergy", 26))
        state, _ = engine.new_game(DB, [list(deck), list(deck)],
                                   rng=ScriptedRandom(), first_player=0)
        self.assertEqual(state.phase, engine.PHASE_SETUP)
        for p in (0, 1):
            self.assertEqual(len(state.players[p].hand), 7)
            self.assertEqual(len(state.players[p].deck), 23)
            self.assertEqual(state.players[p].prizes, [])

        for p in (0, 1):
            active = engine.legal_actions(state, p)[0]
            self.assertIsInstance(active, engine.SetupPlaceActive)
            state, _ = engine.apply(state, active)
            state, _ = engine.apply(state, engine.SetupDone(p))

        self.assertEqual(state.phase, engine.PHASE_MAIN)
        for p in (0, 1):
            self.assertEqual(len(state.players[p].prizes), 6)
            self.assertIsNotNone(state.players[p].active)
        self.assertEqual(state.turn_number, 1)
        self.assertEqual(state.to_move, 0)

    def test_setup_only_offers_basics_and_stops_at_a_full_bench(self):
        deck = self.deck(("Pipsqueak", 7), ("Bigmouth", 3), ("FireEnergy", 20))
        state, _ = engine.new_game(DB, [list(deck), list(deck)],
                                   rng=ScriptedRandom(), first_player=0)
        # Hand is seven Pipsqueak; every setup action names a Basic.
        for action in engine.legal_actions(state, 0):
            self.assertIsInstance(action, engine.SetupPlaceActive)

        state, _ = engine.apply(state, engine.SetupPlaceActive(
            0, state.players[0].hand[0]))
        for _ in range(engine.DEFAULT_RULES.bench_size):
            benchings = actions_of(state, 0, engine.SetupPlaceBench)
            self.assertTrue(benchings)
            state, _ = engine.apply(state, benchings[0])

        self.assertEqual(len(state.players[0].bench), 5)
        self.assertEqual(actions_of(state, 0, engine.SetupPlaceBench), [])
        self.assertTrue(state.players[0].hand)  # a 7th Pipsqueak, unplayable
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.SetupPlaceBench(0, state.players[0].hand[0]))

    def test_evolution_card_cannot_be_placed_during_setup(self):
        deck = self.deck(("Pipsqueak", 1), ("Bigmouth", 6), ("FireEnergy", 23))
        state, _ = engine.new_game(DB, [list(deck), list(deck)],
                                   rng=ScriptedRandom(), first_player=0)
        bigmouth = next(c for c in state.players[0].hand
                        if state.card(c).name == "Bigmouth")
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.SetupPlaceActive(0, bigmouth))

    def test_mulligan_redeals_and_pays_the_opponent(self):
        # No Basic in the first seven, one waiting at position 8: with shuffle
        # disabled the redeal is forced to find it.
        deck0 = self.deck(("FireEnergy", 7), ("Pipsqueak", 1), ("FireEnergy", 22))
        deck1 = self.deck(("Pipsqueak", 2), ("FireEnergy", 28))
        state, changes = engine.new_game(DB, [deck0, deck1],
                                         rng=ScriptedRandom(), first_player=0)

        self.assertEqual(state.players[0].mulligans, 1)
        self.assertEqual(state.players[1].mulligans, 0)
        self.assertEqual(len(kinds(changes, engine.CHANGE_MULLIGAN)), 1)
        self.assertTrue(any(state.card(c).is_basic_pokemon
                            for c in state.players[0].hand))
        # The mulliganing player keeps seven; the opponent is paid one extra.
        self.assertEqual(len(state.players[0].hand), 7)
        self.assertEqual(len(state.players[1].hand), 8)

    def test_deck_with_no_basic_is_rejected_rather_than_looping(self):
        deck = self.deck(("FireEnergy", 30))
        with self.assertRaises(ValueError):
            engine.new_game(DB, [deck, list(deck)], rng=ScriptedRandom(),
                            first_player=0)

    def test_first_player_skips_only_the_first_draw(self):
        deck = self.deck(("Pipsqueak", 4), ("FireEnergy", 26))

        def played(rules):
            state, _ = engine.new_game(DB, [list(deck), list(deck)],
                                       rng=ScriptedRandom(), first_player=0,
                                       rules=rules)
            for p in (0, 1):
                state, _ = engine.apply(state, engine.SetupPlaceActive(
                    p, state.players[p].hand[0]))
                state, _ = engine.apply(state, engine.SetupDone(p))
            return state

        skipped = played(engine.DEFAULT_RULES)
        self.assertEqual(len(skipped.players[0].hand), 6)   # 7 - 1 placed

        drew = played(engine.Rules(first_player_draws_on_first_turn=True))
        self.assertEqual(len(drew.players[0].hand), 7)      # 7 - 1 placed + 1

    def test_second_player_always_draws_on_their_first_turn(self):
        deck = [GUID["Pipsqueak"]] * 4 + [GUID["FireEnergy"]] * 26
        state, _ = engine.new_game(DB, [list(deck), list(deck)],
                                   rng=ScriptedRandom(), first_player=0)
        for p in (0, 1):
            state, _ = engine.apply(state, engine.SetupPlaceActive(
                p, state.players[p].hand[0]))
            state, _ = engine.apply(state, engine.SetupDone(p))
        state, _ = engine.apply(state, engine.Pass(0))
        self.assertEqual(state.to_move, 1)
        self.assertEqual(len(state.players[1].hand), 7)     # 7 - 1 placed + 1


# --------------------------------------------------------------------------
# turn structure
# --------------------------------------------------------------------------

class TurnTests(unittest.TestCase):

    def test_one_energy_attachment_per_turn_and_it_resets(self):
        state = make_state()
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        first = to_hand(state, 0, "FireEnergy")
        to_hand(state, 0, "FireEnergy")
        self.assertEqual(len(actions_of(state, 0, engine.AttachEnergy)), 2)

        state, changes = engine.apply(
            state, engine.AttachEnergy(0, first, state.players[0].active.slot_id))
        self.assertEqual(len(kinds(changes, engine.CHANGE_ATTACH)), 1)
        self.assertEqual(actions_of(state, 0, engine.AttachEnergy), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.AttachEnergy(
                0, state.players[0].hand[0], state.players[0].active.slot_id))

        state, _ = engine.apply(state, engine.Pass(0))
        state, _ = engine.apply(state, engine.Pass(1))
        self.assertEqual(state.to_move, 0)
        self.assertEqual(state.players[0].energy_attached_this_turn, 0)
        self.assertTrue(actions_of(state, 0, engine.AttachEnergy))

    def test_energy_may_be_attached_to_a_benched_pokemon(self):
        state = make_state()
        place(state, 0, "Pipsqueak")
        bench = place(state, 0, "Bigmouth", where="bench")
        place(state, 1, "Pipsqueak")
        cid = to_hand(state, 0, "FireEnergy")
        state, _ = engine.apply(state, engine.AttachEnergy(0, cid, bench.slot_id))
        self.assertEqual(state.slot(bench.slot_id)[1].energy, [cid])

    def test_energy_cannot_be_attached_to_the_opponent(self):
        state = make_state()
        place(state, 0, "Pipsqueak")
        theirs = place(state, 1, "Pipsqueak")
        cid = to_hand(state, 0, "FireEnergy")
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.AttachEnergy(0, cid, theirs.slot_id))

    def test_bench_holds_five(self):
        state = make_state()
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        for _ in range(6):
            to_hand(state, 0, "Pipsqueak")
        for _ in range(engine.DEFAULT_RULES.bench_size):
            playable = actions_of(state, 0, engine.PlayBasic)
            self.assertTrue(playable)
            state, _ = engine.apply(state, playable[0])
        self.assertEqual(len(state.players[0].bench), 5)
        self.assertEqual(actions_of(state, 0, engine.PlayBasic), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.PlayBasic(0, state.players[0].hand[0]))

    def test_a_player_cannot_act_out_of_turn(self):
        state = make_state()
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        to_hand(state, 1, "Pipsqueak")
        self.assertEqual(engine.legal_actions(state, 1), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Pass(1))

    def test_apply_does_not_mutate_the_state_it_is_given(self):
        state = make_state()
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        cid = to_hand(state, 0, "FireEnergy")
        before_hand = list(state.players[0].hand)
        before_turn = state.turn_number
        engine.apply(state, engine.AttachEnergy(
            0, cid, state.players[0].active.slot_id))
        engine.apply(state, engine.Pass(0))
        self.assertEqual(state.players[0].hand, before_hand)
        self.assertEqual(state.players[0].active.energy, [])
        self.assertEqual(state.turn_number, before_turn)


# --------------------------------------------------------------------------
# evolution
# --------------------------------------------------------------------------

class EvolutionTests(unittest.TestCase):

    def test_cannot_evolve_a_pokemon_played_this_turn(self):
        state = make_state()
        place(state, 1, "Pipsqueak")
        fresh = place(state, 0, "Pipsqueak", where="bench",
                      played_on_turn=state.players[0].turns_taken)
        place(state, 0, "Rockjaw")
        cid = to_hand(state, 0, "Bigmouth")
        self.assertEqual(actions_of(state, 0, engine.Evolve), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Evolve(0, cid, fresh.slot_id))

    def test_can_evolve_a_pokemon_played_on_an_earlier_turn(self):
        state = make_state()
        place(state, 1, "Pipsqueak")
        slot = place(state, 0, "Pipsqueak",
                     played_on_turn=state.players[0].turns_taken - 1)
        cid = to_hand(state, 0, "Bigmouth")
        self.assertIn(engine.Evolve(0, cid, slot.slot_id),
                      engine.legal_actions(state, 0))
        state, changes = engine.apply(state, engine.Evolve(0, cid, slot.slot_id))
        evolved = state.slot(slot.slot_id)[1]
        self.assertEqual(state.card(evolved.top).name, "Bigmouth")
        self.assertEqual(len(evolved.stack), 2)
        self.assertEqual(len(kinds(changes, engine.CHANGE_EVOLVE)), 1)

    def test_nobody_evolves_on_the_first_turn_of_the_game(self):
        # Setup placements are recorded as played on that player's turn 1, so
        # the "not the turn it was played" rule covers this with no extra check.
        state = make_state(turn=1, to_move=0, turns_taken=(1, 0))
        slot = place(state, 0, "Pipsqueak",
                     played_on_turn=engine.DEFAULT_RULES.setup_play_turn)
        place(state, 1, "Pipsqueak")
        cid = to_hand(state, 0, "Bigmouth")
        self.assertEqual(actions_of(state, 0, engine.Evolve), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Evolve(0, cid, slot.slot_id))

    def test_evolution_must_match_the_named_pre_evolution(self):
        state = make_state()
        place(state, 1, "Pipsqueak")
        wrong = place(state, 0, "Emberling", played_on_turn=1)
        cid = to_hand(state, 0, "Bigmouth")
        self.assertEqual(actions_of(state, 0, engine.Evolve), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Evolve(0, cid, wrong.slot_id))

    def test_stage2_cannot_skip_stage1(self):
        state = make_state()
        place(state, 1, "Pipsqueak")
        basic = place(state, 0, "Pipsqueak", played_on_turn=1)
        cid = to_hand(state, 0, "Hugemouth")
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Evolve(0, cid, basic.slot_id))

    def test_stage2_onto_stage1_is_fine(self):
        state = make_state()
        place(state, 1, "Pipsqueak")
        slot = place(state, 0, "Bigmouth", played_on_turn=1)
        cid = to_hand(state, 0, "Hugemouth")
        state, _ = engine.apply(state, engine.Evolve(0, cid, slot.slot_id))
        self.assertEqual(state.card(state.slot(slot.slot_id)[1].top).name,
                         "Hugemouth")

    def test_evolving_keeps_damage_and_energy_but_clears_conditions(self):
        state = make_state()
        place(state, 1, "Pipsqueak")
        slot = place(state, 0, "Pipsqueak", energy=["FireEnergy"], damage=40,
                     played_on_turn=1, conditions=[engine.ASLEEP, engine.POISONED])
        cid = to_hand(state, 0, "Bigmouth")
        state, changes = engine.apply(state, engine.Evolve(0, cid, slot.slot_id))
        evolved = state.slot(slot.slot_id)[1]
        self.assertEqual(evolved.damage, 40)          # damage counters stay put
        self.assertEqual(len(evolved.energy), 1)
        self.assertEqual(evolved.conditions, set())
        removed = {c.detail["condition"]
                   for c in kinds(changes, engine.CHANGE_CONDITION)
                   if not c.detail["added"]}
        self.assertEqual(removed, {engine.ASLEEP, engine.POISONED})

    def test_evolving_counts_as_played_this_turn(self):
        state = make_state()
        place(state, 1, "Pipsqueak")
        slot = place(state, 0, "Pipsqueak", played_on_turn=1)
        state, _ = engine.apply(state, engine.Evolve(
            0, to_hand(state, 0, "Bigmouth"), slot.slot_id))
        cid = to_hand(state, 0, "Hugemouth")
        self.assertEqual(actions_of(state, 0, engine.Evolve), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Evolve(0, cid, slot.slot_id))


# --------------------------------------------------------------------------
# retreat
# --------------------------------------------------------------------------

class RetreatTests(unittest.TestCase):

    def setUp(self):
        self.state = make_state()
        place(self.state, 1, "Pipsqueak")

    def test_retreat_discards_exactly_the_cost_and_swaps_the_active(self):
        active = place(self.state, 0, "Bigmouth",
                       energy=["FireEnergy", "FireEnergy", "WaterEnergy"])
        bench = place(self.state, 0, "Rockjaw", where="bench")
        payment = tuple(active.energy[:2])

        state, changes = engine.apply(
            self.state, engine.Retreat(0, bench.slot_id, payment))
        ps = state.players[0]
        self.assertEqual(ps.active.slot_id, bench.slot_id)
        self.assertEqual([s.slot_id for s in ps.bench], [active.slot_id])
        self.assertEqual(sorted(ps.discard), sorted(payment))
        self.assertEqual(len(state.slot(active.slot_id)[1].energy), 1)
        self.assertEqual(len(kinds(changes, engine.CHANGE_RETREAT)), 1)

    def test_underpaying_the_retreat_cost_is_refused(self):
        active = place(self.state, 0, "Bigmouth", energy=["FireEnergy", "FireEnergy"])
        bench = place(self.state, 0, "Rockjaw", where="bench")
        with self.assertRaises(engine.IllegalAction):
            engine.apply(self.state,
                         engine.Retreat(0, bench.slot_id, (active.energy[0],)))

    def test_overpaying_the_retreat_cost_is_refused(self):
        # A sloppy client selection must not be allowed to bin the whole board.
        active = place(self.state, 0, "Bigmouth",
                       energy=["FireEnergy", "FireEnergy", "FireEnergy"])
        bench = place(self.state, 0, "Rockjaw", where="bench")
        with self.assertRaises(engine.IllegalAction):
            engine.apply(self.state,
                         engine.Retreat(0, bench.slot_id, tuple(active.energy)))

    def test_retreat_cost_counts_symbols_not_cards(self):
        # One Double Colorless pays a retreat cost of two on its own.
        active = place(self.state, 0, "Bigmouth", energy=["DoubleColorless"])
        bench = place(self.state, 0, "Rockjaw", where="bench")
        payment = tuple(active.energy)
        self.assertIn(engine.Retreat(0, bench.slot_id, payment),
                      engine.legal_actions(self.state, 0))
        state, _ = engine.apply(self.state, engine.Retreat(0, bench.slot_id, payment))
        self.assertEqual(state.players[0].active.slot_id, bench.slot_id)

    def test_energy_with_no_implemented_text_pays_nothing(self):
        active = place(self.state, 0, "Bigmouth", energy=["Dud", "Dud", "Dud"])
        bench = place(self.state, 0, "Rockjaw", where="bench")
        self.assertEqual(actions_of(self.state, 0, engine.Retreat), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(self.state,
                         engine.Retreat(0, bench.slot_id, tuple(active.energy[:2])))

    def test_free_retreat_needs_no_energy_and_refuses_payment(self):
        active = place(self.state, 0, "Rockjaw", energy=["FireEnergy"])
        bench = place(self.state, 0, "Bigmouth", where="bench")
        self.assertIn(engine.Retreat(0, bench.slot_id, ()),
                      engine.legal_actions(self.state, 0))
        with self.assertRaises(engine.IllegalAction):
            engine.apply(self.state,
                         engine.Retreat(0, bench.slot_id, tuple(active.energy)))
        state, _ = engine.apply(self.state, engine.Retreat(0, bench.slot_id, ()))
        self.assertEqual(state.players[0].discard, [])

    def test_retreat_is_once_per_turn(self):
        place(self.state, 0, "Rockjaw")
        bench = place(self.state, 0, "Bigmouth", where="bench")
        state, _ = engine.apply(self.state, engine.Retreat(0, bench.slot_id, ()))
        self.assertEqual(actions_of(state, 0, engine.Retreat), [])
        back = state.players[0].bench[0].slot_id
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Retreat(0, back, ()))

    def test_asleep_and_paralyzed_cannot_retreat_but_confused_can(self):
        for condition, allowed in ((engine.ASLEEP, False),
                                   (engine.PARALYZED, False),
                                   (engine.CONFUSED, True)):
            state = make_state()
            place(state, 1, "Pipsqueak")
            place(state, 0, "Rockjaw", conditions=[condition])
            bench = place(state, 0, "Bigmouth", where="bench")
            action = engine.Retreat(0, bench.slot_id, ())
            if allowed:
                self.assertIn(action, engine.legal_actions(state, 0), condition)
                engine.apply(state, action)
            else:
                self.assertEqual(actions_of(state, 0, engine.Retreat), [], condition)
                with self.assertRaises(engine.IllegalAction):
                    engine.apply(state, action)

    def test_retreating_clears_special_conditions(self):
        place(self.state, 0, "Rockjaw",
              conditions=[engine.CONFUSED, engine.POISONED])
        bench = place(self.state, 0, "Bigmouth", where="bench")
        state, _ = engine.apply(self.state, engine.Retreat(0, bench.slot_id, ()))
        self.assertEqual(state.players[0].bench[0].conditions, set())


# --------------------------------------------------------------------------
# energy costs
# --------------------------------------------------------------------------

class EnergyCostTests(unittest.TestCase):
    """can_pay_cost() in isolation - the piece most likely to be subtly wrong."""

    def options(self, *names):
        return [list(DB.get(GUID[n]).energy_options) or [()] for n in names]

    def test_colorless_takes_anything(self):
        self.assertTrue(engine.can_pay_cost(
            self.options("FireEnergy", "WaterEnergy"), {"Colorless": 2}))

    def test_colorless_energy_cannot_pay_a_coloured_cost(self):
        self.assertFalse(engine.can_pay_cost(
            self.options("DoubleColorless"), {"Fire": 1, "Colorless": 1}))

    def test_double_colorless_counts_as_two_symbols(self):
        self.assertTrue(engine.can_pay_cost(
            self.options("DoubleColorless"), {"Colorless": 2}))
        self.assertFalse(engine.can_pay_cost(
            self.options("DoubleColorless"), {"Colorless": 3}))

    def test_coloured_first_then_colorless_from_the_remainder(self):
        self.assertTrue(engine.can_pay_cost(
            self.options("FireEnergy", "DoubleColorless"),
            {"Fire": 1, "Colorless": 2}))
        self.assertFalse(engine.can_pay_cost(
            self.options("FireEnergy", "DoubleColorless"),
            {"Fire": 2, "Colorless": 1}))

    def test_a_coloured_energy_may_be_spent_as_colorless(self):
        self.assertTrue(engine.can_pay_cost(
            self.options("FireEnergy", "FireEnergy"),
            {"Fire": 1, "Colorless": 1}))

    def test_multi_option_energy_picks_the_option_that_works(self):
        # Two Rainbows paying one Fire and one Water means each has to choose
        # differently; a greedy first-option match would fail this.
        self.assertTrue(engine.can_pay_cost(
            self.options("Rainbow", "Rainbow"), {"Fire": 1, "Water": 1}))
        self.assertFalse(engine.can_pay_cost(
            self.options("Rainbow"), {"Fire": 1, "Water": 1}))

    def test_surplus_energy_is_allowed(self):
        self.assertTrue(engine.can_pay_cost(
            self.options("FireEnergy", "FireEnergy", "WaterEnergy"), {"Fire": 1}))

    def test_free_attacks_need_nothing(self):
        self.assertTrue(engine.can_pay_cost([], {}))

    def test_attack_is_offered_only_when_it_can_be_paid_for(self):
        state = make_state()
        place(state, 1, "Pipsqueak")
        slot = place(state, 0, "Emberling", energy=["WaterEnergy", "WaterEnergy"])
        self.assertEqual(actions_of(state, 0, engine.Attack), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Attack(0, FLARE))

        slot.energy.append(engine._new_card(state, GUID["FireEnergy"], 0))
        self.assertEqual([a.ability_id for a in actions_of(state, 0, engine.Attack)],
                         [FLARE])


# --------------------------------------------------------------------------
# damage arithmetic
# --------------------------------------------------------------------------

class DamageTests(unittest.TestCase):

    def card(self, name):
        return DB.get(GUID[name])

    def test_weakness_multiplies(self):
        # Rockjaw is Fighting; Pipsqueak is Fighting-weak with operator "x"
        # and amount 2, which is the shape 7,243 of the 7,244 real cards use.
        self.assertEqual(engine.damage_after_weakness(
            self.card("Rockjaw"), self.card("Pipsqueak"), 30), 60)

    def test_weakness_adds_when_the_operator_is_plus(self):
        self.assertEqual(engine.damage_after_weakness(
            self.card("Rockjaw"), self.card("Sparky"), 40), 50)

    def test_resistance_subtracts(self):
        self.assertEqual(engine.damage_after_weakness(
            self.card("Sparky"), self.card("Rockjaw"), 50), 30)

    def test_resistance_floors_at_zero_rather_than_healing(self):
        self.assertEqual(engine.damage_after_weakness(
            self.card("Sparky"), self.card("Rockjaw"), 10), 0)

    def test_no_weakness_and_no_resistance_leaves_damage_alone(self):
        self.assertEqual(engine.damage_after_weakness(
            self.card("Emberling"), self.card("Blobfish"), 30), 30)

    def test_a_zero_damage_attack_stays_zero_through_weakness(self):
        self.assertEqual(engine.damage_after_weakness(
            self.card("Rockjaw"), self.card("Pipsqueak"), 0), 0)

    def test_weakness_applies_end_to_end_through_an_attack(self):
        state = make_state()
        place(state, 0, "Rockjaw", energy=["FightingEnergy", "FightingEnergy"])
        target = place(state, 1, "Pipsqueak")
        place(state, 1, "Blobfish", where="bench")   # so the KO is survivable
        state, changes = engine.apply(state, engine.Attack(0, SMASH))
        # SMASH is 50, Pipsqueak is Fighting-weak x2 and only has 60 HP.
        damage = kinds(changes, engine.CHANGE_DAMAGE)[0]
        self.assertEqual(damage.amount, 100)
        self.assertTrue(damage.detail["weakness"])
        self.assertEqual(len(kinds(changes, engine.CHANGE_KNOCKOUT)), 1)

    def test_resistance_applies_end_to_end_through_an_attack(self):
        state = make_state()
        place(state, 0, "Sparky", energy=["LightningEnergy"])
        place(state, 1, "Rockjaw")
        state, changes = engine.apply(state, engine.Attack(0, ZAP))
        damage = kinds(changes, engine.CHANGE_DAMAGE)[0]
        self.assertEqual(damage.amount, 20)          # 40 printed, -20 Resistance
        self.assertTrue(damage.detail["resistance"])
        self.assertEqual(state.players[1].active.damage, 20)

    def test_attack_with_unimplemented_text_still_deals_its_printed_damage(self):
        state = make_state()
        place(state, 0, "Emberling", energy=["FireEnergy", "FireEnergy"])
        place(state, 1, "Blobfish")
        state, changes = engine.apply(state, engine.Attack(0, FLARE))
        self.assertEqual(kinds(changes, engine.CHANGE_DAMAGE)[0].amount, 30)


# --------------------------------------------------------------------------
# knockouts, prizes and promotion
# --------------------------------------------------------------------------

class KnockoutTests(unittest.TestCase):

    def test_knockout_discards_the_whole_stack_and_takes_one_prize(self):
        state = make_state()
        place(state, 0, "Bigmouth", energy=["FireEnergy", "FireEnergy"])
        victim = place(state, 1, "Pipsqueak", energy=["WaterEnergy"], damage=10)
        place(state, 1, "Rockjaw", where="bench")
        # Evolve the victim so the discard has to carry two Pokemon cards.
        victim.stack.insert(0, engine._new_card(state, GUID["Pipsqueak"], 1))

        prizes_before = len(state.players[0].prizes)
        state, changes = engine.apply(state, engine.Attack(0, CHOMP))

        self.assertEqual(len(kinds(changes, engine.CHANGE_KNOCKOUT)), 1)
        self.assertIsNone(state.players[1].active)
        self.assertEqual(len(state.players[1].discard), 3)   # 2 Pokemon + 1 Energy
        self.assertEqual(len(state.players[0].prizes), prizes_before - 1)
        self.assertEqual(len(state.players[0].hand), 1)      # the prize
        self.assertEqual(len(kinds(changes, engine.CHANGE_PRIZE)), 1)

    def test_the_knocked_out_player_must_promote_before_anything_else(self):
        state = make_state()
        place(state, 0, "Bigmouth", energy=["FireEnergy", "FireEnergy"])
        place(state, 1, "Pipsqueak")
        bench = place(state, 1, "Rockjaw", where="bench")
        state, _ = engine.apply(state, engine.Attack(0, CHOMP))

        self.assertEqual(engine.players_to_act(state), [1])
        self.assertEqual(engine.legal_actions(state, 0), [])
        self.assertEqual(engine.legal_actions(state, 1), [engine.Promote(1, bench.slot_id)])

        state, changes = engine.apply(state, engine.Promote(1, bench.slot_id))
        self.assertEqual(state.players[1].active.slot_id, bench.slot_id)
        self.assertEqual(state.players[1].bench, [])
        # Attacking ends the turn, so promotion hands play straight to player 1.
        self.assertEqual(state.to_move, 1)
        self.assertEqual(len(kinds(changes, engine.CHANGE_TURN_START)), 1)

    def test_promoting_a_pokemon_that_is_not_yours_or_not_benched_is_refused(self):
        state = make_state()
        place(state, 0, "Bigmouth", energy=["FireEnergy", "FireEnergy"])
        place(state, 1, "Pipsqueak")
        mine = place(state, 0, "Rockjaw", where="bench")
        place(state, 1, "Rockjaw", where="bench")
        state, _ = engine.apply(state, engine.Attack(0, CHOMP))
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Promote(1, mine.slot_id))
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Promote(0, mine.slot_id))

    def test_a_pokemon_below_its_hp_is_not_knocked_out(self):
        state = make_state()
        place(state, 0, "Pipsqueak", energy=["FireEnergy"])
        place(state, 1, "Blobfish")
        state, changes = engine.apply(state, engine.Attack(0, TACKLE))
        self.assertEqual(kinds(changes, engine.CHANGE_KNOCKOUT), [])
        self.assertEqual(state.players[1].active.damage, 10)


# --------------------------------------------------------------------------
# win conditions
# --------------------------------------------------------------------------

class WinConditionTests(unittest.TestCase):

    def test_taking_the_last_prize_wins(self):
        state = make_state(prizes=1)
        place(state, 0, "Bigmouth", energy=["FireEnergy", "FireEnergy"])
        place(state, 1, "Pipsqueak")
        place(state, 1, "Rockjaw", where="bench")   # they could promote, but lose first
        state, changes = engine.apply(state, engine.Attack(0, CHOMP))
        self.assertTrue(state.over)
        self.assertEqual(state.winner, 0)
        over = kinds(changes, engine.CHANGE_GAME_OVER)[0]
        self.assertEqual(over.detail["reasons"][0], engine.WIN_PRIZES)
        self.assertEqual(engine.legal_actions(state, 0), [])
        self.assertEqual(engine.legal_actions(state, 1), [])

    def test_running_out_of_pokemon_in_play_loses(self):
        state = make_state()
        place(state, 0, "Bigmouth", energy=["FireEnergy", "FireEnergy"])
        place(state, 1, "Pipsqueak")               # and nothing on the bench
        state, changes = engine.apply(state, engine.Attack(0, CHOMP))
        self.assertTrue(state.over)
        self.assertEqual(state.winner, 0)
        self.assertEqual(kinds(changes, engine.CHANGE_GAME_OVER)[0]
                         .detail["reasons"][0], engine.WIN_NO_POKEMON)
        self.assertEqual(state.pending_promotions, [])

    def test_being_unable_to_draw_loses(self):
        state = make_state(deck=0)
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        state, changes = engine.apply(state, engine.Pass(0))
        # Player 1 begins their turn, is asked to draw, and has no deck.
        self.assertTrue(state.over)
        self.assertEqual(state.winner, 0)
        self.assertEqual(kinds(changes, engine.CHANGE_GAME_OVER)[0]
                         .detail["reasons"][0], engine.WIN_DECK_OUT)

    def test_an_empty_deck_only_loses_at_the_moment_of_drawing(self):
        state = make_state(deck=0)
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        self.assertFalse(state.over)
        self.assertTrue(engine.legal_actions(state, 0))

    def test_simultaneous_knockouts_with_no_bench_are_a_tie(self):
        # Confusion tails knocks the attacker out; the defender is already
        # dead from a previous turn's damage, and neither has a bench.
        state = make_state(flips=[TAILS])
        attacker = place(state, 0, "Sparky", energy=["LightningEnergy"],
                         damage=20, conditions=[engine.CONFUSED])
        defender = place(state, 1, "Pipsqueak")
        defender.damage = 60                        # already at its 60 HP
        state, changes = engine.apply(state, engine.Attack(0, ZAP))
        self.assertTrue(state.over)
        self.assertEqual(state.winner, engine.WINNER_TIE)
        self.assertEqual(len(kinds(changes, engine.CHANGE_KNOCKOUT)), 2)


# --------------------------------------------------------------------------
# special conditions
# --------------------------------------------------------------------------

class SpecialConditionTests(unittest.TestCase):

    def between_turns(self, conditions, flips=(), player=0, damage=0):
        """Run one Pass and return the state plus that player's active slot."""
        state = make_state(flips=flips)
        slot = place(state, player, "Blobfish", conditions=conditions, damage=damage)
        place(state, 1 - player, "Blobfish")
        state, changes = engine.apply(state, engine.Pass(0))
        return state, state.slot(slot.slot_id)[1], changes

    def test_poison_damages_between_turns(self):
        _, slot, _ = self.between_turns([engine.POISONED])
        self.assertEqual(slot.damage, engine.DEFAULT_RULES.poison_damage)
        self.assertIn(engine.POISONED, slot.conditions)   # poison does not wear off

    def test_burn_damages_and_a_heads_removes_it(self):
        _, slot, _ = self.between_turns([engine.BURNED], flips=[HEADS])
        self.assertEqual(slot.damage, engine.DEFAULT_RULES.burn_damage)
        self.assertNotIn(engine.BURNED, slot.conditions)

    def test_burn_tails_keeps_burning(self):
        _, slot, _ = self.between_turns([engine.BURNED], flips=[TAILS])
        self.assertEqual(slot.damage, engine.DEFAULT_RULES.burn_damage)
        self.assertIn(engine.BURNED, slot.conditions)

    def test_sleep_wakes_on_heads_and_persists_on_tails(self):
        _, awake, _ = self.between_turns([engine.ASLEEP], flips=[HEADS])
        self.assertNotIn(engine.ASLEEP, awake.conditions)
        _, still, _ = self.between_turns([engine.ASLEEP], flips=[TAILS])
        self.assertIn(engine.ASLEEP, still.conditions)
        self.assertEqual(still.damage, 0)

    def test_poison_and_burn_stack(self):
        _, slot, _ = self.between_turns([engine.POISONED, engine.BURNED],
                                        flips=[TAILS])
        self.assertEqual(slot.damage, engine.DEFAULT_RULES.poison_damage
                         + engine.DEFAULT_RULES.burn_damage)

    def test_poison_can_knock_a_pokemon_out_between_turns(self):
        state = make_state()
        place(state, 0, "Pipsqueak")
        victim = place(state, 1, "Pipsqueak", damage=50,
                       conditions=[engine.POISONED])
        place(state, 1, "Rockjaw", where="bench")
        prizes_before = len(state.players[0].prizes)
        state, changes = engine.apply(state, engine.Pass(0))
        self.assertEqual(len(kinds(changes, engine.CHANGE_KNOCKOUT)), 1)
        # The prize goes to the opponent of the owner, not to "whoever attacked".
        self.assertEqual(len(state.players[0].prizes), prizes_before - 1)
        self.assertEqual(engine.players_to_act(state), [1])
        self.assertIsNone(state.slot(victim.slot_id))

        state, _ = engine.apply(state, engine.legal_actions(state, 1)[0])
        self.assertEqual(state.to_move, 1)          # the interrupted turn resumes

    def test_paralysis_is_cured_at_the_end_of_the_owners_own_turn(self):
        # Paralysed on player 0's turn: it must survive the whole of player 1's
        # turn and only wear off when player 1's turn ends.
        state = make_state()
        place(state, 0, "Blobfish")
        slot = place(state, 1, "Blobfish", conditions=[engine.PARALYZED])

        state, _ = engine.apply(state, engine.Pass(0))
        self.assertIn(engine.PARALYZED, state.slot(slot.slot_id)[1].conditions)
        self.assertEqual(actions_of(state, 1, engine.Attack), [])

        state, _ = engine.apply(state, engine.Pass(1))
        self.assertNotIn(engine.PARALYZED, state.slot(slot.slot_id)[1].conditions)

    def test_asleep_and_paralyzed_cannot_attack(self):
        for condition in (engine.ASLEEP, engine.PARALYZED):
            state = make_state()
            place(state, 0, "Sparky", energy=["LightningEnergy"],
                  conditions=[condition])
            place(state, 1, "Blobfish")
            self.assertEqual(actions_of(state, 0, engine.Attack), [], condition)
            with self.assertRaises(engine.IllegalAction):
                engine.apply(state, engine.Attack(0, ZAP))

    def test_confusion_tails_hurts_the_attacker_and_does_nothing_else(self):
        state = make_state(flips=[TAILS])
        attacker = place(state, 0, "Sparky", energy=["LightningEnergy"],
                         conditions=[engine.CONFUSED])
        defender = place(state, 1, "Blobfish")
        state, changes = engine.apply(state, engine.Attack(0, ZAP))
        self.assertEqual(state.slot(attacker.slot_id)[1].damage,
                         engine.DEFAULT_RULES.confusion_self_damage)
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 0)
        self.assertEqual(state.to_move, 1)          # the turn is spent regardless

    def test_confusion_heads_attacks_normally(self):
        state = make_state(flips=[HEADS])
        attacker = place(state, 0, "Sparky", energy=["LightningEnergy"],
                         conditions=[engine.CONFUSED])
        defender = place(state, 1, "Blobfish")
        state, _ = engine.apply(state, engine.Attack(0, ZAP))
        self.assertEqual(state.slot(attacker.slot_id)[1].damage, 0)
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 40)

    def test_sleep_paralysis_and_confusion_replace_each_other(self):
        state = make_state()
        slot = place(state, 0, "Blobfish", conditions=[engine.ASLEEP,
                                                       engine.POISONED])
        changes = []
        engine._add_condition(state, slot, engine.PARALYZED, changes)
        self.assertEqual(slot.conditions, {engine.PARALYZED, engine.POISONED})


# --------------------------------------------------------------------------
# extension seam
# --------------------------------------------------------------------------

class EffectHookTests(unittest.TestCase):

    def test_an_attack_effect_hook_runs_after_damage(self):
        seen = {}

        def sleep_the_defender(state, ctx, changes):
            seen.update(ctx)
            engine._add_condition(state, ctx["defender"], engine.ASLEEP, changes)

        # Tails on the between-turns sleep flip, or the checkup that follows
        # the attack would wake the defender straight back up.
        rules = engine.Rules(attack_effects={ZAP: sleep_the_defender})
        state = make_state(rules=rules, flips=[TAILS])
        place(state, 0, "Sparky", energy=["LightningEnergy"])
        defender = place(state, 1, "Blobfish")
        state, _ = engine.apply(state, engine.Attack(0, ZAP))
        self.assertEqual(seen["damage"], 40)
        self.assertIn(engine.ASLEEP, state.slot(defender.slot_id)[1].conditions)


# --------------------------------------------------------------------------
# whole games
# --------------------------------------------------------------------------

class RandomGameTests(unittest.TestCase):
    """Play games out at random and assert the things that must never break.

    Rules bugs that only show up in combination - a card in two zones at once,
    a board with no Active and no promotion owed, a game that cannot end - do
    not have a unit test that names them, so this is the net that catches them.
    """

    DECK = ([GUID["Pipsqueak"]] * 8 + [GUID["Bigmouth"]] * 4
            + [GUID["Rockjaw"]] * 4 + [GUID["Sparky"]] * 4
            + [GUID["FightingEnergy"]] * 20 + [GUID["LightningEnergy"]] * 20)

    def check_invariants(self, state):
        seen = {}
        for p in (0, 1):
            ps = state.players[p]
            for zone in (ps.deck, ps.hand, ps.discard, ps.lost, ps.prizes):
                for cid in zone:
                    self.assertNotIn(cid, seen)
                    seen[cid] = p
                    self.assertEqual(state.cards[cid].owner, p)
            for slot in ps.in_play:
                self.assertTrue(slot.stack)
                self.assertGreaterEqual(slot.damage, 0)
                for cid in slot.cards:
                    self.assertNotIn(cid, seen)
                    seen[cid] = p
                    self.assertEqual(state.cards[cid].owner, p)
            self.assertLessEqual(len(ps.bench), state.rules.bench_size)
            if (state.phase == engine.PHASE_MAIN and not state.over
                    and not state.pending_promotions):
                # Either you have an Active, or you had no bench to promote
                # from - in which case the game should already be over.
                self.assertTrue(ps.active is not None or not ps.bench)
        self.assertEqual(len(seen), len(state.cards))

    def test_random_games_finish_without_breaking_an_invariant(self):
        finished = 0
        for seed in range(25):
            state, _ = engine.new_game(DB, [list(self.DECK), list(self.DECK)],
                                       seed=seed)
            rng = random.Random(seed)
            for _ in range(2000):
                if state.over:
                    break
                actors = engine.players_to_act(state)
                self.assertTrue(actors)
                player = actors[0]
                actions = engine.legal_actions(state, player)
                self.assertTrue(actions, "no legal action in phase %s" % state.phase)
                # Prefer doing something over passing, or games never end.
                doing = [a for a in actions if not isinstance(a, engine.Pass)]
                action = rng.choice(doing if doing and rng.random() < 0.9 else actions)
                state, changes = engine.apply(state, action)
                self.assertTrue(changes or isinstance(action, engine.SetupDone))
                self.check_invariants(state)
            self.assertTrue(state.over, "game %d did not finish" % seed)
            self.assertIn(state.winner, (0, 1, engine.WINNER_TIE))
            finished += 1
        self.assertEqual(finished, 25)

    def test_the_same_seed_replays_identically(self):
        def play(seed):
            state, _ = engine.new_game(DB, [list(self.DECK), list(self.DECK)],
                                       seed=seed)
            rng = random.Random(99)
            log = []
            for _ in range(400):
                if state.over:
                    break
                player = engine.players_to_act(state)[0]
                actions = engine.legal_actions(state, player)
                action = rng.choice(actions)
                state, changes = engine.apply(state, action)
                log.append((action, tuple(changes)))
            return log

        self.assertEqual(play(7), play(7))
        self.assertNotEqual(play(7), play(8))


# --------------------------------------------------------------------------
# Trainers: the structural rules
# --------------------------------------------------------------------------
#
# These test the ENGINE, not any card, so every effect below is a stub that
# records that it ran. What is under test is "one Supporter a turn", "a
# Stadium replaces the Stadium in play", "one Tool per Pokemon" - rules that
# are the same for every card and have to hold whatever the card does.


def _stub(record, name, result=None):
    """An effect that notes it ran and optionally returns a Choice."""
    def effect(state, ctx, changes):
        record.append((name, ctx["player"], tuple(ctx["answers"])))
        return result(state, ctx, changes) if callable(result) else result
    return effect


def with_trainers(**by_name):
    """A Rules whose trainer_effects are the given stubs, keyed by GUID."""
    return engine.Rules(trainer_effects={GUID[n]: e for n, e in by_name.items()})


class TrainerStructureTests(unittest.TestCase):

    def setUp(self):
        self.log = []

    def board(self, rules):
        state = make_state(rules=rules)
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        return state

    def test_an_unimplemented_trainer_is_never_offered_or_accepted(self):
        # DEFAULT_RULES has an empty registry, so every Trainer is inert.
        state = self.board(engine.DEFAULT_RULES)
        cid = to_hand(state, 0, "Potion")
        self.assertEqual(actions_of(state, 0, engine.PlayTrainer), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.PlayTrainer(0, cid))

    def test_an_item_resolves_and_goes_to_the_discard(self):
        state = self.board(with_trainers(Potion=_stub(self.log, "potion")))
        cid = to_hand(state, 0, "Potion")
        self.assertIn(engine.PlayTrainer(0, cid), engine.legal_actions(state, 0))
        state, changes = engine.apply(state, engine.PlayTrainer(0, cid))
        self.assertEqual(self.log, [("potion", 0, ())])
        self.assertEqual(state.players[0].discard, [cid])
        self.assertNotIn(cid, state.players[0].hand)
        self.assertEqual(len(kinds(changes, engine.CHANGE_PLAY)), 1)

    def test_items_are_unlimited_but_supporters_are_once_a_turn(self):
        rules = with_trainers(Potion=_stub(self.log, "item"),
                              Cheerful=_stub(self.log, "supporter"))
        state = self.board(rules)
        for _ in range(3):
            state, _ = engine.apply(state, engine.PlayTrainer(
                0, to_hand(state, 0, "Potion")))
        self.assertEqual(len(self.log), 3)

        first = to_hand(state, 0, "Cheerful")
        second = to_hand(state, 0, "Cheerful")
        state, _ = engine.apply(state, engine.PlayTrainer(0, first))
        self.assertEqual(state.players[0].supporters_this_turn, 1)
        self.assertNotIn(engine.PlayTrainer(0, second),
                         engine.legal_actions(state, 0))
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.PlayTrainer(0, second))

        # ... and the allowance comes back next turn.
        state, _ = engine.apply(state, engine.Pass(0))
        state, _ = engine.apply(state, engine.Pass(1))
        self.assertEqual(state.players[0].supporters_this_turn, 0)
        self.assertIn(engine.PlayTrainer(0, second),
                      engine.legal_actions(state, 0))

    def test_a_trainer_cannot_be_played_out_of_the_main_phase(self):
        rules = with_trainers(Potion=_stub(self.log, "item"))
        state = engine.GameState(db=DB, rules=rules, rng=ScriptedRandom(),
                                 players=[engine.PlayerState(index=0),
                                          engine.PlayerState(index=1)])
        cid = to_hand(state, 0, "Potion")
        self.assertEqual(state.phase, engine.PHASE_SETUP)
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.PlayTrainer(0, cid))

    def test_a_playable_guard_hides_a_card_that_would_do_nothing(self):
        effect = _stub(self.log, "item")
        effect.playable = lambda state, player: state.players[player].hand[1:] != []
        state = self.board(with_trainers(Potion=effect))
        only = to_hand(state, 0, "Potion")
        self.assertEqual(actions_of(state, 0, engine.PlayTrainer), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.PlayTrainer(0, only))
        to_hand(state, 0, "FireEnergy")
        self.assertIn(engine.PlayTrainer(0, only), engine.legal_actions(state, 0))


class StadiumTests(unittest.TestCase):

    def setUp(self):
        self.log = []
        self.rules = with_trainers(Arena=_stub(self.log, "arena"),
                                   OtherArena=_stub(self.log, "other"))
        self.state = make_state(rules=self.rules)
        place(self.state, 0, "Pipsqueak")
        place(self.state, 1, "Pipsqueak")

    def test_a_stadium_stays_in_play_and_belongs_to_neither_board(self):
        cid = to_hand(self.state, 0, "Arena")
        state, changes = engine.apply(self.state, engine.PlayTrainer(0, cid))
        self.assertIsNotNone(state.stadium)
        self.assertEqual(state.stadium.card, cid)
        self.assertEqual(state.stadium.owner, 0)
        self.assertNotIn(cid, state.players[0].discard)
        moved = kinds(changes, engine.CHANGE_MOVE)[0]
        self.assertEqual(moved.to_zone, engine.ZONE_STADIUM)
        self.assertEqual(len(kinds(changes, engine.CHANGE_STADIUM)), 1)

    def test_a_new_stadium_discards_the_old_one_to_its_own_owner(self):
        first = to_hand(self.state, 0, "Arena")
        state, _ = engine.apply(self.state, engine.PlayTrainer(0, first))
        state, _ = engine.apply(state, engine.Pass(0))
        second = to_hand(state, 1, "OtherArena")
        state, changes = engine.apply(state, engine.PlayTrainer(1, second))

        self.assertEqual(state.stadium.card, second)
        self.assertEqual(state.stadium.owner, 1)
        # The replaced Stadium goes to the pile of whoever played it.
        self.assertEqual(state.players[0].discard, [first])
        self.assertEqual(state.players[1].discard, [])

    def test_a_stadium_of_the_same_name_may_not_replace_itself(self):
        first = to_hand(self.state, 0, "Arena")
        state, _ = engine.apply(self.state, engine.PlayTrainer(0, first))
        state, _ = engine.apply(state, engine.Pass(0))
        state, _ = engine.apply(state, engine.Pass(1))
        again = to_hand(state, 0, "Arena")
        self.assertEqual(actions_of(state, 0, engine.PlayTrainer), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.PlayTrainer(0, again))

    def test_one_stadium_a_turn(self):
        state, _ = engine.apply(self.state, engine.PlayTrainer(
            0, to_hand(self.state, 0, "Arena")))
        other = to_hand(state, 0, "OtherArena")
        self.assertEqual(state.players[0].stadiums_this_turn, 1)
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.PlayTrainer(0, other))


class PokemonToolTests(unittest.TestCase):

    def setUp(self):
        self.rules = engine.Rules(
            trainer_effects={GUID["Bandana"]: lambda s, c, ch: None},
            static_effects={GUID["Bandana"]: _bandana})
        self.state = make_state(rules=self.rules)
        self.mine = place(self.state, 0, "Pipsqueak")
        place(self.state, 1, "Pipsqueak")

    def test_a_tool_attaches_and_stays_on_the_pokemon(self):
        cid = to_hand(self.state, 0, "Bandana")
        self.assertIn(engine.AttachTool(0, cid, self.mine.slot_id),
                      engine.legal_actions(self.state, 0))
        state, changes = engine.apply(
            self.state, engine.AttachTool(0, cid, self.mine.slot_id))
        slot = state.slot(self.mine.slot_id)[1]
        self.assertEqual(slot.tools, [cid])
        self.assertEqual(len(kinds(changes, engine.CHANGE_TOOL)), 1)

    def test_only_one_tool_per_pokemon(self):
        first = to_hand(self.state, 0, "Bandana")
        second = to_hand(self.state, 0, "Bandana")
        state, _ = engine.apply(self.state,
                                engine.AttachTool(0, first, self.mine.slot_id))
        self.assertEqual(actions_of(state, 0, engine.AttachTool), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.AttachTool(0, second, self.mine.slot_id))

    def test_a_tool_cannot_be_attached_to_the_opponent(self):
        theirs = self.state.players[1].active
        cid = to_hand(self.state, 0, "Bandana")
        with self.assertRaises(engine.IllegalAction):
            engine.apply(self.state, engine.AttachTool(0, cid, theirs.slot_id))

    def test_a_tool_leaves_play_with_the_pokemon_it_is_on(self):
        cid = to_hand(self.state, 0, "Bandana")
        state, _ = engine.apply(self.state,
                                engine.AttachTool(0, cid, self.mine.slot_id))
        slot = state.slot(self.mine.slot_id)[1]
        slot.damage = state.max_hp(slot)
        changes = []
        engine._resolve_knockouts(state, changes)
        self.assertIn(cid, state.players[0].discard)

    def test_a_tool_changes_the_number_it_says_it_changes(self):
        # Bandana adds retreat cost and HP; both have to be read through the
        # Tool everywhere, not just where it was convenient.
        slot = self.mine
        self.assertEqual(engine.retreat_cost(self.state, slot), 1)
        self.assertEqual(self.state.max_hp(slot), 60)
        cid = to_hand(self.state, 0, "Bandana")
        state, _ = engine.apply(self.state,
                                engine.AttachTool(0, cid, slot.slot_id))
        after = state.slot(slot.slot_id)[1]
        self.assertEqual(engine.retreat_cost(state, after), 3)
        self.assertEqual(state.max_hp(after), 90)


def _bandana(query, state, ctx, value):
    """A synthetic Tool: +2 retreat, +30 HP, -10 damage taken."""
    if query == engine.STATIC_RETREAT_COST:
        return value + 2
    if query == engine.STATIC_MAX_HP:
        return value + 30
    if query == engine.STATIC_DAMAGE_TAKEN:
        return value + 10
    return value


# --------------------------------------------------------------------------
# pending choices
# --------------------------------------------------------------------------

class PendingChoiceTests(unittest.TestCase):
    """The suspension model itself, with effects written only for this test."""

    def setUp(self):
        self.log = []

    def board(self, effect, name="Potion"):
        rules = with_trainers(**{name: effect})
        state = make_state(rules=rules)
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak", damage=20)
        return state

    def one_choice(self, options, player=0, minimum=1, maximum=1):
        def effect(state, ctx, changes):
            if not ctx["answers"]:
                return engine.Choice(player=player, prompt="pick",
                                     options=options,
                                     option_kind=engine.CHOICE_OPTION,
                                     minimum=minimum, maximum=maximum)
            self.log.append(ctx["answers"][0])
        return effect

    def test_a_choice_parks_the_state_and_nothing_else_is_legal(self):
        state = self.board(self.one_choice(("a", "b", "c")))
        cid = to_hand(state, 0, "Potion")
        state, changes = engine.apply(state, engine.PlayTrainer(0, cid))

        self.assertIsNotNone(state.pending)
        self.assertEqual(engine.players_to_act(state), [0])
        self.assertEqual(len(kinds(changes, engine.CHANGE_CHOICE)), 1)
        self.assertEqual(kinds(changes, engine.CHANGE_CHOICE)[0].detail["options"],
                         ["a", "b", "c"])

        offered = engine.legal_actions(state, 0)
        self.assertEqual(offered, [engine.Choose(0, ("a",)),
                                   engine.Choose(0, ("b",)),
                                   engine.Choose(0, ("c",))])
        # Not even passing the turn.
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Pass(0))
        self.assertEqual(engine.legal_actions(state, 1), [])

    def test_answering_resumes_the_effect_with_the_answer(self):
        state = self.board(self.one_choice(("a", "b")))
        cid = to_hand(state, 0, "Potion")
        state, _ = engine.apply(state, engine.PlayTrainer(0, cid))
        state, changes = engine.apply(state, engine.Choose(0, ("b",)))

        self.assertIsNone(state.pending)
        self.assertEqual(self.log, [("b",)])
        self.assertEqual(len(kinds(changes, engine.CHANGE_CHOSE)), 1)
        # The turn carries on: the Trainer did not end it.
        self.assertEqual(state.to_move, 0)
        self.assertIn(engine.Pass(0), engine.legal_actions(state, 0))

    def test_an_answer_outside_the_option_list_is_refused(self):
        state = self.board(self.one_choice(("a", "b")))
        state, _ = engine.apply(state, engine.PlayTrainer(
            0, to_hand(state, 0, "Potion")))
        for bad in (engine.Choose(0, ("z",)), engine.Choose(0, ()),
                    engine.Choose(0, ("a", "b"))):
            with self.assertRaises(engine.IllegalAction):
                engine.apply(state, bad)
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Choose(1, ("a",)))

    def test_a_choice_may_belong_to_the_other_player(self):
        state = self.board(self.one_choice(("a", "b"), player=1))
        state, _ = engine.apply(state, engine.PlayTrainer(
            0, to_hand(state, 0, "Potion")))
        # It is still player 0's turn, but player 1 is the one who answers.
        self.assertEqual(state.to_move, 0)
        self.assertEqual(engine.players_to_act(state), [1])
        self.assertEqual(engine.legal_actions(state, 0), [])
        state, _ = engine.apply(state, engine.Choose(1, ("a",)))
        self.assertEqual(engine.players_to_act(state), [0])

    def test_an_optional_choice_offers_declining_first(self):
        state = self.board(self.one_choice(("a", "b"), minimum=0, maximum=1))
        state, _ = engine.apply(state, engine.PlayTrainer(
            0, to_hand(state, 0, "Potion")))
        offered = engine.legal_actions(state, 0)
        self.assertEqual(offered[0], engine.Choose(0, ()))
        state, _ = engine.apply(state, engine.Choose(0, ()))
        self.assertEqual(self.log, [()])

    def test_minimum_is_clamped_so_a_choice_always_has_an_answer(self):
        # An effect that asks for three of two options would otherwise leave a
        # state with no legal action at all, which hangs a live game.
        state = self.board(self.one_choice(("a", "b"), minimum=3, maximum=3))
        state, _ = engine.apply(state, engine.PlayTrainer(
            0, to_hand(state, 0, "Potion")))
        offered = engine.legal_actions(state, 0)
        self.assertTrue(offered)
        state, _ = engine.apply(state, offered[0])
        self.assertEqual(self.log, [("a", "b")])

    def test_enumeration_is_capped_but_apply_still_accepts_more(self):
        options = tuple("abcdefgh")
        rules = engine.Rules(
            max_enumerated_choices=5,
            trainer_effects={GUID["Potion"]: self.one_choice(
                options, minimum=2, maximum=2)})
        state = make_state(rules=rules)
        place(state, 0, "Pipsqueak")
        place(state, 1, "Pipsqueak")
        state, _ = engine.apply(state, engine.PlayTrainer(
            0, to_hand(state, 0, "Potion")))
        offered = engine.legal_actions(state, 0)
        self.assertEqual(len(offered), 5)          # 28 combinations exist
        # A legal answer it declined to list is still legal.
        state, _ = engine.apply(state, engine.Choose(0, ("g", "h")))
        self.assertEqual(self.log, [("g", "h")])

    def test_several_choices_in_one_effect_run_in_order(self):
        def effect(state, ctx, changes):
            answers = ctx["answers"]
            if len(answers) < 1:
                return engine.Choice(player=0, prompt="first",
                                     options=("a", "b"),
                                     option_kind=engine.CHOICE_OPTION)
            if len(answers) < 2:
                return engine.Choice(player=1, prompt="second",
                                     options=("x", "y"),
                                     option_kind=engine.CHOICE_OPTION)
            self.log.append(tuple(answers))

        state = self.board(effect)
        state, _ = engine.apply(state, engine.PlayTrainer(
            0, to_hand(state, 0, "Potion")))
        self.assertEqual(state.pending.choice.prompt, "first")
        state, _ = engine.apply(state, engine.Choose(0, ("a",)))
        self.assertEqual(state.pending.choice.prompt, "second")
        self.assertEqual(engine.players_to_act(state), [1])
        state, _ = engine.apply(state, engine.Choose(1, ("y",)))
        self.assertIsNone(state.pending)
        self.assertEqual(self.log, [(("a",), ("y",))])

    def test_a_pending_choice_survives_the_deep_copy_apply_makes(self):
        state = self.board(self.one_choice(("a", "b")))
        state, _ = engine.apply(state, engine.PlayTrainer(
            0, to_hand(state, 0, "Potion")))
        # apply() copies; the Pending has to be data all the way down or the
        # copy loses the effect's place in its own execution.
        copied = copy.deepcopy(state)
        self.assertIsNot(copied.pending, state.pending)
        copied, _ = engine.apply(copied, engine.Choose(0, ("a",)))
        self.assertIsNone(copied.pending)
        self.assertEqual(self.log, [("a",)])


class AttackChoiceTests(unittest.TestCase):
    """An attack whose effect asks a question must not end the turn early."""

    def test_the_turn_ends_only_once_the_attack_effect_finishes(self):
        log = []

        def effect(state, ctx, changes):
            if not ctx["answers"]:
                return engine.Choice(player=ctx["player"], prompt="pick",
                                     options=("a", "b"),
                                     option_kind=engine.CHOICE_OPTION)
            log.append((ctx["answers"][0], ctx["damage"]))

        rules = engine.Rules(attack_effects={TACKLE: effect})
        state = make_state(rules=rules)
        place(state, 0, "Pipsqueak", energy=["FireEnergy"])
        place(state, 1, "Pipsqueak")

        state, _ = engine.apply(state, engine.Attack(0, TACKLE))
        self.assertIsNotNone(state.pending)
        self.assertEqual(state.to_move, 0)             # turn has NOT passed
        self.assertEqual(state.players[1].active.damage, 10)

        state, _ = engine.apply(state, engine.Choose(0, ("b",)))
        self.assertEqual(log, [(("b",), 10)])
        self.assertEqual(state.to_move, 1)             # ... and now it has

    def test_the_damage_hook_replaces_the_printed_number(self):
        # "40x": three tails means the attack does nothing at all, which is
        # exactly why the hook has to run before the damage lands.
        rules = engine.Rules(attack_damage={CHOMP: lambda s, c, ch: 0})
        state = make_state(rules=rules)
        place(state, 0, "Bigmouth", energy=["FireEnergy", "FireEnergy"])
        defender = place(state, 1, "Pipsqueak")
        state, changes = engine.apply(state, engine.Attack(0, CHOMP))
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 0)
        self.assertEqual(kinds(changes, engine.CHANGE_DAMAGE), [])

    def test_the_damage_hook_runs_before_weakness_and_resistance(self):
        # The hook says 100; the Whale resists Lightning by 20. If the hook
        # ran after the pipeline the answer would be 100, and if the printed
        # damage were used it would be 20.
        rules = engine.Rules(attack_damage={ZAP: lambda s, c, ch: 100})
        state = make_state(rules=rules)
        place(state, 0, "Sparky", energy=["LightningEnergy"])
        defender = place(state, 1, "Whale")
        state, _ = engine.apply(state, engine.Attack(0, ZAP))
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 80)


# --------------------------------------------------------------------------
# continuous effects and temporary modifiers
# --------------------------------------------------------------------------

class ModifierTests(unittest.TestCase):

    def attack_with(self, modifiers=(), rules=None):
        state = make_state(rules=rules or engine.DEFAULT_RULES)
        attacker = place(state, 0, "Pipsqueak", energy=["FireEnergy"])
        defender = place(state, 1, "Blobfish")      # 200 HP, no weakness
        for make in modifiers:
            state.modifiers.append(make(state, attacker, defender))
        state, _ = engine.apply(state, engine.Attack(0, TACKLE))
        return state.slot(defender.slot_id)[1].damage

    def test_damage_dealt_adds_before_weakness(self):
        self.assertEqual(self.attack_with(), 10)
        self.assertEqual(self.attack_with([
            lambda s, a, d: engine.Modifier(kind=engine.MOD_DAMAGE_DEALT,
                                            until_turn=s.turn_number, player=0,
                                            amount=30)]), 40)

    def test_damage_taken_subtracts_after_weakness_and_floors_at_zero(self):
        self.assertEqual(self.attack_with([
            lambda s, a, d: engine.Modifier(kind=engine.MOD_DAMAGE_TAKEN,
                                            until_turn=s.turn_number,
                                            slot=d.slot_id, amount=50)]), 0)

    def test_prevent_damage_beats_everything(self):
        self.assertEqual(self.attack_with([
            lambda s, a, d: engine.Modifier(kind=engine.MOD_DAMAGE_DEALT,
                                            until_turn=s.turn_number, player=0,
                                            amount=100),
            lambda s, a, d: engine.Modifier(kind=engine.MOD_PREVENT_DAMAGE,
                                            until_turn=s.turn_number,
                                            slot=d.slot_id)]), 0)

    def test_no_weakness_switches_off_weakness_and_leaves_resistance(self):
        # Rockjaw is Fighting; the Whale is weak to Fighting (x2) and resists
        # Lightning. Smash is 50, so 100 with the Weakness and 50 without it.
        def board():
            state = make_state()
            place(state, 0, "Rockjaw", energy=["FightingEnergy",
                                               "FightingEnergy"])
            return state, place(state, 1, "Whale")

        state, defender = board()
        after, _ = engine.apply(state, engine.Attack(0, SMASH))
        self.assertEqual(after.slot(defender.slot_id)[1].damage, 100)

        state, defender = board()
        state.modifiers.append(engine.Modifier(
            kind=engine.MOD_NO_WEAKNESS, until_turn=state.turn_number,
            slot=defender.slot_id))
        after, _ = engine.apply(state, engine.Attack(0, SMASH))
        self.assertEqual(after.slot(defender.slot_id)[1].damage, 50)

        # ... and the Resistance it also has is untouched by that.
        state, defender = board()
        state.players[0].active = None
        place(state, 0, "Sparky", energy=["LightningEnergy"])
        after, _ = engine.apply(state, engine.Attack(0, ZAP))
        self.assertEqual(after.slot(defender.slot_id)[1].damage, 20)  # 40 - 20

    def test_a_modifier_expires_and_stops_being_consulted(self):
        state = make_state()
        attacker = place(state, 0, "Pipsqueak", energy=["FireEnergy"])
        defender = place(state, 1, "Blobfish")
        del attacker
        state.modifiers.append(engine.Modifier(
            kind=engine.MOD_DAMAGE_DEALT, until_turn=state.turn_number,
            player=0, amount=30))
        state, _ = engine.apply(state, engine.Pass(0))    # turn 4
        state, _ = engine.apply(state, engine.Pass(1))    # turn 5
        self.assertEqual(state.modifiers, [])
        state, _ = engine.apply(state, engine.Attack(0, TACKLE))
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 10)

    def test_a_no_retreat_modifier_blocks_only_the_slot_it_names(self):
        state = make_state()
        active = place(state, 0, "Rockjaw")               # free retreat
        place(state, 0, "Pipsqueak", where="bench")
        place(state, 1, "Pipsqueak")
        self.assertTrue(actions_of(state, 0, engine.Retreat))
        state.modifiers.append(engine.Modifier(
            kind=engine.MOD_NO_RETREAT, until_turn=state.turn_number,
            slot=active.slot_id))
        self.assertEqual(actions_of(state, 0, engine.Retreat), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.Retreat(
                0, state.players[0].bench[0].slot_id, ()))

    def test_ignoring_resistance_is_a_property_of_the_attack(self):
        state = make_state()
        place(state, 0, "Sparky", energy=["LightningEnergy"])
        defender = place(state, 1, "Whale")               # resists Lightning
        after, _ = engine.apply(state, engine.Attack(0, ZAP))
        self.assertEqual(after.slot(defender.slot_id)[1].damage, 20)  # 40 - 20

        def ignores(s, ctx, ch):
            ctx["data"]["ignore"] = [engine.IGNORE_RESISTANCE]
            return ctx["attack"].damage
        state.rules = engine.Rules(attack_damage={ZAP: ignores})
        after, _ = engine.apply(state, engine.Attack(0, ZAP))
        self.assertEqual(after.slot(defender.slot_id)[1].damage, 40)


# --------------------------------------------------------------------------
# Pokemon Abilities
# --------------------------------------------------------------------------

class AbilityTests(unittest.TestCase):

    def setUp(self):
        self.used = []

    def rules(self, **kw):
        def effect(state, ctx, changes):
            self.used.append((ctx["player"], ctx["slot_id"]))
        base = {"ability_effects": {DIG: effect}}
        base.update(kw)
        return engine.Rules(**base)

    def board(self, rules=None):
        state = make_state(rules=rules or self.rules())
        slot = place(state, 0, "Burrower")
        place(state, 1, "Pipsqueak")
        return state, slot

    def test_an_ability_is_offered_only_when_it_is_implemented(self):
        state, slot = self.board(rules=engine.DEFAULT_RULES)
        self.assertEqual(actions_of(state, 0, engine.UseAbility), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.UseAbility(0, slot.slot_id, DIG))

        state, slot = self.board()
        self.assertIn(engine.UseAbility(0, slot.slot_id, DIG),
                      engine.legal_actions(state, 0))

    def test_an_ability_is_once_per_turn_per_pokemon_and_resets(self):
        state, slot = self.board()
        state, changes = engine.apply(state, engine.UseAbility(0, slot.slot_id, DIG))
        self.assertEqual(self.used, [(0, slot.slot_id)])
        self.assertEqual(len(kinds(changes, engine.CHANGE_ABILITY)), 1)
        self.assertEqual(actions_of(state, 0, engine.UseAbility), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.UseAbility(0, slot.slot_id, DIG))

        state, _ = engine.apply(state, engine.Pass(0))
        state, _ = engine.apply(state, engine.Pass(1))
        self.assertTrue(actions_of(state, 0, engine.UseAbility))

    def test_using_an_ability_does_not_end_the_turn(self):
        state, slot = self.board()
        state, _ = engine.apply(state, engine.UseAbility(0, slot.slot_id, DIG))
        self.assertEqual(state.to_move, 0)

    def test_an_attack_is_never_offered_as_an_ability(self):
        state, slot = self.board()
        offered = {a.ability_id for a in actions_of(state, 0, engine.UseAbility)}
        self.assertEqual(offered, {DIG})

    def test_abilities_can_be_switched_off_for_a_side(self):
        state, slot = self.board()
        state.modifiers.append(engine.Modifier(
            kind=engine.MOD_NO_ABILITIES, until_turn=state.turn_number))
        self.assertEqual(actions_of(state, 0, engine.UseAbility), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.UseAbility(0, slot.slot_id, DIG))

    def test_a_static_ability_changes_retreat_cost_everywhere(self):
        def hook(query, state, ctx, value):
            return 0 if query == engine.STATIC_RETREAT_COST else value

        rules = engine.Rules(static_effects={DIG: hook})
        state = make_state(rules=rules)
        slot = place(state, 0, "Burrower", energy=["FireEnergy"])
        place(state, 0, "Pipsqueak", where="bench")
        place(state, 1, "Pipsqueak")
        # Burrower's printed retreat is 2; the Ability makes it free, and the
        # free-retreat action has to be the one legal_actions offers.
        self.assertEqual(engine.retreat_cost(state, slot), 0)
        bench = state.players[0].bench[0].slot_id
        self.assertIn(engine.Retreat(0, bench, ()),
                      engine.legal_actions(state, 0))
        state, _ = engine.apply(state, engine.Retreat(0, bench, ()))
        self.assertEqual(state.players[0].discard, [])

    def test_evolving_gives_the_new_pokemon_its_own_allowance(self):
        state, slot = self.board()
        state, _ = engine.apply(state, engine.UseAbility(0, slot.slot_id, DIG))
        cid = to_hand(state, 0, "Digger")
        state, _ = engine.apply(state, engine.Evolve(0, cid, slot.slot_id))
        self.assertEqual(state.slot(slot.slot_id)[1].abilities_used, set())


# --------------------------------------------------------------------------
# the real card database
# --------------------------------------------------------------------------

CARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "carddata")


@unittest.skipUnless(os.path.isdir(CARD_DIR), "carddata/ not present")
class RealCardDataTests(unittest.TestCase):
    """Prove the ATTR_* ids mean what the engine thinks they mean.

    The synthetic fixtures above are self-consistent by construction, so only
    real cards with known printed values can show that 200490 is HP rather
    than, say, retreat cost.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def test_the_whole_database_parses(self):
        self.assertGreater(len(self.db), 9000)
        pokemon = [c for c in self.db if c.is_pokemon]
        self.assertGreater(len(pokemon), 7000)
        # Every Pokemon must have HP or the damage model is meaningless.
        self.assertTrue(all(c.max_hp > 0 for c in pokemon))

    def test_watchog_matches_its_printed_card(self):
        watchog = next(c for c in self.db.by_name("Watchog")
                       if c.set_code == "BW1")
        self.assertEqual(watchog.stage, "Stage1")
        self.assertEqual(watchog.max_hp, 90)
        self.assertEqual(watchog.evolves_from, "Patrat")
        self.assertEqual(watchog.retreat_cost, 1)
        self.assertEqual(watchog.weakness_types, ("Fighting",))
        self.assertEqual(watchog.weakness_operator, "x")
        self.assertEqual(watchog.weakness_amount, 2)
        self.assertIsNone(watchog.resistance_type)

        hyper_fang = watchog.attack("63dde8d3-7040-467a-a44e-f409315dd58d")
        self.assertIsNotNone(hyper_fang)
        self.assertEqual(hyper_fang.damage, 60)
        self.assertEqual(hyper_fang.cost, {"Colorless": 2})

    def test_basic_energy_provides_one_symbol_of_its_colour(self):
        psychic = next(c for c in self.db.by_name("PsychicEnergy")
                       if c.is_basic_energy)
        self.assertEqual(psychic.energy_options, (("Psychic",),))
        self.assertEqual(psychic.energy_units, 1)

    def test_double_colorless_provides_two(self):
        dce = self.db.by_name("DoubleColorlessEnergy")[0]
        self.assertEqual(dce.energy_units, 2)
        self.assertTrue(engine.can_pay_cost([list(dce.energy_options)],
                                            {"Colorless": 2}))

    def test_every_attack_keeps_its_ability_id(self):
        # The protocol layer identifies attacks by this GUID; an attack that
        # loses it cannot be selected by the client at all.
        missing = [(c.name, a.title) for c in self.db
                   for a in c.attacks if not a.ability_id]
        self.assertEqual(missing, [])

    def test_trainer_types_are_the_four_kinds_the_engine_knows(self):
        kinds_seen = {}
        for card in self.db:
            if card.is_trainer:
                kinds_seen[card.trainer_kind] = kinds_seen.get(card.trainer_kind, 0) + 1
        # Five values, not four: PokemonToolF is Team Flare Gear, a Tool with
        # a printed restriction the data does not encode.
        self.assertEqual(set(kinds_seen), {engine.TRAINER_ITEM,
                                           engine.TRAINER_SUPPORTER,
                                           engine.TRAINER_STADIUM,
                                           engine.TRAINER_TOOL,
                                           engine.TRAINER_TOOL_F})
        self.assertGreater(kinds_seen[engine.TRAINER_ITEM], 400)
        # Every Trainer has exactly one kind, so trainer_kind is total.
        self.assertTrue(all(c.trainer_kind is not None
                            for c in self.db if c.is_trainer))

    def test_attribute_200310_is_the_trainer_rules_text_key(self):
        """Prove ATTR_GAME_TEXT means what effects.py depends on it meaning.

        Two things have to hold: Trainers carry it and Pokemon never do - a
        Pokemon's text lives per-ability inside ATTR_ABILITIES. If 200310 were
        something else, one of those would fail.

        24 Trainers have no key at all, 23 of them in SL and one in SM4.
        Those cards simply have no text on this machine and get no effect;
        the number is asserted so it cannot grow quietly.
        """
        trainers = [c for c in self.db if c.is_trainer]
        pokemon = [c for c in self.db if c.is_pokemon]
        self.assertGreater(len(trainers), 1000)
        missing = [c for c in trainers if not c.game_text_key]
        self.assertEqual(len(missing), 24)
        self.assertEqual({c.set_code for c in missing}, {"SL", "SM4"})
        self.assertEqual([c.name for c in pokemon if c.game_text_key], [])

        # The key names the card it is on: Potion's says Potion, and it is a
        # key rather than English (English would not contain a dotted path).
        potion = next(c for c in self.db.by_name("Potion")
                      if c.set_code == "BW1")
        self.assertIn("Potion", potion.game_text_key)
        self.assertIn("GameText", potion.game_text_key)
        self.assertNotIn("$", potion.game_text_key)

    def test_amount_operator_takes_exactly_four_values(self):
        """Checked rather than assumed - effects.py cross-checks against it.

        "" is a flat number, "+" and "x" and "-" mean the printed number is
        conditional. Nothing says on WHAT, which is why the operator is never
        the source of an effect, only a corroboration of one.
        """
        seen = {}
        for card in self.db:
            for attack in card.attacks:
                seen[attack.amount_operator] = seen.get(attack.amount_operator, 0) + 1
        self.assertEqual(set(seen), {"", "+", "x", "-"})
        self.assertGreater(seen[""], seen["+"])
        self.assertGreater(seen["+"], seen["x"])
        self.assertGreater(seen["x"], seen["-"])

    def test_a_real_tool_and_a_real_stadium_parse_as_such(self):
        float_stone = next(c for c in self.db.by_name("FloatStone"))
        self.assertTrue(float_stone.is_trainer)
        self.assertTrue(float_stone.is_tool)
        self.assertFalse(float_stone.is_item)
        festival = next(c for c in self.db.by_name("ChampionsFestival"))
        self.assertTrue(festival.is_stadium)
        sycamore = next(c for c in self.db.by_name("ProfessorSycamore"))
        self.assertTrue(sycamore.is_supporter)

    def test_non_attack_abilities_are_separated_from_attacks(self):
        # Card.attacks and Card.pokemon_abilities must partition
        # ATTR_ABILITIES, or an Ability would be selectable as an attack.
        for card in self.db:
            self.assertEqual(len(card.attacks) + len(card.pokemon_abilities),
                             len(card.abilities))
        powered = [c for c in self.db if c.pokemon_abilities]
        self.assertGreater(len(powered), 500)

    def test_a_real_evolution_line_is_playable(self):
        patrat = next(c for c in self.db.by_name("Patrat") if c.set_code == "BW1")
        watchog = next(c for c in self.db.by_name("Watchog") if c.set_code == "BW1")
        fighting = next(c for c in self.db if c.name == "FightingEnergy"
                        and c.is_basic_energy)

        deck = [patrat.guid] * 12 + [watchog.guid] * 8 + [fighting.guid] * 40
        state, _ = engine.new_game(self.db, [list(deck), list(deck)],
                                   rng=ScriptedRandom(), first_player=0)
        for p in (0, 1):
            state, _ = engine.apply(state, engine.SetupPlaceActive(
                p, state.players[p].hand[0]))
            state, _ = engine.apply(state, engine.SetupDone(p))

        # Turn 1: no evolving, and the first player may not attack.
        self.assertEqual(actions_of(state, 0, engine.Evolve), [])
        self.assertEqual(actions_of(state, 0, engine.Attack), [])

        state, _ = engine.apply(state, engine.Pass(0))
        state, _ = engine.apply(state, engine.Pass(1))

        # Turn 3: the setup-placed Patrat is now old enough to evolve.
        slot = state.players[0].active
        hand_watchog = next((c for c in state.players[0].hand
                             if state.card(c).name == "Watchog"), None)
        if hand_watchog is None:                    # unlucky deal, force one
            hand_watchog = engine._new_card(state, watchog.guid, 0)
            state.players[0].hand.append(hand_watchog)
        state, _ = engine.apply(state, engine.Evolve(0, hand_watchog, slot.slot_id))
        self.assertEqual(state.card(state.players[0].active.top).name, "Watchog")


if __name__ == "__main__":
    unittest.main()
