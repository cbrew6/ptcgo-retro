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

    def test_a_foil_card_says_so_on_the_playmat(self):
        """Foils rendered everywhere except in a match, and this is why.

        CardImageRenderer asks for a foil mask only when the entity's own art
        data reports IsFoil, and IsFoil is computed from attribute 200620 (the
        mask) and 200610 (the effect) and nothing else. Collection and deck
        views build their cards from the local archetype DB, which carries
        both. A match entity carries only what card_attributes() sends, so
        every card on the board claimed to be non-foil - no mask was ever
        requested, and no error was logged either. Same shape as 200570 and
        200740 before it: an attribute the renderer reads and we never sent.
        """
        db = engine.CardDB.from_directory(CARD_DIR)
        foils = [c for c in db if c.foil_mask or c.foil_effect]
        self.assertGreater(len(foils), 1000,
                           "carddata itself has no foil attributes - the "
                           "export, not the wire format, is what broke")
        m = self._match()
        seen = 0
        for cid in m.all_cards(m.state.players[0]):
            card = m.card(cid)
            if not (card.foil_mask or card.foil_effect):
                continue
            attrs = {a["name"]: a["value"] for a in m.card_attributes(cid)}
            if card.foil_mask:
                self.assertEqual(attrs.get(match.ATTR_FOIL_MASK),
                                 card.foil_mask)
            if card.foil_effect:
                self.assertEqual(attrs.get(match.ATTR_FOIL_EFFECT),
                                 card.foil_effect)
            seen += 1
        self.assertGreater(seen, 0, "no foil card in the fixture deck")

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

    def test_retreat_is_offered_on_the_active(self):
        """Retreat has exactly one home: the Active, described "BaseRetreat".

        The client finds retreat only by asking the ACTIVE for an action whose
        Description is "BaseRetreat" (SelectableActionUtil.IsRetreat, and every
        caller of it). Hanging the row off the bench Pokemon being switched to
        is well-formed, decodes correctly, and is completely invisible - there
        was no way to retreat at all, and no test noticed because none of them
        asked WHERE the row was.
        """
        m = self._match()
        me = m.state.players[0]
        # Retreat costs Energy, so an Active with none attached cannot retreat
        # and would make this test assert on an empty list.
        cost = engine.retreat_cost(m.state, me.active)
        self.assertGreater(cost, 0, "fixture Pokemon retreats for free")
        paid = 0
        for cid in list(me.hand):
            if paid >= cost:
                break
            if m.state.card(cid).is_energy:
                me.hand.remove(cid)
                me.active.energy.append(cid)
                paid += m.state.card(cid).energy_units
        self.assertGreaterEqual(paid, cost, "fixture had too little Energy")
        self.assertTrue(me.bench and any(s.stack for s in me.bench),
                        "fixture put nothing on the bench to retreat into")

        body, _decode = m.build_offer(0, 1)
        active = m.entity_of_slot(me.active)
        rows = [r for r in body["targetMap"]
                if r["selectableAction"]["description"] == "BaseRetreat"]
        self.assertEqual(len(rows), 1,
                         "expected exactly one retreat row, got %d" % len(rows))
        row = rows[0]
        self.assertEqual(row["entityID"], active,
                         "retreat must hang off the Active, not %s"
                         % row["entityID"])
        # Sits in the same node as the attacks; CreateButtons pulls it out by
        # description before it ever looks for a PieAbilityDescription, so it
        # needs no entry in attribute 200740.
        self.assertEqual(row["selectableAction"]["selectionType"],
                         "AbilitySelection")
        # Pay the cost, then choose who comes in - in that order, because the
        # second TargetInformation becomes a CHILD of the first.
        # Destination first, cost tray LAST: only the final node in a chain
        # has NodeToAdvanceTo() == null, and only that node's confirm button
        # is ever active.
        self.assertEqual([i["name"] for i in row["targetInfoLst"]],
                         ["RetreatNewActiveTargetInformation",
                          "RetreatCostEntityListTargetInformation"])
        tray = row["targetInfoLst"][1]
        # valueToSelect is the cost in Energy SYMBOLS - the node tallies
        # get_EnergyProvidedCount(), so a card count here would let a Double
        # Colorless underpay a two-cost retreat.
        self.assertEqual(tray["valueToSelect"], cost)
        self.assertTrue(tray["forced"],
                        "an unforced tray is satisfied by selecting nothing, "
                        "which retreats without paying")
        self.assertEqual(sorted(tray["validTargets"]),
                         sorted(m.eid(c) for c in me.active.energy))
        # The destinations are the benched Pokemon, which is what the player
        # picks - retreat names the Pokemon coming IN.
        bench = {m.entity_of_slot(sl) for sl in me.bench if sl.stack}
        offered = set(row["targetInfoLst"][0]["validTargets"])
        self.assertTrue(offered, "retreat offered with nowhere to go")
        self.assertTrue(offered <= bench,
                        "retreat targets something that is not a benched "
                        "Pokemon: %s" % sorted(offered - bench))

    def test_the_energy_the_player_picks_is_what_gets_discarded(self):
        """The pip tray must not be theatre.

        The offer holds one legal Retreat per destination with a payment the
        server chose, so a reply that ignored the tray would still be legal -
        and would discard Energy the player did not pick. _do_retreat
        validates the payment, so honouring the choice is safe: a bad one is
        refused and re-offered rather than quietly applied.
        """
        m = self._match()
        me = m.state.players[0]
        cost = engine.retreat_cost(m.state, me.active)
        # Two different Energy attached, so "which one" is a real question.
        # Off the deck, not the hand: the fixture's hand holds too few to
        # leave a spare, and a spare is the whole point of the test.
        moved = [c for c in list(me.deck) if m.state.card(c).is_energy][:cost + 1]
        self.assertGreater(len(moved), cost, "need a spare Energy to choose")
        for cid in moved:
            me.deck.remove(cid)
            me.active.energy.append(cid)

        body, decode = m.build_offer(0, 1)
        row = next(r for r in body["targetMap"]
                   if r["selectableAction"]["description"] == "BaseRetreat")
        destination = row["targetInfoLst"][0]["validTargets"][0]
        chosen = moved[-1:]                      # the LAST one, deliberately
        reply = [[row["entityID"], row["selectableAction"]["actionID"]],
                 [{"entityList": [destination],
                   "name": "EntityListTargetResponse"},
                  {"entityList": [m.eid(c) for c in chosen],
                   "name": "EntityListTargetResponse"}]]
        action = match.Match.decode_reply(reply, decode)
        self.assertIsInstance(action, engine.Retreat)
        self.assertEqual(list(action.energy), chosen,
                         "the tray's answer was ignored")
        # And the engine accepts it, which is what makes it a real payment.
        state, _changes = engine.apply(m.state, action)
        self.assertNotIn(chosen[0], state.players[0].active.energy)

    def test_a_promotion_uses_its_own_selection_not_an_action_offer(self):
        """CheckShouldEndTurn (pie_d.cs:31489) dereferences the Active's first
        child with no guard, and an empty Active is exactly the state a
        promotion is asked in - so an action offer there throws in the client.
        The knockout selection kind exists for this."""
        m = self._match()
        me = m.state.players[0]
        self.assertTrue(me.bench, "fixture put nothing on the bench")
        # As a knockout leaves it. Clearing active alone is not enough: the
        # engine records the debt in pending_promotions, and that is what
        # makes Promote the only legal move.
        me.active = None
        m.state.pending_promotions.append(0)
        self.assertEqual(engine.players_to_act(m.state), [0])

        body, slots = m.promote_selection(0, 1)
        self.assertEqual(len(slots), len(me.bench))
        infos = next(iter(body["targetMap"].values()))
        self.assertEqual([i["name"] for i in infos],
                         ["KnockoutPokemonTargetInformation"])
        self.assertTrue(body["forced"], "a new Active cannot be declined")
        self.assertEqual(infos[0]["minimumToSelect"], 1)

    def test_the_promotion_reply_names_a_bench_slot(self):
        m = self._match()
        me = m.state.players[0]
        me.active = None
        m.state.pending_promotions.append(0)
        body, slots = m.promote_selection(0, 1)
        entity = next(iter(body["targetMap"].values()))[0]["validTargets"][0]
        reply = {"entityID": "x", "targetResponses": [
            {"name": "EntityListTargetResponse", "entityList": [entity]}]}
        self.assertEqual(m.decode_promote_reply(reply, slots), slots[0])
        self.assertIsNone(m.decode_promote_reply(None, slots))

    def test_an_action_offer_is_never_forced(self):
        """forced:true makes MayCancel false, which is the only way to caption
        the button "End Turn" - but it makes MayAdvance false too, so the
        button is drawn and does nothing. That is a soft lock, not a stricter
        prompt."""
        m = self._match()
        body, _decode = m.build_offer(0, 1)
        self.assertFalse(body["forced"])

    def test_no_offer_row_carries_a_null_target_list(self):
        """validTargets has .Length read on it directly."""
        m = self._match()
        body, _decode = m.build_offer(0, 1)
        for row in body["targetMap"]:
            self.assertIsInstance(row["targetInfoLst"], list)
            for info in row["targetInfoLst"]:
                self.assertIsInstance(info["validTargets"], list)
                self.assertTrue(info["validTargets"])


