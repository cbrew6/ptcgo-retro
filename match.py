"""
Binds the rules engine to the client's view of a game.

The engine knows the rules and nothing about the client; the client renders and
knows no rules at all. This module is the only place the two meet. It owns
three translations:

  engine card id  -> the entity GUID the client was told about
  engine state    -> the SerializedGameState entity tree
  engine Change   -> the mutation messages that animate it

Keeping that here means `engine.py` stays testable without a socket, and
`server.py` stays a protocol handler rather than a rules engine.

Two client behaviours shape the design and are easy to get wrong:

  - "Face down" is not a flag, it is the absence of attributes. An entity whose
    attributes are null renders as a card back, so revealing a card is an
    EntityIntroduced, not an AttributeModified.
  - Energy attachment is structural, not an attribute: the energy card entity
    literally becomes a child of the Pokemon entity, so attaching is an
    EntityMoved onto the Pokemon.
"""

import uuid

import engine

# Entity class names, as Java FQCNs. k.P.introduce throws on anything else.
ENTITY_PLAYMAT = "com.direwolfdigital.cake.rules.entities.CakePlayMat"
ENTITY_PLAYER = "com.direwolfdigital.cake.rules.entities.CakePlayerEntity"
ENTITY_AREA = "com.direwolfdigital.game.core.PlayArea"
ENTITY_SLOTTED = "com.direwolfdigital.cake.rules.entities.SlottedPlayArea"
ENTITY_POKEMON = "com.direwolfdigital.cake.rules.entities.Pokemon"
ENTITY_TRAINER = "com.direwolfdigital.cake.rules.entities.TrainerCard"
ENTITY_ENERGY = "com.direwolfdigital.cake.rules.entities.Energy"

ZONE_PLAYMAT = "playmat"
ZONE_DECK, ZONE_HAND, ZONE_PRIZES = "deck", "hand", "prizePile"
ZONE_ACTIVE, ZONE_BENCH = "activePokemonArea", "bench"
ZONE_DISCARD, ZONE_LOST = "discard", "lostZone"
PLAYMAT_ZONES = ("outOfPlay", "activeStadium", "activeTrainer")
PLAYER_ZONES = (ZONE_DECK, ZONE_HAND, ZONE_PRIZES, ZONE_ACTIVE,
                ZONE_BENCH, ZONE_DISCARD, ZONE_LOST)

ATTR_ARCHETYPE_ID = 10000
ATTR_NAME_KEY = 10140
ATTR_HP = 200490                 # value = current, originalValue = max
ATTR_SET, ATTR_CARD_NAME, ATTR_CARD_NUM = 200580, 200630, 200780

# Zones whose contents the owner may see. Everything else stays face down.
OPEN_ZONES = (ZONE_HAND, ZONE_ACTIVE, ZONE_BENCH, ZONE_DISCARD)

# Synthetic action ids for moves that are not a printed ability. These only
# need to be stable GUIDs: an action rendered with selectionType "Ability"
# auto-advances and never has its id looked up in the card's ability list.
ACTION_ATTACH = "1e7c0b00-0000-4000-8000-000000000001"
ACTION_PLAY_BASIC = "1e7c0b00-0000-4000-8000-000000000002"
ACTION_RETREAT = "1e7c0b00-0000-4000-8000-000000000003"
ACTION_EVOLVE = "1e7c0b00-0000-4000-8000-000000000004"


def _loc(text):
    return {"id": text}


def _entity(eid, parent, owner, name, attrs, children=None):
    return {
        "entityID": eid,
        "parentID": parent,
        "owningPlayerID": owner,
        "entityName": name,
        "archetypeID": None,
        "attributes": attrs,
        # Never None: Entities.initialize reads .Length on it directly.
        "children": children if children is not None else [],
    }


