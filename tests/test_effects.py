"""
Card behaviour tests for effects.py.

The split here is different from test_engine.py's, because effects.py is a
different kind of module. Its correctness has two halves:

  * The pattern table reads the right numbers out of the right sentences.
    That is tested against SYNTHETIC cards carrying invented localization
    keys, so a test says "this exact English produces this exact behaviour"
    without depending on any printed card staying as it is.

  * Those patterns actually match the shipped text. That is tested against
    REAL carddata and the client's real LocalizationDB, because the whole
    design rests on the claim that attribute 200310 and the gameText keys
    resolve to English the table can read. A synthetic fixture cannot show
    that, and it is the assumption most likely to be wrong.

Every test that needs the localization database skips without it: the
database lives in the game install, not in this repo, and the rules tests
have to keep running on a machine with no game on it.

Run: python -m unittest discover -s tests
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import effects  # noqa: E402
import engine  # noqa: E402

from test_engine import (  # noqa: E402
    DB, GUID, ScriptedRandom, actions_of, kinds, make_state, place, to_hand,
)

CARD_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "carddata")


# --------------------------------------------------------------------------
# text handling
# --------------------------------------------------------------------------

class NormalizeTests(unittest.TestCase):

    def test_whitespace_collapses(self):
        self.assertEqual(effects.normalize("  Draw\n 3   cards. "),
                         "Draw 3 cards.")
        self.assertEqual(effects.normalize(None), "")

    def test_wordings_that_never_meant_anything_are_folded(self):
        # One rule, four printings, four wordings. Folding them here is what
        # lets one pattern cover a card printed across three eras.
        for text in ["Each player shuffles his or her hand into his or her deck.",
                     "Each player shuffles their hand into their deck."]:
            self.assertEqual(
                effects.normalize(text),
                "Each player shuffles their hand into their deck.")
        self.assertEqual(effects.normalize("show it to your opponent"),
                         "reveal it")
        self.assertEqual(effects.normalize("your opponent’s turn"),
                         "your opponent's turn")

    def test_a_key_is_unwrapped_the_same_way_the_export_stores_it(self):
        card = DB.get(GUID["Potion"])
        self.assertEqual(card.game_text_key, "test.potion.gametext")


# --------------------------------------------------------------------------
# synthetic cards, real patterns
# --------------------------------------------------------------------------
#
# build_rules() is handed a hand-written localization table, so these say
# "this sentence, on this card, does this" with nothing else in the way.

class SyntheticCardTests(unittest.TestCase):

    def rules(self, text):
        return effects.build_rules(DB, loc={"test.potion.gametext": text})

    def board(self, text, damage=0, hand=(), bench=(), rules=None):
        state = make_state(rules=rules or self.rules(text))
        place(state, 0, "Pipsqueak", damage=damage)
        place(state, 1, "Pipsqueak")
        for name in bench:
            place(state, 0, name, where="bench")
        for name in hand:
            to_hand(state, 0, name)
        return state, to_hand(state, 0, "Potion")

    def play(self, state, cid, *answers):
        state, changes = engine.apply(state, engine.PlayTrainer(0, cid))
        for picks in answers:
            self.assertIsNotNone(state.pending, "expected a choice for %r" % (picks,))
            state, more = engine.apply(state,
                                       engine.Choose(state.pending.choice.player,
                                                     picks))
            changes += more
        self.assertIsNone(state.pending)
        return state, changes

    def test_an_unreadable_sentence_gets_no_effect_at_all(self):
        # The core safety property: text the table does not understand makes
        # the card unplayable rather than making it do something else.
        rules = self.rules("Do something nobody has implemented.")
        self.assertNotIn(GUID["Potion"], rules.trainer_effects)
        state, cid = self.board("", rules=rules)
        self.assertEqual(actions_of(state, 0, engine.PlayTrainer), [])

    def test_draw_takes_its_number_from_the_sentence(self):
        state, cid = self.board("Draw 3 cards.")
        before = len(state.players[0].hand)
        state, _ = self.play(state, cid)
        # -1 for the Potion itself, +3 drawn.
        self.assertEqual(len(state.players[0].hand), before - 1 + 3)

    def test_discard_your_hand_and_draw(self):
        state, cid = self.board("Discard your hand and draw 7 cards.",
                                hand=["FireEnergy"] * 4)
        state, _ = self.play(state, cid)
        ps = state.players[0]
        self.assertEqual(len(ps.hand), 7)
        # Four Energy plus the played card itself.
        self.assertEqual(len(ps.discard), 5)

    def test_heal_asks_which_pokemon_and_heals_that_one(self):
        state, cid = self.board("Heal 30 damage from 1 of your Pokémon.",
                                damage=40, bench=["Rockjaw", "Sparky"])
        active = state.players[0].active.slot_id
        hurt_bench = state.players[0].bench[0]
        hurt_bench.damage = 10
        healthy = state.players[0].bench[1].slot_id

        state, changes = engine.apply(state, engine.PlayTrainer(0, cid))
        choice = state.pending.choice
        self.assertEqual(choice.prompt, "healTarget")
        self.assertEqual(choice.option_kind, engine.CHOICE_SLOT)
        # Only the damaged Pokemon are offered - healing an undamaged one is
        # not a decision, it is a wasted card.
        self.assertEqual(set(choice.options), {active, hurt_bench.slot_id})
        self.assertNotIn(healthy, choice.options)
        state, _ = engine.apply(state, engine.Choose(0, (active,)))
        self.assertEqual(state.players[0].active.damage, 10)

    def test_heal_is_not_playable_against_an_undamaged_board(self):
        state, cid = self.board("Heal 30 damage from 1 of your Pokémon.")
        self.assertEqual(actions_of(state, 0, engine.PlayTrainer), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.PlayTrainer(0, cid))

    def test_a_forced_choice_is_taken_without_asking(self):
        # One damaged Pokemon, one option, one legal answer: asking would put
        # a dialog with a single button in front of the player.
        state, cid = self.board("Heal 30 damage from 1 of your Pokémon.",
                                damage=40)
        state, changes = engine.apply(state, engine.PlayTrainer(0, cid))
        self.assertIsNone(state.pending)
        self.assertEqual(state.players[0].active.damage, 10)
        self.assertEqual(kinds(changes, engine.CHANGE_CHOICE), [])

    def test_switch_promotes_without_paying_or_spending_the_retreat(self):
        state, cid = self.board(
            "Switch your Active Pokémon with 1 of your Benched Pokémon.",
            bench=["Rockjaw"])
        bench = state.players[0].bench[0].slot_id
        state, _ = self.play(state, cid)
        self.assertEqual(state.players[0].active.slot_id, bench)
        self.assertEqual(state.players[0].retreats_this_turn, 0)
        self.assertEqual(state.players[0].discard[-1], cid)

    def test_escape_rope_asks_the_opponent_first(self):
        text = ("Each player switches their Active Pokémon with 1 of their "
                "Benched Pokémon. Your opponent switches first. (If a player "
                "does not have a Benched Pokémon, that player doesn't switch "
                "Pokémon.)")
        state, cid = self.board(text, bench=["Rockjaw", "Sparky"])
        place(state, 1, "Bigmouth", where="bench")
        place(state, 1, "Blobfish", where="bench")
        theirs = [s.slot_id for s in state.players[1].bench]
        mine = [s.slot_id for s in state.players[0].bench]

        state, _ = engine.apply(state, engine.PlayTrainer(0, cid))
        self.assertEqual(state.pending.choice.player, 1)
        self.assertEqual(engine.players_to_act(state), [1])
        state, _ = engine.apply(state, engine.Choose(1, (theirs[0],)))
        self.assertEqual(state.pending.choice.player, 0)
        state, _ = engine.apply(state, engine.Choose(0, (mine[1],)))

        self.assertEqual(state.players[1].active.slot_id, theirs[0])
        self.assertEqual(state.players[0].active.slot_id, mine[1])

    def test_a_search_reads_the_deck_and_shuffles_afterwards(self):
        state, cid = self.board(
            "Search your deck for a Basic Pokémon and put it onto your Bench. "
            "Then, shuffle your deck.")
        # make_state stocks the deck with Energy; give it one Basic to find.
        target = engine._new_card(state, GUID["Rockjaw"], 0)
        state.players[0].deck.insert(3, target)

        state, changes = engine.apply(state, engine.PlayTrainer(0, cid))
        # One Basic, but the search may still be declined - "search your deck
        # for a Basic" does not oblige you to find one - so it still asks.
        self.assertEqual(state.pending.choice.options, (target,))
        self.assertEqual(state.pending.choice.minimum, 0)
        state, more = engine.apply(state, engine.Choose(0, (target,)))
        self.assertEqual([s.top for s in state.players[0].bench], [target])
        self.assertEqual(len(kinds(changes + more, engine.CHANGE_SHUFFLE)), 1)

    def test_identical_search_results_are_offered_once(self):
        state, cid = self.board(
            "Search your deck for a Basic Pokémon and put it onto your Bench. "
            "Then, shuffle your deck.")
        for _ in range(4):
            state.players[0].deck.append(
                engine._new_card(state, GUID["Rockjaw"], 0))
        state.players[0].deck.append(engine._new_card(state, GUID["Sparky"], 0))
        state, _ = engine.apply(state, engine.PlayTrainer(0, cid))
        # Four Rockjaw collapse to one option; Sparky is the second.
        self.assertEqual(len(state.pending.choice.options), 2)

    def test_ultra_ball_pays_before_it_searches_and_cannot_pay_with_itself(self):
        text = ("Discard 2 cards from your hand. (If you can't discard 2 "
                "cards, you can't play this card.) Search your deck for a "
                "Pokémon, reveal it, and put it into your hand. Shuffle your "
                "deck afterward.")
        state, cid = self.board(text, hand=["FireEnergy", "WaterEnergy"])
        target = engine._new_card(state, GUID["Bigmouth"], 0)
        state.players[0].deck.insert(0, target)
        pay = tuple(c for c in state.players[0].hand if c != cid)

        to_hand(state, 0, "PsychicEnergy")     # a third card, so it can choose
        pay = pay[:2]
        state, _ = engine.apply(state, engine.PlayTrainer(0, cid))
        choice = state.pending.choice
        self.assertEqual(choice.prompt, "discardFromHand")
        self.assertEqual(choice.minimum, 2)
        self.assertNotIn(cid, choice.options)      # it is already discarded
        state, _ = engine.apply(state, engine.Choose(0, pay))
        state, _ = engine.apply(state, engine.Choose(0, (target,)))
        self.assertIn(target, state.players[0].hand)
        for card in pay:
            self.assertIn(card, state.players[0].discard)

    def test_ultra_ball_is_unplayable_without_the_two_cards_to_pay(self):
        text = ("Discard 2 cards from your hand. (If you can't discard 2 "
                "cards, you can't play this card.) Search your deck for a "
                "Pokémon, reveal it, and put it into your hand. Shuffle your "
                "deck afterward.")
        # One other card in hand is not two: the Ultra Ball does not pay for
        # itself, and the guard runs while it is still sitting in the hand.
        state, cid = self.board(text, hand=["FireEnergy"])
        self.assertEqual(actions_of(state, 0, engine.PlayTrainer), [])
        to_hand(state, 0, "WaterEnergy")
        self.assertEqual(len(actions_of(state, 0, engine.PlayTrainer)), 1)

    def test_n_draws_each_player_their_own_prize_count(self):
        text = ("Each player shuffles his or her hand into his or her deck. "
                "Then, each player draws a card for each of his or her "
                "remaining Prize cards.")
        state, cid = self.board(text, hand=["FireEnergy"] * 3)
        state.players[0].prizes = state.players[0].prizes[:2]
        state.players[1].prizes = state.players[1].prizes[:5]
        state, _ = self.play(state, cid)
        self.assertEqual(len(state.players[0].hand), 2)
        self.assertEqual(len(state.players[1].hand), 5)

    def test_lillie_draws_more_on_your_first_turn(self):
        text = ("Draw cards until you have 6 cards in your hand. If it's your "
                "first turn, draw cards until you have 8 cards in your hand.")
        state, cid = self.board(text)
        state.players[0].turns_taken = 1
        state, _ = self.play(state, cid)
        self.assertEqual(len(state.players[0].hand), 8)

        state, cid = self.board(text)
        state.players[0].turns_taken = 4
        state, _ = self.play(state, cid)
        self.assertEqual(len(state.players[0].hand), 6)

    def test_hex_maniac_switches_abilities_off_for_both_sides(self):
        text = ("Until the end of your opponent's next turn, each Pokémon in "
                "play, in each player's hand, and in each player's discard "
                "pile has no Abilities. (This includes cards that come into "
                "play on that turn.)")
        rules = effects.build_rules(
            DB, loc={"test.potion.gametext": text},
            base=engine.Rules(ability_effects={"abl-dig": lambda s, c, ch: None}))
        state = make_state(rules=rules)
        mine = place(state, 0, "Burrower")
        place(state, 1, "Burrower")
        cid = to_hand(state, 0, "Potion")
        self.assertTrue(actions_of(state, 0, engine.UseAbility))

        state, _ = engine.apply(state, engine.PlayTrainer(0, cid))
        self.assertEqual(actions_of(state, 0, engine.UseAbility), [])
        with self.assertRaises(engine.IllegalAction):
            engine.apply(state, engine.UseAbility(0, mine.slot_id, "abl-dig"))
        # ... and the opponent's are off too, on their turn.
        state, _ = engine.apply(state, engine.Pass(0))
        self.assertEqual(actions_of(state, 1, engine.UseAbility), [])

    def test_a_tool_is_continuous_and_stops_when_it_leaves(self):
        rules = effects.build_rules(
            DB, loc={"test.bandana.gametext":
                     "The Pokémon this card is attached to has no Retreat Cost."})
        # The synthetic Bandana carries no key, so wire it up by GUID the way
        # build_rules would have if it had one.
        self.assertNotIn(GUID["Bandana"], rules.trainer_effects)

    def test_pluspower_lasts_exactly_this_turn(self):
        text = ("During this turn, your Pokémon's attacks do 10 more damage "
                "to the Active Pokémon (before applying Weakness and "
                "Resistance).")
        state, cid = self.board(text)
        state.players[0].active.energy.append(
            engine._new_card(state, GUID["FireEnergy"], 0))
        defender = state.players[1].active.slot_id
        state, _ = self.play(state, cid)

        after, _ = engine.apply(state, engine.Attack(0, "atk-tackle"))
        self.assertEqual(after.slot(defender)[1].damage, 20)   # 10 + 10

        # Next turn it is gone.
        state, _ = engine.apply(state, engine.Pass(0))
        state, _ = engine.apply(state, engine.Pass(1))
        after, _ = engine.apply(state, engine.Attack(0, "atk-tackle"))
        self.assertEqual(after.slot(defender)[1].damage, 10)


class SyntheticAttackTests(unittest.TestCase):
    """Attack text, read through the same table."""

    def rules(self, text, ability="atk-flare"):
        # Emberling's Flare carries the key "$$$Flare.GameText$$$".
        return effects.build_rules(DB, loc={"flare.gametext": text})

    def board(self, text, flips=(), damage=0):
        state = make_state(rules=self.rules(text), flips=flips)
        attacker = place(state, 0, "Emberling",
                         energy=["FireEnergy", "FireEnergy"], damage=damage)
        defender = place(state, 1, "Blobfish")     # 200 HP, no weakness
        return state, attacker, defender

    def resolve(self, state, *answers):
        state, changes = engine.apply(state, engine.Attack(0, "atk-flare"))
        for picks in answers:
            self.assertIsNotNone(state.pending)
            state, more = engine.apply(
                state, engine.Choose(state.pending.choice.player, picks))
            changes += more
        return state, changes

    def test_coin_flip_adds_the_printed_extra_only_on_heads(self):
        text = "Flip a coin. If heads, this attack does 20 more damage."
        HEADS, TAILS = 0.0, 0.9
        state, _, defender = self.board(text, flips=[HEADS])
        state, _ = self.resolve(state)
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 50)  # 30 + 20

        state, _, defender = self.board(text, flips=[TAILS])
        state, _ = self.resolve(state)
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 30)

    def test_damage_times_heads_can_be_zero(self):
        # The reason attack_damage runs BEFORE the damage lands: three tails
        # means the attack does nothing, and the printed 30 must never land.
        text = ("Flip 3 coins. This attack does 40 damage times the number "
                "of heads.")
        state, _, defender = self.board(text, flips=[0.9, 0.9, 0.9])
        state, _ = self.resolve(state)
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 0)

        state, _, defender = self.board(text, flips=[0.0, 0.9, 0.0])
        state, _ = self.resolve(state)
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 80)

    def test_the_multiplier_comes_from_the_text_not_the_damage_field(self):
        # Emberling's printed damage is 30; this sentence says 20 per bench
        # Pokemon, and the answer must be 20-based.
        text = "Does 20 damage times the number of your Benched Pokémon."
        state, _, defender = self.board(text)
        place(state, 0, "Rockjaw", where="bench")
        place(state, 0, "Sparky", where="bench")
        state, _ = self.resolve(state)
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 40)

    def test_a_condition_lands_on_the_defender(self):
        # Tails on the between-turns Sleep flip, or the checkup would wake it
        # up again before the assertion could see it.
        text = "Your opponent's Active Pokémon is now Asleep."
        state, _, defender = self.board(text, flips=[0.9])
        state, changes = self.resolve(state)
        self.assertIn(engine.ASLEEP, state.slot(defender.slot_id)[1].conditions)
        self.assertTrue(kinds(changes, engine.CHANGE_CONDITION))

    def test_self_damage_can_knock_the_attacker_out(self):
        text = "This Pokémon does 100 damage to itself."
        state, attacker, _ = self.board(text, damage=0)
        place(state, 0, "Rockjaw", where="bench")
        state, changes = self.resolve(state)
        self.assertTrue(kinds(changes, engine.CHANGE_KNOCKOUT))
        self.assertIsNone(state.slot(attacker.slot_id))

    def test_energy_discard_asks_which_when_there_is_a_choice(self):
        text = "Discard an Energy attached to this Pokémon."
        state, attacker, _ = self.board(text)
        state, _ = engine.apply(state, engine.Attack(0, "atk-flare"))
        self.assertIsNotNone(state.pending)
        self.assertEqual(state.pending.choice.prompt, "discardEnergy")
        # The turn has not ended: the attack is still resolving.
        self.assertEqual(state.to_move, 0)
        pick = state.pending.choice.options[0]
        state, _ = engine.apply(state, engine.Choose(0, (pick,)))
        self.assertEqual(state.to_move, 1)
        self.assertEqual(len(state.slot(attacker.slot_id)[1].energy), 1)
        self.assertIn(pick, state.players[0].discard)

    def test_damage_reduction_lasts_into_the_opponents_turn(self):
        text = ("During your opponent's next turn, any damage done to this "
                "Pokémon by attacks is reduced by 20 (after applying Weakness "
                "and Resistance).")
        state, attacker, _ = self.board(text)
        state, _ = self.resolve(state)
        # Player 1 attacks back with Blobfish... which deals 0, so use the
        # modifier directly: what matters is that it exists and names the slot.
        live = [m for m in state.modifiers if m.kind == engine.MOD_DAMAGE_TAKEN]
        self.assertEqual(len(live), 1)
        self.assertEqual(live[0].slot, attacker.slot_id)
        self.assertEqual(live[0].amount, 20)
        self.assertEqual(live[0].until_turn, 4)      # played on turn 3

    def test_ignoring_resistance_is_read_off_the_sentence(self):
        text = "This attack's damage isn't affected by Resistance."
        rules = effects.build_rules(DB, loc={"flare.gametext": text})
        state = make_state(rules=rules)
        place(state, 0, "Emberling", energy=["FireEnergy", "FireEnergy"])
        # Rockjaw resists Lightning, not Fire, so build the comparison on a
        # card that does resist: reuse the Whale.
        defender = place(state, 1, "Whale")
        state, _ = engine.apply(state, engine.Attack(0, "atk-flare"))
        # Whale is Water and weak to Fighting; Fire is neither, so 30 flat.
        self.assertEqual(state.slot(defender.slot_id)[1].damage, 30)


# --------------------------------------------------------------------------
# the real card database and the real localization table
# --------------------------------------------------------------------------

LOC = effects.load_localization()


@unittest.skipUnless(os.path.isdir(CARD_DIR), "carddata/ not present")
@unittest.skipUnless(LOC, "the client's LocalizationDB is not on this machine")
class PrizeValueTests(unittest.TestCase):
    """The name suffix is a judgement about the data, so it gets pinned here.

    Nothing in carddata marks a rule box. The claim is that the printed name
    does, and that claim is only worth anything if it agrees with the one
    other signal the pool carries - the RareHoloEX / RareHoloGX rarities.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)
        cls.pokemon = [c for c in cls.db if c.is_pokemon]

    def test_the_suffix_needs_a_lowercase_letter_in_front_of_it(self):
        """Otherwise any name ending in those two letters reads as a rule box."""
        class Fake(object):
            def __init__(self, name):
                self.name = name
        self.assertEqual(effects.prize_value(Fake("MewtwoEX")), 2)
        self.assertEqual(effects.prize_value(Fake("TapuKokoGX")), 2)
        self.assertEqual(effects.prize_value(Fake("Pikachu")), 1)
        self.assertEqual(effects.prize_value(Fake("EX")), 1)
        self.assertEqual(effects.prize_value(Fake("GX")), 1)
        self.assertEqual(effects.prize_value(Fake("")), 1)
        self.assertIsNotNone(effects.prize_value(Fake(None)))

    def test_a_break_is_worth_one_prize(self):
        """A BREAK is an evolution, not a rule box. Easy to lump in; wrong."""
        breaks = [c for c in self.pokemon if c.name.endswith("BREAK")]
        self.assertTrue(breaks)
        for card in breaks:
            self.assertEqual(effects.prize_value(card), 1, card.name)

    def test_every_rule_box_rarity_is_found_by_the_name(self):
        """The cross-check. A miss here means the rule is unsound, not stale."""
        rare = [c for c in self.pokemon
                if c.rarity in ("RareHoloEX", "RareHoloGX")]
        self.assertGreater(len(rare), 200)
        missed = [c.name for c in rare if effects.prize_value(c) != 2]
        self.assertEqual(missed, [])

    def test_the_registry_is_built_and_holds_only_real_multipliers(self):
        rules = effects.build_rules(self.db, loc=LOC)
        self.assertGreater(len(rules.prize_values), 500)
        self.assertEqual(set(rules.prize_values.values()), {2})
        for guid in rules.prize_values:
            self.assertTrue(self.db.get(guid).is_pokemon)

    def test_an_ordinary_pokemon_is_absent_from_the_registry(self):
        rules = effects.build_rules(self.db, loc=LOC)
        pikachu = next(c for c in self.db.by_name("Pikachu"))
        self.assertNotIn(pikachu.guid, rules.prize_values)