class SetupSelectionTests(unittest.TestCase):
    """The setup screen has to stay answerable with any legal opening hand."""

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _match_with_basics(self, wanted):
        """A dealt match whose opening hand holds exactly `wanted` Basics."""
        basic = next(c for c in self.db
                     if c.is_pokemon and c.stage == "Basic" and c.attacks)
        energy = next(c for c in self.db
                      if c.is_basic_energy and c.energy_options)
        # Few Basics in the list makes a one-Basic hand common; the engine
        # mulligans until there is at least one, so zero cannot occur.
        deck = [basic.guid] * 6 + [energy.guid] * 54
        for seed in range(400):
            m = match.Match("game-5", ["acct-a", "acct-b"], self.db,
                            [deck, list(deck)], seed=seed)
            m.serialized_state(predeal=True)
            hand = m.state.players[0].hand
            if sum(1 for c in hand if m.card(c).is_basic_pokemon) == wanted:
                return m
        raise AssertionError("no seed dealt exactly %d Basics" % wanted)

    def test_one_basic_offers_no_bench_step(self):
        """The bug the player hit.

        With a single Basic in hand, that card becomes the Active and there is
        nothing left to bench. Offering the bench step anyway is a dead end:
        it lights up, its only candidate is the card that just became Active,
        and the hand has no Pokemon in it. A chain with nothing after it
        advances straight to the reply instead.
        """
        m = self._match_with_basics(1)
        body, basics = m.setup_selection(0, 1)
        self.assertEqual(len(basics), 1)
        infos = next(iter(body["targetMap"].values()))
        self.assertEqual([i["name"] for i in infos],
                         ["ActivePokemonTargetInformation"])

    def test_the_bench_is_never_chained_onto_the_active(self):
        """Finishing a chained InitialBenchedTargetInformation needs the
        client's own Done button, and a player who does not get one is frozen
        with a lit bench and no way forward. Benching is asked separately, as
        clickable rows, so the Active always resolves on the drag."""
        # 1 and 2 are what the sparse fixture deck reliably deals; the shape
        # of the node list does not depend on how many there are.
        for wanted in (1, 2):
            m = self._match_with_basics(wanted)
            body, _ = m.setup_selection(0, 1)
            infos = next(iter(body["targetMap"].values()))
            self.assertEqual([i["name"] for i in infos],
                             ["ActivePokemonTargetInformation"])

    def test_exactly_one_target_map_key_always(self):
        """ignoreFirst makes the client throw on anything but one key."""
        for wanted in (1, 2, 3):
            m = self._match_with_basics(wanted)
            body, _ = m.setup_selection(0, 1)
            self.assertEqual(len(body["targetMap"]), 1)
            self.assertTrue(body["ignoreFirst"])

    def test_a_one_node_reply_still_decodes(self):
        m = self._match_with_basics(1)
        _body, basics = m.setup_selection(0, 1)
        reply = {"entityID": "whatever", "targetResponses": [
            {"name": "EntityListTargetResponse",
             "entityList": [m.eid(basics[0])]}]}
        active, bench = m.decode_setup_reply(reply, basics)
        self.assertEqual(active, basics[0])
        self.assertEqual(bench, [])

    def test_the_active_is_never_also_benched(self):
        """The client has been seen echoing the Active back in the bench list."""
        m = self._match_with_basics(2)
        _body, basics = m.setup_selection(0, 1)
        reply = {"entityID": "whatever", "targetResponses": [
            {"name": "EntityListTargetResponse",
             "entityList": [m.eid(basics[0])]},
            {"name": "EntityListTargetResponse",
             "entityList": [m.eid(basics[0]), m.eid(basics[1])]}]}
        active, bench = m.decode_setup_reply(reply, basics)
        self.assertEqual(active, basics[0])
        self.assertEqual(bench, [basics[1]])