class Match:
    """One live game, plus the entity ids the client knows its cards by."""

    def __init__(self, game_id, accounts, db, decks, seed=0, rules=None,
                 first_player=None):
        self.game_id = game_id
        self.accounts = list(accounts)          # index 0 is the local player
        self.db = db
        self.state, self.opening = engine.new_game(
            db, decks, seed=seed, first_player=first_player,
            rules=rules or engine.DEFAULT_RULES)
        self.entity = {}                        # engine card id -> entity GUID
        self.pile = {}                          # (player, zone) -> entity GUID
        self.playmat_id = str(uuid.uuid4())
        self.player_entity = {}                 # player index -> entity GUID
        self.known = set()                      # every entity the client has

    # -- identity --------------------------------------------------------

    def account(self, player):
        return self.accounts[player]

    def player_of(self, account):
        return self.accounts.index(account) if account in self.accounts else None

    def eid(self, cid):
        """The entity GUID for an engine card, minted on first use."""
        if cid not in self.entity:
            self.entity[cid] = str(uuid.uuid4())
        return self.entity[cid]

    def card(self, cid):
        return self.state.card(cid)

    # -- card presentation -----------------------------------------------

    def card_kind(self, cid):
        card = self.card(cid)
        if card.is_energy:
            return ENTITY_ENERGY
        if card.is_pokemon:
            return ENTITY_POKEMON
        return ENTITY_TRAINER

    def card_attributes(self, cid, slot=None):
        """What makes a card render face up.

        HP carries current and max in one attribute - the client derives damage
        as originalValue - value - so a damaged Pokemon needs no separate
        counter attribute.
        """
        card = self.card(cid)
        attrs = [{"name": ATTR_ARCHETYPE_ID, "value": card.guid}]
        if card.name:
            attrs.append({"name": ATTR_CARD_NAME, "value": card.name})
        if card.set_code:
            attrs.append({"name": ATTR_SET, "value": card.set_code})
        if card.collector_number is not None:
            attrs.append({"name": ATTR_CARD_NUM, "value": card.collector_number})
        if card.is_pokemon and card.max_hp:
            current = card.max_hp - (slot.damage if slot else 0)
            attrs.append({"name": ATTR_HP, "value": max(0, current),
                          "originalValue": card.max_hp})
        return attrs

    # -- the board -------------------------------------------------------

    def all_cards(self, player):
        """Every card that player owns, wherever it currently is."""
        cards = list(player.deck) + list(player.hand) + list(player.prizes)
        cards += list(player.discard) + list(player.lost)
        for slot in ([player.active] if player.active else []) + list(player.bench):
            cards += list(slot.stack) + list(slot.energy)
        return cards

    def serialized_state(self, predeal=True):
        """The whole board as one SerializedGameState body.

        predeal puts every card back in its owner's deck, face down, so the
        engine's own opening Changes can then animate the deal. Without it the
        board arrives already set up and the game appears rather than starts.
        """
        children = [
            _entity(str(uuid.uuid4()), self.playmat_id, self.account(0),
                    ENTITY_AREA, [{"name": ATTR_NAME_KEY, "value": _loc(z)}])
            for z in PLAYMAT_ZONES
        ]
        for index, player in enumerate(self.state.players):
            owner = self.account(index)
            player_id = self.player_entity.setdefault(index, str(uuid.uuid4()))
            piles = []
            for zone in PLAYER_ZONES:
                pile_id = self.pile.setdefault((index, zone), str(uuid.uuid4()))
                kind = ENTITY_SLOTTED if zone == ZONE_BENCH else ENTITY_AREA
                if predeal:
                    contents = self.all_cards(player) if zone == ZONE_DECK else []
                else:
                    contents = self._zone_cards(player, zone)
                kids = [self._card_entity(cid, pile_id, owner,
                                          ZONE_DECK if predeal else zone, index)
                        for cid in contents]
                piles.append(_entity(
                    pile_id, player_id, owner, kind,
                    [{"name": ATTR_NAME_KEY, "value": _loc(zone)}], kids))
            children.append(_entity(
                player_id, self.playmat_id, owner, ENTITY_PLAYER,
                [{"name": ATTR_NAME_KEY, "value": _loc(owner)}], piles))

        playmat = _entity(self.playmat_id, None, self.account(0),
                          ENTITY_PLAYMAT,
                          [{"name": ATTR_NAME_KEY, "value": _loc(ZONE_PLAYMAT)}],
                          children)
        self._remember(playmat)
        return {
            "gameID": self.game_id,
            "playerAccounts": list(self.accounts),
            "gameOptions": {"Timers": "false"},
            "entities": playmat,
        }

    def _zone_cards(self, player, zone):
        if zone == ZONE_DECK:
            return list(player.deck)
        if zone == ZONE_HAND:
            return list(player.hand)
        if zone == ZONE_PRIZES:
            return list(player.prizes)
        if zone == ZONE_DISCARD:
            return list(player.discard)
        if zone == ZONE_LOST:
            return list(player.lost)
        # Pokemon in play are slots, and attached Energy are children of the
        # Pokemon rather than of the pile.
        slots = []
        if zone == ZONE_ACTIVE and player.active is not None:
            slots = [player.active]
        elif zone == ZONE_BENCH:
            slots = list(player.bench)
        return [slot.stack[-1] for slot in slots if slot.stack]

    def _card_entity(self, cid, parent, owner, zone, player_index):
        """A card in a pile. Only the local player's open zones are face up."""
        visible = (zone in OPEN_ZONES
                   and (player_index == 0 or zone != ZONE_HAND))
        attrs = self.card_attributes(cid) if visible else None
        return _entity(self.eid(cid), parent, owner, self.card_kind(cid), attrs)

    def _remember(self, entity):
        self.known.add(entity["entityID"])
        for child in entity["children"]:
            self._remember(child)

    # -- slots in play ---------------------------------------------------

    def slot_entities(self, index):
        """(slot, pile entity, is_active) for everything that player has out."""
        player = self.state.players[index]
        out = []
        if player.active is not None:
            out.append((player.active, self.pile[(index, ZONE_ACTIVE)], True))
        for slot in player.bench:
            out.append((slot, self.pile[(index, ZONE_BENCH)], False))
        return out

    def slot_of_entity(self, entity_id):
        """Which engine slot a Pokemon entity is, or None."""
        for index in range(len(self.state.players)):
            for slot, _pile, _active in self.slot_entities(index):
                if slot.stack and self.eid(slot.stack[-1]) == entity_id:
                    return index, slot
        return None, None

    def entity_of_slot(self, slot):
        return self.eid(slot.stack[-1]) if slot.stack else None

    # -- setup -----------------------------------------------------------

    def auto_setup(self):
        """Place both sides' Pokemon without asking.

        Temporary. The real game prompts each player to choose an Active and a
        Bench, which needs the setup selection UI; until that exists the server
        makes a reasonable choice so a game can be played at all.
        """
        changes = []
        for _ in range(64):                    # bounded: never spin on a bug
            if self.state.phase != engine.PHASE_SETUP:
                break
            acting = engine.players_to_act(self.state)
            if not acting:
                break
            actions = engine.legal_actions(self.state, acting[0])
            if not actions:
                break
            action = self._setup_choice(actions)
            self.state, made = engine.apply(self.state, action)
            changes.extend(made)
        return changes

    @staticmethod
    def _setup_choice(actions):
        for kind in (engine.SetupPlaceActive, engine.SetupPlaceBench):
            for action in actions:
                if isinstance(action, kind):
                    return action
        return actions[0]

    # -- engine changes -> client messages --------------------------------

    def messages_for(self, changes):
        """Translate engine Changes into (name, value) messages to send.

        Only the visible consequences are emitted. Shuffles and phase markers
        move no card the client can see, and the deal is already reflected in
        whatever board state was sent, so they produce nothing here.
        """
        out = []
        for change in changes:
            handler = getattr(self, "_change_" + change.kind, None)
            if handler is not None:
                out.extend(handler(change) or [])
        return out

    def _introduce(self, cid, slot=None):
        return ("EntityIntroduced", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "entityName": self.card_kind(cid),   # never null, or Introduce throws
            "attributeMap": self.card_attributes(cid, slot),
        })

    def _move(self, cid, destination, duration=300):
        return ("EntityMoved", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "destinationID": destination,
            "positionInParent": -1,              # negative appends
            "animDuration": duration,
        })

    def _destination(self, change):
        """The entity a moved card should end up inside."""
        zone = change.to_zone
        if zone in (ZONE_ACTIVE, ZONE_BENCH):
            return self.pile.get((change.player, zone))
        return self.pile.get((change.player, zone))

    def _change_move(self, change):
        if change.card is None or change.to_zone is None:
            return []
        destination = self._destination(change)
        if destination is None:
            return []
        msgs = []
        # A card arriving somewhere its owner can see has to be turned face up,
        # and face up means "has attributes" - so this is an introduction.
        if change.to_zone in OPEN_ZONES and (
                change.player == 0 or change.to_zone != ZONE_HAND):
            msgs.append(self._introduce(change.card))
        msgs.append(self._move(change.card, destination))
        return msgs

    def _change_attach(self, change):
        """Energy becomes a child of the Pokemon; there is no energy attribute."""
        if change.card is None or change.slot is None:
            return []
        target = self.entity_of_slot(change.slot)
        if target is None:
            return []
        return [self._introduce(change.card), self._move(change.card, target)]

    def _change_damage(self, change):
        """Damage is max minus current on one attribute, not a counter."""
        if change.slot is None or not change.slot.stack:
            return []
        cid = change.slot.stack[-1]
        card = self.card(cid)
        if not card.max_hp:
            return []
        current = max(0, card.max_hp - change.slot.damage)
        return [("AttributeModified", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "attribute": {"name": ATTR_HP, "value": current,
                          "originalValue": card.max_hp},
        })]

    def _change_prize(self, change):
        return self._change_move(change)

    def _change_knockout(self, change):
        return []            # the engine also emits the moves to the discard

    def _change_turnStart(self, change):
        return [("ActivePlayerSet", {
            "gameID": self.game_id,
            "accountID": self.account(change.player),
        })]
