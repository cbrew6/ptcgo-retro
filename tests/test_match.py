"""
Protocol tests for match.py.

These do not test rules - engine tests cover those. They test the parts of the
entity tree the *client* dereferences without checking, which is the category
of bug that costs the most time here: the client throws inside a Unity update
loop, the exception never reaches our socket except as a LogClientError, and
the visible symptom (blank cards, an empty hand) looks like missing art rather
than a malformed message.

Every assertion below corresponds to a specific unguarded dereference found by
decompiling the client. The comment names it, so a future change that trips the
test can be checked against the real code rather than guessed at.

Run: python -m unittest discover -s tests
"""

import json
import re
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine  # noqa: E402
import match   # noqa: E402

CARD_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "carddata")


def _attrs(entity):
    return {a["name"]: a for a in (entity.get("attributes") or [])}


def _walk(entity):
    yield entity
    for child in entity.get("children") or []:
        for e in _walk(child):
            yield e


class CardAttributeTests(unittest.TestCase):
    """What a card entity must carry to render at all."""

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _match(self):
        """A match built from two real decks, dealt but not yet set up."""
        pokemon = [c for c in self.db if c.is_pokemon and c.stage == "Basic"]
        pokemon.sort(key=lambda c: (c.set_code or "", c.collector_number or 0))
        basic = pokemon[0]
        energy = next(c for c in self.db
                      if c.is_basic_energy and c.energy_options)
        deck = [basic.guid] * 20 + [energy.guid] * 40
        return match.Match("game-1", ["acct-a", "acct-b"], self.db,
                           [deck, list(deck)], seed=7)

    def test_every_pokemon_entity_carries_its_types(self):
        """CardImageRenderer.getDefaultPerCardType does

            typeData.get_EnergyType().Value

        with no HasValue check. EnergyType is populated only from the last
        entry of attribute 200570 and only for Pokemon/LegendHalf, so a
        Pokemon without it makes .Value throw. The throw unwinds
        RefreshRequestData before getImageRequestString runs, so the card
        never requests its texture - every card in the match renders blank.
        """
        m = self._match()
        state = m.serialized_state(predeal=False)
        seen = 0
        for entity in _walk(state["entities"]):
            if entity["entityName"] != match.ENTITY_POKEMON:
                continue
            if entity.get("attributes") is None:
                continue                        # face down, and legitimately so
            at = _attrs(entity)
            self.assertIn(match.ATTR_POKEMON_TYPES, at,
                          "a face-up Pokemon with no 200570 blanks the board")
            value = at[match.ATTR_POKEMON_TYPES]["value"]
            self.assertIsInstance(value, list)
            self.assertTrue(value, "an empty array leaves EnergyType null too")
            seen += 1
        self.assertGreater(seen, 0, "the fixture revealed no Pokemon at all")

    def test_types_are_bare_strings(self):
        """200570 deserializes as PokemonTypes[] - an enum array.

        A dict, or the {"s": ...} envelope carddata uses on disk, does not
        parse into an enum, so the attribute is dropped on arrival - which is
        indistinguishable from never sending it.
        """
        m = self._match()
        for entity in _walk(m.serialized_state(predeal=False)["entities"]):
            at = _attrs(entity)
            if match.ATTR_POKEMON_TYPES not in at:
                continue
            for entry in at[match.ATTR_POKEMON_TYPES]["value"]:
                self.assertIsInstance(entry, str)

    def test_energy_provided_is_an_options_object(self):
        """201040 binds to a class whose only field is [JsonName("options")]
        PokemonTypes[][]. carddata stores it as a JSON *string*; the wire
        format wants the object itself."""
        m = self._match()
        found = 0
        for cid in m.all_cards(m.state.players[0]):
            if not m.card(cid).energy_options:
                continue
            attrs = {a["name"]: a["value"] for a in m.card_attributes(cid)}
            value = attrs.get(match.ATTR_ENERGY_PROVIDED)
            self.assertIsInstance(value, dict)
            self.assertIn("options", value)
            self.assertIsInstance(value["options"], list)
            self.assertIsInstance(value["options"][0], list)
            self.assertTrue(all(isinstance(t, str) for t in value["options"][0]))
            found += 1
        self.assertGreater(found, 0, "the fixture deck had no energy")

    def test_name_key_is_present_on_every_revealed_card(self):
        """HandSort.Compare calls GetOne<NameLookup>().Name.CompareTo(...)
        unguarded. A card without 10140 throws inside List.Sort, and
        EntityChildRenderer clears the layout *before* sorting, so the hand is
        emptied every frame and never refilled."""
        m = self._match()
        for entity in _walk(m.serialized_state(predeal=False)["entities"]):
            if entity.get("attributes") is None:
                continue
            if entity["entityName"] not in (match.ENTITY_POKEMON,
                                            match.ENTITY_TRAINER,
                                            match.ENTITY_ENERGY):
                continue
            self.assertIn(match.ATTR_NAME_KEY, _attrs(entity))