class AttackSequenceTests(unittest.TestCase):
    """The Attack sequence's first statement is unguarded."""

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _attacking_match(self):
        """A match with both Actives in place and an attack resolved."""
        basic = next(c for c in self.db
                     if c.is_pokemon and c.stage == "Basic" and c.attacks
                     and c.max_hp >= 60)
        energy = next(c for c in self.db
                      if c.is_basic_energy and c.energy_options)
        deck = [basic.guid] * 20 + [energy.guid] * 40
        m = match.Match("game-8", ["acct-a", "acct-b"], self.db,
                        [deck, list(deck)], seed=9)
        m.serialized_state(predeal=True)
        m.auto_setup()
        return m

    def test_the_playmat_names_the_attacker_before_the_sequence(self):
        """M.N.executeSequence opens with

            All[Playmat.GetAttribute(201870).Value[0]]

        and no guard, so a playmat without that attribute throws a
        NullReferenceException out of the sequence - which escapes the message
        pump coroutine and kills it for the rest of the game. Every later
        message then piles up unprocessed: the board stops accepting clicks
        and conceding does nothing.
        """
        m = self._attacking_match()
        attacker = m.state.players[0].active
        defender = m.state.players[1].active
        self.assertIsNotNone(attacker)
        self.assertIsNotNone(defender)
        card = m.card(attacker.stack[-1])
        attack = card.attacks[0]

        items = m.animation_for([
            engine.Change(engine.CHANGE_ATTACK, player=0,
                          slot=attacker.slot_id,
                          detail={"abilityID": attack.ability_id,
                                  "title": attack.title,
                                  "baseDamage": attack.damage}),
            engine.Change(engine.CHANGE_DAMAGE, player=1,
                          slot=defender.slot_id, amount=30,
                          detail={"abilityID": attack.ability_id,
                                  "baseDamage": attack.damage}),
        ])
        kinds = [(kind, name) for kind, name, _ in items]
        self.assertIn(("seq", "Attack"), kinds)
        index = kinds.index(("seq", "Attack"))
        self.assertGreater(index, 0, "nothing was sent before the sequence")

        before = items[index - 1]
        self.assertEqual((before[0], before[1]), ("msg", "AttributeModified"))
        self.assertEqual(before[2]["entityID"], m.playmat_id,
                         "the attribute belongs to the PLAYMAT")
        self.assertEqual(before[2]["attribute"]["name"],
                         match.ATTR_ABILITY_SOURCE)
        value = before[2]["attribute"]["value"]
        self.assertIsInstance(value, list)
        self.assertTrue(value, "[0] is read directly; an empty list throws too")
        self.assertEqual(value[0], m.entity_of_slot(attacker))