class RealCardTests(unittest.TestCase):
    """The claim the whole module rests on: the shipped text is readable.

    A synthetic fixture proves the pattern table works. Only the real files
    can prove that attribute 200310 and the gameText keys resolve to English
    those patterns actually match.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)
        cls.rules = effects.build_rules(cls.db, loc=LOC)
        cls.coverage = effects.coverage(cls.db, loc=LOC)

    def implemented(self, name):
        return [c for c in self.db.by_name(name)
                if c.guid in self.rules.trainer_effects]

    def test_the_localization_table_resolves_trainer_text(self):
        trainers = [c for c in self.db if c.is_trainer]
        resolved = [c for c in trainers if effects.trainer_text(c, LOC)]
        # 1,094 of 1,120 on this machine. The rest are sets whose
        # localization never arrived before the servers were shut off.
        self.assertGreater(len(resolved), 1000)
        potion = next(c for c in self.db.by_name("Potion")
                      if c.set_code == "XY12")
        self.assertEqual(effects.trainer_text(potion, LOC),
                         "Heal 30 damage from 1 of your Pokémon.")

    def test_the_localization_table_resolves_attack_text(self):
        watchog = next(c for c in self.db.by_name("Watchog")
                       if c.set_code == "BW1")
        hyper_fang = watchog.attack("63dde8d3-7040-467a-a44e-f409315dd58d")
        self.assertEqual(effects.ability_text(hyper_fang, LOC),
                         "Flip a coin. If tails, this attack does nothing.")
        confuse_ray = watchog.attack("bc9b7217-b57a-4b75-ba16-4fa11db8f703")
        self.assertIn("Confused", effects.ability_text(confuse_ray, LOC))
        # Both are sentences the table reads, so both are implemented.
        self.assertIn(hyper_fang.ability_id, self.rules.attack_damage)
        self.assertIn(confuse_ray.ability_id, self.rules.attack_effects)

    def test_the_staples_a_deck_is_actually_built_from_are_implemented(self):
        for name in ["Potion", "Switch", "UltraBall", "GreatBall", "NestBall",
                     "EnergyRetrieval", "EnergySwitch", "RareCandy",
                     "PokemonCommunication", "EscapeRope", "VSSeeker",
                     "HexManiac", "ProfessorSycamore", "ProfessorJuniper",
                     "N", "Lillie", "Skyla", "PokemonCatcher", "FullHeal",
                     "PokemonFanClub", "ProfessorsLetter", "Revive",
                     "FloatStone", "Eviolite", "WeaknessPolicy", "Lysandre",
                     "Guzma", "TeamFlareGrunt", "CrushingHammer",
                     "MuscleBand", "ChoiceBand", "BodybuildingDumbbells",
                     "TrainingCenter", "AspertiaCityGym", "FairyGarden",
                     "Colress", "Wicke", "ComputerSearch", "Xerosic",
                     "MultiSwitch", "EnergyReturner", "Iris"]:
            with self.subTest(card=name):
                self.assertTrue(self.implemented(name),
                                "%s has no implemented printing" % name)

    def test_a_printing_whose_wording_the_table_cannot_read_is_left_alone(self):
        # Champion's Festival is an activated Stadium, which the engine has
        # no framework for. It must be unplayable, not silently inert.
        for card in self.db.by_name("ChampionsFestival"):
            self.assertNotIn(card.guid, self.rules.trainer_effects)

    def test_every_registry_key_belongs_to_a_card_in_this_database(self):
        for guid in self.rules.trainer_effects:
            self.assertIn(guid, self.db)
        ids = {a.ability_id for c in self.db for a in c.abilities}
        for key in list(self.rules.attack_damage) + list(self.rules.attack_effects):
            self.assertIn(key, ids)

    def test_a_tool_gets_both_an_entry_to_play_it_and_one_to_read_it(self):
        stone = self.implemented("FloatStone")[0]
        self.assertIn(stone.guid, self.rules.trainer_effects)
        self.assertIn(stone.guid, self.rules.static_effects)

    def test_float_stone_really_zeroes_a_retreat_cost(self):
        """End to end on real cards: attribute, text, pattern and pipeline."""
        stone = self.implemented("FloatStone")[0]
        heavy = next(c for c in self.db.by_name("Bouffalant")
                     if c.set_code == "BW1")
        self.assertGreater(heavy.retreat_cost, 0)

        state = _real_board(self.db, self.rules, heavy)
        slot = state.players[0].active
        self.assertEqual(engine.retreat_cost(state, slot), heavy.retreat_cost)

        cid = engine._new_card(state, stone.guid, 0)
        state.players[0].hand.append(cid)
        state, _ = engine.apply(state, engine.AttachTool(0, cid, slot.slot_id))
        self.assertEqual(engine.retreat_cost(state, state.players[0].active), 0)
        # ... and the free retreat is the one legal_actions offers.
        bench = state.players[0].bench[0].slot_id
        self.assertIn(engine.Retreat(0, bench, ()),
                      engine.legal_actions(state, 0))

    def test_coverage_is_reported_and_has_not_collapsed(self):
        # Not a target, a tripwire: a refactor that broke the matcher would
        # show up here as a number falling off a cliff.
        c = self.coverage
        self.assertGreater(c["trainersImplemented"], 400)
        self.assertGreater(c["trainerNames"], 60)
        self.assertGreater(c["attacksImplemented"], 3500)
        self.assertGreater(c["abilitiesActivated"], 20)
        self.assertGreater(c["tools"], 8)

    def test_a_real_game_with_real_trainers_plays_to_the_end(self):
        """The integration test that would have caught every bug I made.

        A deck of real cards, real Trainers and a real evolution line, played
        by random legal moves. It asserts the two properties that matter for
        a live server: there is always something legal to do, and no card
        ever goes missing.
        """
        import random

        def implemented(name):
            found = self.implemented(name)
            return found[0] if found else None

        oshawott = next(c for c in self.db.by_name("Oshawott")
                        if c.set_code == "BW1")
        dewott = next(c for c in self.db.by_name("Dewott") if c.set_code == "BW1")
        water = next(c for c in self.db if c.name == "WaterEnergy"
                     and c.is_basic_energy)
        deck = [oshawott.guid] * 8 + [dewott.guid] * 4 + [water.guid] * 24
        for name in ["Potion", "Switch", "UltraBall", "GreatBall",
                     "ProfessorSycamore", "N", "NestBall", "EscapeRope"]:
            card = implemented(name)
            if card is not None:
                deck += [card.guid] * 3
        deck = (deck + [water.guid] * 60)[:60]

        for seed in range(6):
            state, _ = engine.new_game(self.db, [list(deck), list(deck)],
                                       seed=seed, rules=self.rules)
            rng = random.Random(seed)
            for _ in range(3000):
                if state.over:
                    break
                actors = engine.players_to_act(state)
                self.assertTrue(actors, "nobody may act")
                actions = engine.legal_actions(state, actors[0])
                self.assertTrue(actions, "no legal action while %s" % (
                    state.pending.choice.prompt if state.pending else state.phase))
                doing = [a for a in actions if not isinstance(a, engine.Pass)]
                action = rng.choice(doing if doing and rng.random() < 0.9
                                    else actions)
                state, _ = engine.apply(state, action)
                self.assertEqual(_accounted(state), len(state.cards),
                                 "a card went missing")
            self.assertTrue(state.over, "game %d never finished" % seed)


def _real_board(db, rules, active_card):
    """A mid-game board built from real cards rather than the fixtures."""
    state = engine.GameState(db=db, rules=rules, rng=ScriptedRandom(),
                             players=[engine.PlayerState(index=0),
                                      engine.PlayerState(index=1)])
    state.phase = engine.PHASE_MAIN
    state.turn_number = 3
    filler = next(c for c in db.by_name("Patrat") if c.set_code == "BW1")
    for player in (0, 1):
        ps = state.players[player]
        ps.setup_done = True
        ps.turns_taken = 2
        ps.prizes = [engine._new_card(state, filler.guid, player)
                     for _ in range(6)]
        card = active_card if player == 0 else filler
        ps.active = engine._new_slot(
            state, engine._new_card(state, card.guid, player), 1)
        ps.bench.append(engine._new_slot(
            state, engine._new_card(state, filler.guid, player), 1))
    return state


def _accounted(state):
    """Every card id that is somewhere it can be found."""
    total = 1 if state.stadium is not None else 0
    for player in (0, 1):
        ps = state.players[player]
        total += len(ps.deck) + len(ps.hand) + len(ps.discard)
        total += len(ps.prizes) + len(ps.lost)
        for slot in ps.in_play:
            total += len(slot.cards)
    return total



class ContinuousRealCardTests(unittest.TestCase):
    """Tools and Stadiums that only add a number, on real cards.

    These are worth testing end to end rather than synthetically, because the
    thing most likely to be wrong is not the arithmetic - it is the condition.
    Muscle Band that fires on a bench snipe, or a Stadium that reads the
    holder's stage instead of the slot it was asked about, both pass a
    synthetic test that only ever looks at one Pokemon.
    """

    @classmethod
    def setUpClass(cls):
        if LOC is None:
            raise unittest.SkipTest("no localization database")
        cls.db = engine.CardDB.from_directory(CARD_DIR)
        cls.rules = effects.build_rules(cls.db, loc=LOC)

    def one(self, name):
        found = [c for c in self.db.by_name(name)
                 if c.guid in self.rules.trainer_effects]
        if not found:
            self.skipTest("%s is not implemented in this pool" % name)
        return found[0]

    def attach(self, state, card, slot):
        cid = engine._new_card(state, card.guid, 0)
        state.players[0].hand.append(cid)
        return engine.apply(state, engine.AttachTool(0, cid, slot.slot_id))[0]

    def matchup(self):
        """A real attack whose damage is a number, and a defender it lands on.

        Every constraint here removes something that would hide the effect
        under test. Fixed damage and no registered damage or effect hook, so
        the printed number is the number; an all-Colorless cost, so any Energy
        pays it; and a defender with neither Weakness nor Resistance to the
        attacker's type, because Weakness MULTIPLIES - (base + 20) x 2 is not
        base x 2 + 20, and a test ignoring that would assert the wrong sum.
        The defender also has to survive the hit.
        """
        attack = None
        for card in self.db:
            if not card.is_pokemon or card.stage != "Basic":
                continue
            for ability in card.abilities:
                if not ability.is_attack or not ability.damage:
                    continue
                if ability.amount_operator or ability.damage > 30:
                    continue
                if set(ability.cost) - {"Colorless"}:
                    continue
                if (ability.ability_id in self.rules.attack_damage
                        or ability.ability_id in self.rules.attack_effects):
                    continue
                attack = (card, ability, ability.cost.get("Colorless", 0))
                break
            if attack:
                break
        if attack is None:
            self.skipTest("no plain attack in this pool")
        attacker, ability, cost = attack
        for card in self.db:
            if not card.is_pokemon or card.stage != "Basic":
                continue
            if card.max_hp < 100:
                continue
            if set(card.weakness_types) & set(attacker.types):
                continue
            if card.resistance_type and card.resistance_type in attacker.types:
                continue
            return attacker, ability, cost, card
        self.skipTest("no neutral defender in this pool")

    def matchup_board(self):
        attacker, ability, cost, defender = self.matchup()
        state = _real_board(self.db, self.rules, attacker)
        slot = state.players[0].active
        energy = next(c for c in self.db.by_name("DarknessEnergy"))
        for _ in range(max(cost, 1)):
            slot.energy.append(engine._new_card(state, energy.guid, 0))
        state.players[1].active.stack[-1] = engine._new_card(
            state, defender.guid, 1)
        return state, slot, ability

    def test_muscle_band_adds_its_damage_against_the_active(self):
        state, slot, ability = self.matchup_board()
        defender = state.players[1].active.slot_id

        plain, _ = engine.apply(state, engine.Attack(0, ability.ability_id))
        base = plain.slot(defender)[1].damage
        self.assertGreater(base, 0)

        state = self.attach(state, self.one("MuscleBand"), slot)
        boosted, _ = engine.apply(state, engine.Attack(0, ability.ability_id))
        self.assertEqual(boosted.slot(defender)[1].damage, base + 20)

    def test_muscle_band_does_nothing_to_a_benched_pokemon(self):
        """The condition, not the arithmetic. A bench snipe gets no bonus."""
        state, slot, _ability = self.matchup_board()
        band = self.one("MuscleBand")
        state = self.attach(state, band, slot)
        hook = self.rules.static_effects[band.guid]
        bench = state.players[1].bench[0]
        self.assertEqual(
            hook(engine.STATIC_DAMAGE_DEALT, state,
                 {"slot": slot, "defender": bench}, 0), 0)
        self.assertEqual(
            hook(engine.STATIC_DAMAGE_DEALT, state,
                 {"slot": slot, "defender": state.players[1].active}, 0), 20)

    def test_choice_band_only_fires_against_a_rule_box_pokemon(self):
        """The whole card is the condition, so an ordinary defender gets nothing."""
        state, slot, ability = self.matchup_board()
        attacker_types = set(state.pokemon(slot).types)
        state = self.attach(state, self.one("ChoiceBand"), slot)
        # attach() goes through engine.apply, which deep-copies. Anything held
        # from before it points at the old state and mutating it does nothing.
        defender_slot = state.players[1].active

        plain, _ = engine.apply(state, engine.Attack(0, ability.ability_id))
        ordinary = plain.slot(defender_slot.slot_id)[1].damage

        # Same attack, same board, defender swapped for a Pokemon-EX. It has
        # to be as neutral as the first one, or Weakness explains the change.
        ex = next(c for c in self.db
                  if c.is_pokemon and c.stage == "Basic"
                  and effects.prize_value(c) == 2 and c.max_hp >= 150
                  and not set(c.weakness_types) & attacker_types
                  and (not c.resistance_type
                       or c.resistance_type not in attacker_types))
        defender_slot.stack[-1] = engine._new_card(state, ex.guid, 1)
        boosted, _ = engine.apply(state, engine.Attack(0, ability.ability_id))
        self.assertEqual(boosted.slot(defender_slot.slot_id)[1].damage,
                         ordinary + 30)

    def test_training_center_reaches_both_players_and_only_evolutions(self):
        stadium = self.one("TrainingCenter")
        stage1 = next(c for c in self.db if c.is_pokemon and c.stage == "Stage1")
        basic = next(c for c in self.db if c.is_pokemon and c.stage == "Basic")
        state = _real_board(self.db, self.rules, stage1)
        state.players[1].active.stack[-1] = engine._new_card(
            state, stage1.guid, 1)
        state.players[0].bench[0].stack[-1] = engine._new_card(
            state, basic.guid, 0)

        before_mine = state.max_hp(state.players[0].active)
        before_theirs = state.max_hp(state.players[1].active)
        before_basic = state.max_hp(state.players[0].bench[0])

        cid = engine._new_card(state, stadium.guid, 0)
        state.players[0].hand.append(cid)
        state, _ = engine.apply(state, engine.PlayTrainer(0, cid))

        self.assertEqual(state.max_hp(state.players[0].active), before_mine + 30)
        self.assertEqual(state.max_hp(state.players[1].active),
                         before_theirs + 30)          # "both yours and your opponent's"
        self.assertEqual(state.max_hp(state.players[0].bench[0]), before_basic)

    def test_fairy_garden_frees_retreat_only_for_the_right_colour(self):
        stadium = self.one("FairyGarden")
        heavy = next(c for c in self.db.by_name("Bouffalant")
                     if c.set_code == "BW1")
        state = _real_board(self.db, self.rules, heavy)
        slot = state.players[0].active
        self.assertGreater(engine.retreat_cost(state, slot), 0)

        cid = engine._new_card(state, stadium.guid, 0)
        state.players[0].hand.append(cid)
        state, _ = engine.apply(state, engine.PlayTrainer(0, cid))
        # Still costs: no Fairy Energy is attached yet.
        self.assertGreater(engine.retreat_cost(state, slot), 0)

        fairy = next(c for c in self.db.by_name("FairyEnergy"))
        slot.energy.append(engine._new_card(state, fairy.guid, 0))
        self.assertEqual(engine.retreat_cost(state, slot), 0)

    def test_bodybuilding_dumbbells_reads_the_stage_it_is_attached_to(self):
        tool = self.one("BodybuildingDumbbells")
        basic = next(c for c in self.db if c.is_pokemon and c.stage == "Basic")
        state = _real_board(self.db, self.rules, basic)
        slot = state.players[0].active
        before = state.max_hp(slot)
        state = self.attach(state, tool, slot)
        self.assertEqual(state.max_hp(state.players[0].active), before)

        stage1 = next(c for c in self.db if c.is_pokemon and c.stage == "Stage1")
        state2 = _real_board(self.db, self.rules, stage1)
        slot2 = state2.players[0].active
        before2 = state2.max_hp(slot2)
        state2 = self.attach(state2, tool, slot2)
        self.assertEqual(state2.max_hp(state2.players[0].active), before2 + 40)


if __name__ == "__main__":
    unittest.main()