class ConditionTests(unittest.TestCase):
    """Special conditions reach the board as one whole array.

    No attack inflicts a condition yet - Rules.attack_effects is empty - so
    live play never exercises this. That is exactly why it is tested directly:
    the handler would otherwise sit dormant and unverified until the first card
    effect landed on it.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _match_with_active(self):
        basic = next(c for c in self.db if c.is_pokemon and c.stage == "Basic")
        energy = next(c for c in self.db
                      if c.is_basic_energy and c.energy_options)
        deck = [basic.guid] * 20 + [energy.guid] * 40
        m = match.Match("game-3", ["acct-a", "acct-b"], self.db,
                        [deck, list(deck)], seed=11)
        m.auto_setup()
        return m

    def test_conditions_are_sent_as_the_complete_list(self):
        m = self._match_with_active()
        slot = m.state.players[0].active
        self.assertIsNotNone(slot, "auto_setup left no Active to condition")
        slot.conditions.update({engine.ASLEEP, engine.POISONED})
        change = engine.Change(engine.CHANGE_CONDITION, player=0,
                               slot=slot.slot_id)
        messages = m.messages_for([change])
        self.assertEqual(len(messages), 1)
        name, body = messages[0]
        self.assertEqual(name, "AttributeModified")
        self.assertEqual(body["attribute"]["name"], match.ATTR_CONDITIONS)
        # Both, not just the one that changed: the attribute IS the list, so a
        # delta would silently cure everything else.
        self.assertEqual(body["attribute"]["value"], ["Asleep", "Poisoned"])

    def test_condition_names_match_the_clients_enum(self):
        """The client binds 200340 to SpecialConditions[]. A name outside the
        enum does not parse, and the attribute is dropped on arrival."""
        allowed = {"Asleep", "Burned", "Confused", "Paralyzed", "Poisoned"}
        engine_names = {engine.ASLEEP, engine.BURNED, engine.CONFUSED,
                        engine.PARALYZED, engine.POISONED}
        self.assertEqual(engine_names, allowed)

    def test_a_conditioned_pokemon_keeps_its_markers_when_reintroduced(self):
        """Introducing replaces the whole attribute map rather than merging,
        so a promoted Pokemon would otherwise be silently cured."""
        m = self._match_with_active()
        slot = m.state.players[0].active
        slot.conditions.add(engine.CONFUSED)
        attrs = {a["name"]: a["value"]
                 for a in m.card_attributes(slot.stack[-1], slot)}
        self.assertEqual(attrs.get(match.ATTR_CONDITIONS), ["Confused"])


class CardImageTests(unittest.TestCase):
    """textureLookup prefers 10020 over the padded collector number."""

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def test_product_asset_names_are_not_card_images(self):
        """A 10020 containing "/" is an absolute bundle path naming a booster
        or deck box. Sending it as a card's image would ask the asset server
        for a product texture under the card's set."""
        for card in self.db:
            if card.card_image is not None:
                self.assertNotIn("/", card.card_image)

    def test_variant_printings_keep_their_suffix(self):
        """Cards like the XY-era alternate arts are "017a", not 17. Dropping
        the suffix silently renders the plain printing instead."""
        variants = [c for c in self.db if c.card_image]
        self.assertGreater(len(variants), 0)
        self.assertTrue(any(not c.card_image.isdigit() for c in variants))