class CoinFlipTests(unittest.TestCase):
    """The opening flip, and why it cannot be sent before the board."""

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _match(self):
        basic = next(c for c in self.db if c.is_pokemon and c.stage == "Basic")
        energy = next(c for c in self.db
                      if c.is_basic_energy and c.energy_options)
        deck = [basic.guid] * 20 + [energy.guid] * 40
        m = match.Match("game-6", ["acct-a", "acct-b"], self.db,
                        [deck, list(deck)], seed=2)
        m.serialized_state(predeal=True)
        return m

    def test_the_flip_names_an_entity_the_client_already_has(self):
        """MultipleCoinFlipWithContextEffect's command constructor does
        All.get_Item(source) with no guard, so an unknown id throws a
        KeyNotFoundException inside the message pump."""
        m = self._match()
        for winner in (0, 1):
            items = m.coin_flip_items(winner, heads=(winner == 0))
            effect = items[0][2][0][2]["effectMessage"]["value"]
            self.assertIn(effect["source"], m.known)

    def test_heads_is_zero(self):
        """get_Result() reads resultLst[0]: 0 is heads, anything else tails."""
        m = self._match()
        self.assertEqual(
            m.coin_flip_items(0, heads=True)[0][2][0][2]
            ["effectMessage"]["value"]["resultLst"], [0])
        self.assertNotEqual(
            m.coin_flip_items(1, heads=False)[0][2][0][2]
            ["effectMessage"]["value"]["resultLst"], [0])

    def test_it_is_wrapped_in_initialcoinflip(self):
        """That sequence is what pushes one result onto BOTH coin animators."""
        m = self._match()
        items = m.coin_flip_items(0, heads=True)
        self.assertEqual(items[0][0], "seq")
        self.assertEqual(items[0][1], "InitialCoinFlip")


