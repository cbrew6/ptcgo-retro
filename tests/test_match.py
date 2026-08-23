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