class OfferTests(unittest.TestCase):
    """Every legal move must be offered, and the reply must decode back.

    The client holds no rules and invents nothing, so an action missing from
    the offer is simply unplayable. For a long time only AttachEnergy and
    Attack were offered: a hand of Basics could not be benched, nothing
    evolved, nobody retreated, and a player whose Active was knocked out was
    sent no offer at all and sat there forever.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _match(self, basics=30, energies=30, want_bench=2):
        """A set-up match with a populated bench and Energy still in hand.

        The seed is searched for rather than fixed. A fixed one drew a hand
        with a single Basic, which left the bench empty and quietly turned two
        of these tests into assertions about nothing - the fixture has to
        guarantee the shape the test needs, or the test is only testing the
        shuffle.
        """
        basic = next(c for c in self.db
                     if c.is_pokemon and c.stage == "Basic" and c.attacks
                     and c.retreat_cost)
        energy = next(c for c in self.db
                      if c.is_basic_energy and c.energy_options)
        deck = [basic.guid] * basics + [energy.guid] * energies
        for seed in range(200):
            m = match.Match("game-4", ["acct-a", "acct-b"], self.db,
                            [deck, list(deck)], seed=seed)
            m.serialized_state(predeal=True)  # mints the entity/pile ids
            m.auto_setup()
            me = m.state.players[0]
            has_energy = any(m.state.card(c).is_energy for c in me.hand)
            if me.active is not None and len(me.bench) >= want_bench \
                    and has_energy:
                return m
        raise AssertionError("no seed produced a bench of %d with Energy in "
                             "hand" % want_bench)

    def _offer_kinds(self, m):
        _body, decode = m.build_offer(0, 1)
        return {type(next(iter(by_target.values()))).__name__
                for by_target in decode.values()}

    def test_offer_covers_the_legal_actions(self):
        """Anything legal that is not offered is unreachable in the client."""
        m = self._match()
        legal = {type(a).__name__ for a in engine.legal_actions(m.state, 0)}
        legal.discard("Pass")                 # the Next button, not a row
        legal.discard("SetupDone")
        offered = self._offer_kinds(m)
        missing = legal - offered
        self.assertFalse(missing,
                         "legal but never offered: %s" % ", ".join(sorted(missing)))

    def test_a_reply_decodes_back_to_the_action_it_named(self):
        """The reply is [[entityID, abilityID], [TargetResponse, ...]]."""
        m = self._match()
        body, decode = m.build_offer(0, 1)
        self.assertTrue(body["targetMap"], "nothing offered at all")
        for row in body["targetMap"]:
            entity = row["entityID"]
            action_id = row["selectableAction"]["actionID"]
            targets = [t for info in row["targetInfoLst"]
                       for t in info["validTargets"]]
            for target in targets:
                reply = [[entity, action_id],
                         [{"entityList": [target],
                           "name": "EntityListTargetResponse"}]]
                decoded = match.Match.decode_reply(reply, decode)
                self.assertIsNotNone(
                    decoded, "row %s/%s target %s decoded to nothing"
                    % (entity, action_id, target))
                self.assertIs(decoded, decode[(entity, action_id)][target])

    def test_the_chosen_target_is_honoured(self):
        """One row can stand for several Actions differing only by target.

        Attaching an Energy to any of six Pokemon is one row with six targets.
        Ignoring the target and applying an arbitrary one of them is how this
        used to put the Energy on whichever Pokemon the engine listed first.
        """
        m = self._match()
        _body, decode = m.build_offer(0, 1)
        multi = [(key, by_target) for key, by_target in decode.items()
                 if len(by_target) > 1]
        self.assertTrue(multi, "no multi-target row to test with")
        (entity, action_id), by_target = multi[0]
        for target, expected in by_target.items():
            reply = [[entity, action_id],
                     [{"entityList": [target], "name": "EntityListTargetResponse"}]]
            self.assertIs(match.Match.decode_reply(reply, decode), expected)

    def test_an_unknown_reply_is_refused_rather_than_guessed(self):
        m = self._match()
        _body, decode = m.build_offer(0, 1)
        self.assertIsNone(match.Match.decode_reply(None, decode))
        self.assertIsNone(match.Match.decode_reply(
            [["no-such-entity", "no-such-action"], []], decode))
        # Malformed input must not raise: it comes from off-machine, and an
        # exception here takes the match down where a refusal just re-offers.
        for junk in ([], [[]], "x", [[None, None], None], [[1]], {}):
            self.assertIsNone(match.Match.decode_reply(junk, decode))

    def test_rows_for_one_entity_share_a_selection_type(self):
        """An entity whose rows mix "Ability" and "AbilitySelection" lands in a
        client fallback that draws no UI at all."""
        m = self._match()
        body, _decode = m.build_offer(0, 1)
        kinds = {}
        for row in body["targetMap"]:
            kinds.setdefault(row["entityID"], set()).add(
                row["selectableAction"]["selectionType"])
        for entity, seen in kinds.items():
            self.assertEqual(len(seen), 1,
                             "entity %s mixes %s" % (entity, sorted(seen)))

    def test_a_promotion_is_forced_and_still_offered(self):
        """With no Active the engine offers only Promote. Suppressing that
        offer - which the server used to do - hangs the match for good."""
        m = self._match()
        me = m.state.players[0]
        self.assertTrue(me.bench, "fixture put nothing on the bench")
        # As a knockout leaves it. Clearing active alone is not enough: the
        # engine records the debt in pending_promotions, and that is what
        # makes Promote the only legal move.
        me.active = None
        m.state.pending_promotions.append(0)
        self.assertEqual(engine.players_to_act(m.state), [0])
        body, decode = m.build_offer(0, 1)
        kinds = {type(next(iter(v.values()))).__name__ for v in decode.values()}
        self.assertEqual(kinds, {"Promote"})
        self.assertTrue(body["forced"],
                        "an unforced offer gives an end-turn button that "
                        "escapes a promotion the rules say is owed")
        self.assertTrue(body["targetMap"])

    def test_no_offer_row_carries_a_null_target_list(self):
        """validTargets has .Length read on it directly."""
        m = self._match()
        body, _decode = m.build_offer(0, 1)
        for row in body["targetMap"]:
            self.assertIsInstance(row["targetInfoLst"], list)
            for info in row["targetInfoLst"]:
                self.assertIsInstance(info["validTargets"], list)
                self.assertTrue(info["validTargets"])


class LocalizationKeyTests(unittest.TestCase):
    """Every key we send must exist in the client's shipped string DB.

    A missing key is not an error client-side - L.LT returns the key itself -
    so the UI simply displays "playmat.prompt.yourturn" and looks broken. That
    exact string is what the player saw on screen, and it was a key this server
    invented. The keys are checked here rather than trusted because inventing
    one is silent, easy, and indistinguishable from a hang.
    """

    @classmethod
    def setUpClass(cls):
        import server
        cls.keys = {e["key"] for e in server.load_localization()}
        if not cls.keys:
            raise unittest.SkipTest("no localization DB on this machine")

    def _sent_keys(self):
        """Every localization key literal in the modules that send prompts.

        Both namespaces matter: playmat.* is the client's own, and
        com.direwolfdigital.cake.rules.* is the ORIGINAL SERVER's, still
        present in the shipped DB - which makes it direct evidence of what the
        real server sent rather than something to guess at.
        """
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = r'"((?:playmat|com\.direwolfdigital\.cake)[A-Za-z0-9_.]+)"'
        found = set()
        for name in ("match.py", "server.py"):
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                found.update(re.findall(pattern, fh.read()))
        # Entity class names share the com.direwolfdigital prefix but are Java
        # FQCNs, not localization keys.
        return {k for k in found if ".entities." not in k
                and ".game.core." not in k}

    def test_every_prompt_key_we_send_really_exists(self):
        sent = self._sent_keys()
        self.assertTrue(sent, "found no prompt keys to check - regex stale?")
        missing = sorted(k for k in sent if k not in self.keys)
        self.assertFalse(
            missing,
            "these render as raw text in the UI: %s" % ", ".join(missing))


class SerializedStateTests(unittest.TestCase):
    """Structural rules the client's Entities.initialize depends on."""

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _match(self):
        basic = next(c for c in self.db if c.is_pokemon and c.stage == "Basic")
        energy = next(c for c in self.db
                      if c.is_basic_energy and c.energy_options)
        deck = [basic.guid] * 20 + [energy.guid] * 40
        return match.Match("game-2", ["acct-a", "acct-b"], self.db,
                           [deck, list(deck)], seed=3)

    def test_children_is_never_null(self):
        """Entities.initialize reads .Length on children directly."""
        for entity in _walk(self._match().serialized_state(predeal=False)["entities"]):
            self.assertIsInstance(entity.get("children"), list)

    def test_bench_declares_its_slot_count(self):
        """BenchLayout divides by attribute 201920. Zero or absent gives NaN
        positions and the bench renders off-screen."""
        benches = 0
        for entity in _walk(self._match().serialized_state(predeal=False)["entities"]):
            at = _attrs(entity)
            name = at.get(match.ATTR_NAME_KEY, {}).get("value")
            if isinstance(name, dict) and name.get("id") == match.ZONE_BENCH:
                self.assertEqual(at.get(match.ATTR_BENCH_SLOTS, {}).get("value"), 5)
                benches += 1
        self.assertEqual(benches, 2)

    def test_the_whole_state_is_json_serializable(self):
        json.dumps(self._match().serialized_state())


if __name__ == "__main__":
    unittest.main()