class MulliganDrawTests(unittest.TestCase):
    """Compensation is offered, not taken."""

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _match(self, owed):
        """A match with `owed` mulligan offers outstanding."""
        basic = next(c for c in self.db if c.is_pokemon and c.stage == "Basic")
        energy = next(c for c in self.db
                      if c.is_basic_energy and c.energy_options)
        deck = [basic.guid] * 20 + [energy.guid] * 40
        m = match.Match("game-7", ["acct-a", "acct-b"], self.db,
                        [deck, list(deck)], seed=5)
        m.serialized_state(predeal=True)
        m.state.players[0].owed_draws = owed
        m.state.players[0].owed_draws_total = owed
        return m

    def test_the_question_is_numbered(self):
        """The original asked once per mulligan, numbered - the shipped prompt
        has a "{0}" and a separate .drawmultiple key. A 0..N count list was
        this project's invention."""
        m = self._match(3)
        m.state.players[0].owed_draws_total = 3
        body, owed = m.mulligan_selection(0, 1)
        self.assertEqual(owed, 3)
        self.assertIn("drawmultiple", body["prompt"]["id"])
        self.assertEqual(body["prompt"]["textVars"]["numberMap"]["{0}"],
                         m.state.players[0].mulligan_draw_number)

    def test_a_single_mulligan_is_a_yes_no_question(self):
        m = self._match(1)
        body, owed = m.mulligan_selection(0, 1)
        self.assertEqual(owed, 1)
        self.assertEqual(len(body["buttons"]), 2)      # No, then Yes
        self.assertIn("mulliganchoicechoice2", body["buttons"][0]["id"])
        self.assertIn("mulliganchoicechoice1", body["buttons"][1]["id"])

    def test_many_mulligans_are_still_four_buttons(self):
        """23 mulligans is a real opening, so the dialog must not grow with
        the count. The original answered that with "Yes/No to rest (N)",
        which is why the shipped DB carries those two button keys."""
        m = self._match(40)
        body, owed = m.mulligan_selection(0, 1)
        self.assertEqual(owed, 40)
        self.assertEqual(len(body["buttons"]), 4)
        self.assertIn("drawnonebutton", body["buttons"][2]["id"])
        self.assertIn("drawallbutton", body["buttons"][3]["id"])
        # The "rest" count is substituted, not left as a literal "{0}".
        self.assertEqual(body["buttons"][3]["textVars"]["numberMap"]["{0}"], 40)

    def test_the_last_mulligan_drops_the_rest_buttons(self):
        m = self._match(1)
        body, _ = m.mulligan_selection(0, 1)
        self.assertEqual(len(body["buttons"]), 2)


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
        # Lowercased, because the shipped DB is entirely lowercase and both
        # L.LT and LocalizableText.HasId compare case-insensitively. Some ids
        # must be sent in their original mixed case regardless - the client
        # matches PiePromptListener.suppressedKeys against the id it was given.
        cls.keys = {e["key"].lower() for e in server.load_localization()}
        if not cls.keys:
            raise unittest.SkipTest("no localization DB on this machine")

    def _sent_keys(self):
        """Every localization key the prompt-sending modules can emit.

        Two sources, because neither alone is enough. Module constants are
        read from the imported modules rather than the source, so a key built
        by concatenation - PROMPT_MULLIGAN_DRAW + ".drawmultiple" - is checked
        as the key it actually becomes rather than as two half-keys. Inline
        literals are still regexed out of the source, since plenty of prompts
        are written at the call site.

        Both namespaces matter: playmat.* is the client's own, and
        com.direwolfdigital.cake.rules.* is the ORIGINAL SERVER's, still
        present in the shipped DB - direct evidence of what was really sent.
        """
        import server
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = r'"((?:playmat|com\.direwolfdigital\.cake)[A-Za-z0-9_.]+)"'
        found = set()
        for name in ("match.py", "server.py"):
            with open(os.path.join(here, name), encoding="utf-8") as fh:
                found.update(re.findall(pattern, fh.read()))

        def harvest(value):
            if isinstance(value, str):
                found.add(value)
            elif isinstance(value, dict):
                for item in value.values():
                    harvest(item)

        for module in (match, server):
            for name in dir(module):
                if name.startswith(("PROMPT_", "BUTTON_", "CHOICE_PROMPT")):
                    harvest(getattr(module, name))

        # Entity class names share the com.direwolfdigital prefix but are Java
        # FQCNs, not localization keys. An empty prompt is deliberate - it is
        # how a banner is suppressed - and has nothing to look up.
        return {k for k in found
                if k and ".entities." not in k and ".game.core." not in k}

    def test_every_prompt_key_we_send_really_exists(self):
        sent = self._sent_keys()
        self.assertTrue(sent, "found no prompt keys to check - regex stale?")
        missing = sorted(k for k in sent if k.lower() not in self.keys)
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


class DeckCosmeticsTests(unittest.TestCase):
    """Sleeve, coin and deck box reach a match only through gameOptions.

    They are not attributes on any entity and not part of the board. N.d reads
    them out of MatchFound.gameOptions keyed by ACCOUNT, and getSettingArchetype
    drops any value that is not exactly 36 characters. With none present the
    client falls through to "_default_sleeve" without logging anything, which
    is why a chosen sleeve never appeared on the playmat.
    """

    ACCOUNT = "48b7b3bc-c270-4a78-a3f7-9d735f4104ae"
    DECK = {
        "deckID": "d0fba67a-d11a-459f-90f9-b94c643d922b",
        "attributes": [
            {"name": 200670, "value": "b9a697c4-949e-11e1-890f-efb676c7909c"},
            {"name": 200690, "value": "e2cae5a3-f184-4040-a364-766355305072"},
            {"name": 200680, "value": "0aabe654-9377-4d69-bd9b-f875fcb1bb21"},
            {"name": 10860, "value": ["Standard", "Modified"]},
        ],
    }

    def test_the_three_cosmetics_are_sent_keyed_by_account(self):
        import server
        extras = server.game_extras(self.ACCOUNT, self.DECK)
        self.assertEqual(extras, {
            "gameExtrasSleeve_%s" % self.ACCOUNT:
                "0aabe654-9377-4d69-bd9b-f875fcb1bb21",
            "gameExtrasCoin_%s" % self.ACCOUNT:
                "b9a697c4-949e-11e1-890f-efb676c7909c",
            "gameExtrasDeckBox_%s" % self.ACCOUNT:
                "e2cae5a3-f184-4040-a364-766355305072",
        })

    def test_every_value_is_a_bare_36_character_guid(self):
        """getSettingArchetype tests `value.Length == 36` and drops anything
        else, so a quoted or braced GUID is silently no sleeve at all."""
        import server
        for key, value in server.game_extras(self.ACCOUNT, self.DECK).items():
            self.assertEqual(len(value), 36, key)
            self.assertNotIn('"', value)
            self.assertNotIn("{", value)

    def test_a_deck_without_cosmetics_sends_nothing_rather_than_junk(self):
        import server
        self.assertEqual(server.game_extras(self.ACCOUNT, {}), {})
        self.assertEqual(server.game_extras(None, self.DECK), {})
        # A non-GUID value is dropped here rather than by the client.
        odd = {"attributes": [{"name": 200680, "value": "not-a-guid"},
                              {"name": 200670, "value": None}]}
        self.assertEqual(server.game_extras(self.ACCOUNT, odd), {})


class ChangeCoverageTests(unittest.TestCase):
    """Every engine Change kind is either animated or deliberately not.

    animation_for does `getattr(self, "_change_" + kind, None)` and `continue`s
    when there is none - no warning, nothing logged. So a change the client
    needs to see is dropped in complete silence, the server's board stays
    right, and every other test still passes. That is exactly how promote and
    retreat both shipped: the engine swapped the Pokemon and the client was
    never told, so a knocked-out Active was never replaced on screen and a
    retreat paid its cost without anything moving.

    The allow-list below is the point of this test. A new Change kind fails it
    until someone decides which side it belongs on.
    """

    #: Kinds with no animation of their own, and why.
    NOT_ANIMATED = {
        "choice": "a question, asked through its own selection message",
        "chose": "the answer to one; the effects it causes are their own changes",
        "phase": "bookkeeping - no board state changes",
        "turnEnd": "the client's own banner is driven by turnStart",
        "gameOver": "sent as GameCompletedMessage, which must stay unwrapped",
        "modifier": "continuous effects have no card movement to show",
        "mulligan": "shown by mulligan_items as a MulliganRevealCardsEffect",
        "shuffle": "the opening emits its own Shuffled messages",
        "coinFlip": "in-effect flips ride along with the effect that caused them",
        "evolve": "the card's own CHANGE_MOVE from hand to the slot animates it",
    }

    def test_every_change_kind_is_accounted_for(self):
        kinds = {value for name, value in vars(engine).items()
                 if name.startswith("CHANGE_") and isinstance(value, str)}
        self.assertTrue(kinds, "found no CHANGE_ constants at all")
        for kind in sorted(kinds):
            handled = hasattr(match.Match, "_change_" + kind)
            excused = kind in self.NOT_ANIMATED
            self.assertTrue(
                handled or excused,
                "engine emits CHANGE_%s and match.py neither animates it nor "
                "lists it in NOT_ANIMATED - animation_for will drop it "
                "silently" % kind.upper())
            self.assertFalse(
                handled and excused,
                "%r is both animated and listed as not animated" % kind)

    def test_the_allow_list_has_not_gone_stale(self):
        """A kind that stops existing should not linger as an excuse."""
        kinds = {value for name, value in vars(engine).items()
                 if name.startswith("CHANGE_") and isinstance(value, str)}
        for kind in self.NOT_ANIMATED:
            self.assertIn(kind, kinds,
                          "NOT_ANIMATED lists %r, which the engine no longer "
                          "emits" % kind)


class IntroduceShapeTests(unittest.TestCase):
    """EntityIntroduced must never carry a null attributeMap.

    Its command constructor is `new MutableAttributes(message.AttributeMap)`,
    taken straight off the message, so a null map throws inside the client's
    message translator and it reports "Error translating MessageCommand
    dwd.core.match.messages.EntityIntroduced". Nothing recovers from that.

    `attributes: null` IS how the SerializedGameState tree says face down, and
    conflating the two is what broke hidden setup: a face-down card is one
    that has simply never been introduced, so you move it and send no
    introduction at all.
    """

    @classmethod
    def setUpClass(cls):
        cls.db = engine.CardDB.from_directory(CARD_DIR)

    def _messages(self):
        pokemon = [c for c in self.db if c.is_pokemon and c.stage == "Basic"
                   and c.attacks]
        pokemon.sort(key=lambda c: (c.set_code or "", c.collector_number or 0))
        energy = next(c for c in self.db if c.is_basic_energy and c.energy_options)
        deck = [pokemon[0].guid] * 20 + [energy.guid] * 40
        m = match.Match("g-intro", ["a", "b"], self.db, [deck, list(deck)],
                        seed=11)
        m.serialized_state(predeal=True)
        out = list(_flatten_items(m.opening_animation()))
        m.auto_setup()
        out += list(_flatten_items(m.reveal_setup_items(1)))
        return out

    def test_no_introduction_has_a_null_attribute_map(self):
        seen = 0
        for name, body in self._messages():
            if name != "EntityIntroduced":
                continue
            seen += 1
            self.assertIsNotNone(
                body.get("attributeMap"),
                "EntityIntroduced with a null attributeMap throws in the "
                "client's message translator")
            self.assertIsNotNone(body.get("entityName"),
                                 "a null entityName throws in IntroduceEntity")
        self.assertGreater(seen, 0, "no introductions to check")


def _flatten_items(items):
    """emit_items form -> (name, body) pairs, sequences included."""
    for item in items or []:
        if not isinstance(item, tuple):
            continue
        if item[0] == "seq":
            for inner in _flatten_items(item[2]):
                yield inner
        elif item[0] == "msg":
            yield item[1], item[2]
        elif len(item) == 2:
            yield item[0], item[1]


class StaticHandlerTests(unittest.TestCase):
    """Handlers that answer from constants must actually run.

    `import server` proves nothing about them: a handler referencing a class
    attribute that no longer exists imports fine and raises AttributeError at
    the moment the client asks. That is not a quiet failure either - the
    exception unwinds out of GameSession.run and the socket is dropped, so the
    player sees "Connection to server has been lost" and cannot launch. That
    shipped once, from a constant deleted during an edit while the handler
    that used it stayed behind.

    Every handler here replies from constants, so calling it needs no match,
    no deck and no login.
    """

    HANDLERS = [
        ("on_GetDynamicPages", "DynamicLandingPages"),
        ("on_GetDynamicVersions", "DynamicVersions"),
        ("on_GetThemeDeckContents", "ThemeDeckContentsMap"),
        ("on_GetArchetypeCorrections", "ArchetypeCorrections"),
        ("on_GetAllBannedCardsByFormats", "AllBannedCardsByFormat"),
        ("on_ViewMyLots", "MyLotsRetrieved"),
    ]

    def _session(self):
        import server

        class Recording(server.GameSession):
            def __init__(self):
                self.sent = []

            def send(self, name, body, request_id=None):
                self.sent.append((name, body))

        return Recording()

    def test_every_static_handler_answers(self):
        for method, expected in self.HANDLERS:
            session = self._session()
            handler = getattr(session, method, None)
            self.assertIsNotNone(handler, "%s no longer exists" % method)
            handler({}, 1)
            self.assertTrue(session.sent, "%s sent nothing" % method)
            name, body = session.sent[0]
            self.assertEqual(name, expected, "%s replied with %s" % (method, name))
            self.assertIsInstance(body, dict)

    def test_the_landing_page_is_not_empty(self):
        """An empty pageData is a black home screen, which is what it was."""
        session = self._session()
        session.on_GetDynamicPages({}, 1)
        _name, body = session.sent[0]
        pages = body["pageData"]
        self.assertTrue(pages, "no landing page items: the home screen is black")
        for page in pages:
            # Read directly by the client, so none of these may be null.
            for field in ("labels", "images", "actions"):
                self.assertIsInstance(page[field], dict)
            self.assertTrue(page["images"], "an item with no images shows nothing")
            for slot, image in page["images"].items():
                self.assertIn("en_US", image["localeImageMap"], slot)
            # "Inactive" is the sentinel DynamicTemplate skips. Any other value
            # goes to Resources.Load and is dereferenced unguarded.
            self.assertEqual(page["template"], "Inactive")
            self.assertGreater(page["endTime"], page["startTime"])
