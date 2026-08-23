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
ATTR_CARD_TYPE, ATTR_TRAINER_TYPE = 200300, 200270
ATTR_STAGE, ATTR_RETREAT, ATTR_FAMILY = 200540, 200800, 200260
ATTR_POKEMON_TYPES = 200570        # PokemonTypes[]; CardImageRenderer throws
                                   # without it - see card_attributes
ATTR_ENERGY_PROVIDED = 201040      # {"options": [[type, ...], ...]}
ATTR_ASSET_NAME = 10020            # art-variant suffix, preferred over 200780
ATTR_CONDITIONS = 200340           # SpecialConditions[]; the whole list, not a
                                   # delta - see _change_condition
ATTR_BENCH_SLOTS = 201920          # BenchLayout divides by this: 0 gives NaN

# Zones whose contents the owner may see. Everything else stays face down.
OPEN_ZONES = (ZONE_HAND, ZONE_ACTIVE, ZONE_BENCH, ZONE_DISCARD)

# Synthetic action ids for moves that are not a printed ability. These only
# need to be stable GUIDs: an action rendered with selectionType "Ability"
# auto-advances and never has its id looked up in the card's ability list.
ACTION_ATTACH = "1e7c0b00-0000-4000-8000-000000000001"
ACTION_PLAY_BASIC = "1e7c0b00-0000-4000-8000-000000000002"
ACTION_RETREAT = "1e7c0b00-0000-4000-8000-000000000003"
ACTION_EVOLVE = "1e7c0b00-0000-4000-8000-000000000004"
ACTION_PROMOTE = "1e7c0b00-0000-4000-8000-000000000005"
ACTION_SETUP_ACTIVE = "1e7c0b00-0000-4000-8000-000000000006"
ACTION_SETUP_BENCH = "1e7c0b00-0000-4000-8000-000000000007"


def _loc(text):
    return {"id": text}


def _target_entities(selection):
    """Every entity id the client named in its target responses, in order.

    Shape is [[entityID, abilityID], [{"entityList": [...], "name": ...}, ...]].
    Written defensively because it is parsing input from outside the server and
    a malformed reply must not take the game down - an unrecognised answer is
    re-offered, which is recoverable, while an exception here is not.
    """
    try:
        responses = selection[1]
    except (IndexError, TypeError, KeyError):
        return []
    out = []
    for response in responses or []:
        if isinstance(response, dict):
            for entity in response.get("entityList") or []:
                out.append(entity)
        elif isinstance(response, str):
            out.append(response)
    return out


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
        # 10140 is load-bearing, not decoration. The hand's comparator does
        # GetOne<NameLookup>().Name.CompareTo(...) with no null check, so a card
        # without it throws inside List.Sort - and EntityChildRenderer clears
        # the layout BEFORE sorting, so the whole hand is emptied every frame
        # and never refilled. Cards then end up unparented at the world origin,
        # which is the "in the middle of the board, behind it" symptom.
        if card.name_key:
            attrs.append({"name": ATTR_NAME_KEY, "value": _loc(card.name_key)})
        if card.card_types:
            attrs.append({"name": ATTR_CARD_TYPE, "value": card.card_types[0]})
        if card.stage:
            attrs.append({"name": ATTR_STAGE, "value": card.stage})
        if card.trainer_types:
            attrs.append({"name": ATTR_TRAINER_TYPE,
                          "value": card.trainer_types[0]})
        if card.is_pokemon:
            if card.retreat_cost:
                attrs.append({"name": ATTR_RETREAT, "value": card.retreat_cost})
            if card.family_id is not None:
                attrs.append({"name": ATTR_FAMILY, "value": card.family_id})
        # 200570 is what makes a card show its art at all. CardImageRenderer's
        # getDefaultPerCardType picks the placeholder from
        # typeData.EnergyType.Value, and EnergyType is only populated for a
        # Pokemon from the LAST entry of this array - so with the attribute
        # absent it is a Nullable with no value and .Value throws. The throw
        # unwinds RefreshRequestData before it reaches getImageRequestString,
        # so the card never requests its texture and stays blank forever.
        # Collection and deck views build cards from the local archetype DB,
        # which has every attribute; only entities we synthesise are affected,
        # which is why art broke in matches alone.
        if card.types and (card.is_pokemon or "LegendHalf" in card.card_types):
            attrs.append({"name": ATTR_POKEMON_TYPES, "value": list(card.types)})
        if card.energy_options:
            attrs.append({"name": ATTR_ENERGY_PROVIDED,
                          "value": {"options": [list(o) for o in card.energy_options]}})
        # Variant printings ("017a") are looked up by this instead of the
        # padded collector number; without it they render the plain printing.
        if card.card_image:
            attrs.append({"name": ATTR_ASSET_NAME, "value": card.card_image})
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
        # A Pokemon re-introduced while Asleep - promoted after a knockout,
        # say - would otherwise lose its markers, because introducing replaces
        # the whole attribute map rather than merging into it.
        if slot is not None and slot.conditions:
            attrs.append({"name": ATTR_CONDITIONS,
                          "value": sorted(slot.conditions)})
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
        # owningPlayerID is how IntroduceEntity routes an area: anything owned
        # by a player goes into that player's piles, so a playmat-level area
        # with an owner is never bound to its layout and throws on the way.
        children = [
            _entity(str(uuid.uuid4()), self.playmat_id, None,
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
                pile_attrs = [{"name": ATTR_NAME_KEY, "value": _loc(zone)}]
                if zone == ZONE_BENCH:
                    # BenchLayout computes width as sqrt(k / slots); absent it
                    # reads 0, every transform becomes NaN or Infinity, and
                    # Unity spams collider warnings for each benched card.
                    pile_attrs.append({"name": ATTR_BENCH_SLOTS, "value": 5})
                piles.append(_entity(pile_id, player_id, owner, kind,
                                     pile_attrs, kids))
            children.append(_entity(
                player_id, self.playmat_id, owner, ENTITY_PLAYER,
                [{"name": ATTR_NAME_KEY, "value": _loc(owner)}], piles))

        playmat = _entity(self.playmat_id, None, None,
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
        return self.eid(slot.stack[-1]) if slot and slot.stack else None

    def resolve_slot(self, slot_ref):
        """A Change carries a slot id, not the Slot itself."""
        if slot_ref is None:
            return None
        if hasattr(slot_ref, "stack"):
            return slot_ref
        try:
            _player, slot, _active = self.state.slot(slot_ref)
        except Exception:
            return None
        return slot

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
        target = self.entity_of_slot(self.resolve_slot(change.slot))
        if target is None:
            return []
        return [self._introduce(change.card), self._move(change.card, target)]

    def _change_damage(self, change):
        """Damage is max minus current on one attribute, not a counter."""
        slot = self.resolve_slot(change.slot)
        if slot is None or not slot.stack:
            return []
        cid = slot.stack[-1]
        card = self.card(cid)
        if not card.max_hp:
            return []
        current = max(0, card.max_hp - slot.damage)
        return [("AttributeModified", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "attribute": {"name": ATTR_HP, "value": current,
                          "originalValue": card.max_hp},
        })]

    def _change_condition(self, change):
        """Special conditions are one array attribute, not a flag per state.

        The engine's condition names were chosen to match the client's
        SpecialConditions enum exactly (Asleep, Burned, Confused, Paralyzed,
        Poisoned), so the set is sent through as-is. It is sent WHOLE rather
        than as a delta because the attribute is the complete list: sending
        only the condition that changed would clear every other one, and a
        Pokemon can be Asleep and Poisoned at once.

        Without this the engine was already applying poison damage between
        turns and refusing to let a Paralyzed Pokemon attack, while the board
        showed no marker at all - the rules were running invisibly.
        """
        slot = self.resolve_slot(change.slot)
        if slot is None or not slot.stack:
            return []
        cid = slot.stack[-1]
        return [("AttributeModified", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "attribute": {"name": ATTR_CONDITIONS,
                          "value": sorted(slot.conditions)},
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

    # -- offering actions -------------------------------------------------
    #
    # The client cannot know what is legal - it has no rules - so the server
    # sends a menu and the client renders it. Each row is one (entity, action)
    # pair; the client regroups them by entity.
    #
    # selectionType decides the UI and is the most load-bearing string here.
    # "Ability" auto-advances to target selection and never looks the action id
    # up on the card, which is what makes it right for moves that are not a
    # printed ability. "AbilitySelection" draws a button per ability and only
    # draws it if the action id really appears in that card's ability list, so
    # attacks must carry their true abilityID. A single entity whose rows mix
    # the two lands in a fallback with no UI, so rows are grouped per entity
    # and kept consistent.

    def _target_info(self, valid, prompt, selected=True, minimum=1):
        return {
            "name": "EntityListTargetInformation",
            "selected": selected,
            "accountID": None,
            "targetPrompt": prompt,
            "validTargets": list(valid),      # never null: .Length is read
            "numberToSelect": 1,
            "minimumToSelect": minimum,
            "forced": True,
            "hintTargetMap": {},              # never null: iterated unguarded
        }

    def _action_row(self, entity_id, action_id, description, selection_type,
                    targets):
        return {
            "entityID": entity_id,
            "selectableAction": {
                "gameID": self.game_id,
                "actionID": action_id,
                "description": description,
                "selectionType": selection_type,
                "actionHint": "Optimal",      # never "Unselectable": it throws
            },
            "targetInfoLst": targets,
        }

    def own_pokemon_entities(self, player):
        return [self.entity_of_slot(slot)
                for slot, _pile, _active in self.slot_entities(player)
                if slot.stack]

    def _offer_group(self, rows, decode, entity, action_id, description,
                     selection_type, by_target, prompt=None):
        """One offered move, and how to read the answer back.

        by_target maps a target entity id to the engine Action that choosing it
        means. When it holds one entry the target is forced, so it is sent
        unselected and the click resolves in one step; when it holds several
        the client runs its target picker and echoes the choice back.

        The whole point of keying on the target is that one (entity, action)
        pair can stand for several Actions - attaching an Energy to any of six
        Pokemon is one row with six targets, not six rows. Dropping the target
        and applying an arbitrary one of them is how "attach energy" used to
        put it on whichever Pokemon the engine happened to list first.
        """
        targets = [t for t in by_target if t]
        if not targets:
            return
        rows.append(self._action_row(
            entity, action_id, description, selection_type,
            [self._target_info(targets, prompt, selected=len(targets) > 1)]))
        decode[(entity, action_id)] = dict(by_target)

    def _setup_offer(self, player, counter):
        """Let the player choose their own Active and Bench.

        The real game has a dedicated setup screen driven by
        SelectionWithTargetsRequired carrying ActivePokemonTargetInformation
        and InitialBenchedTargetInformation. This is not that. It presents the
        same two decisions through the ordinary action offer, which is a path
        that is already exercised and tested - the server used to place both
        Pokemon itself, so the player never chose at all.

        The engine makes the phase easy to render because it only ever offers
        one shape at a time: SetupPlaceActive alone while the Active is empty,
        then SetupPlaceBench alongside SetupDone. So the Active step is forced
        and the bench step is not, which puts "I am finished benching" on the
        same Next button that ends a turn - a null selection, decoded by the
        server as SetupDone.
        """
        rows, decode = [], {}
        active_pile = self.pile.get((player, ZONE_ACTIVE))
        bench_pile = self.pile.get((player, ZONE_BENCH))
        placing_active = False

        for action in engine.legal_actions(self.state, player):
            if isinstance(action, engine.SetupPlaceActive):
                placing_active = True
                self._offer_group(rows, decode, self.eid(action.card),
                                  ACTION_SETUP_ACTIVE, "PlayBasic", "Ability",
                                  {active_pile: action})
            elif isinstance(action, engine.SetupPlaceBench):
                self._offer_group(rows, decode, self.eid(action.card),
                                  ACTION_SETUP_BENCH, "PlayBasic", "Ability",
                                  {bench_pile: action})
        return {
            "counter": counter,
            "prompt": ("playmat.prompt.choosebasicpokemon" if placing_active
                       else "playmat.prompt.choosepokemonforbench"),
            "offerLength": 0,
            "startingTimestamp": 0,
            # Choosing an Active is compulsory - the game cannot start without
            # one - so there is no Next button to skip it with. Benching is
            # optional, so there is.
            "forced": placing_active,
            "targetType": "",
            "optimalPlayMap": [],
            "selectionParams": {},
            "targetMap": rows,
        }, decode

    def build_offer(self, player, counter):
        """A SelectionWithTargetsAndActionsRequired body, plus a decode map.

        Every legal move has to appear here, because the client holds no rules
        and will not invent one. Anything missing is simply not playable: for a
        long time only AttachEnergy and Attack were offered, so a hand of Basic
        Pokemon could not be benched, nothing evolved, nobody retreated, and a
        player whose Active was knocked out was offered nothing at all.

        Rows are grouped by entity and each entity keeps ONE selectionType,
        which decides how they are laid out:

          hand cards    "Ability"           play, evolve, attach
          bench Pokemon "Ability"           retreat into, promote
          the Active    "AbilitySelection"  its attacks

        That split is not cosmetic. "AbilitySelection" draws a button per
        ability and only draws it when the action id really appears in that
        card's ability list, so attacks must carry their true abilityID and
        nothing else may share the Active's rows. "Ability" auto-advances to
        target selection and never looks the id up, which is what makes it
        right for moves that are not printed on the card. An entity whose rows
        mix the two lands in a fallback with no UI at all.
        """
        rows, decode = [], {}
        me = self.state.players[player]
        opponent = 1 - player
        opp_active = None
        if self.state.players[opponent].active is not None:
            opp_active = self.entity_of_slot(self.state.players[opponent].active)
        bench_pile = self.pile.get((player, ZONE_BENCH))

        if self.state.phase == engine.PHASE_SETUP:
            return self._setup_offer(player, counter)

        # Gather first, emit second: several Actions can collapse into one row
        # that differs only by target, and that is only visible once they are
        # all in hand.
        attach, evolve, play, retreat, promote = {}, {}, {}, {}, {}
        attacks = []
        for action in engine.legal_actions(self.state, player):
            if isinstance(action, engine.AttachEnergy):
                target = self.entity_of_slot(self.resolve_slot(action.slot))
                attach.setdefault(action.card, {})[target] = action
            elif isinstance(action, engine.Evolve):
                target = self.entity_of_slot(self.resolve_slot(action.slot))
                evolve.setdefault(action.card, {})[target] = action
            elif isinstance(action, engine.PlayBasic):
                play[action.card] = action
            elif isinstance(action, engine.Retreat):
                # Retreat names the Pokemon coming IN, so the row hangs off the
                # bench Pokemon being switched to rather than off the Active.
                # That also keeps it away from the Active's attack rows.
                target = self.entity_of_slot(self.resolve_slot(action.slot))
                retreat[target] = action
            elif isinstance(action, engine.Promote):
                target = self.entity_of_slot(self.resolve_slot(action.slot))
                promote[target] = action
            elif isinstance(action, engine.Attack):
                attacks.append(action)

        for card, by_target in attach.items():
            self._offer_group(rows, decode, self.eid(card), ACTION_ATTACH,
                              "PlayEnergy", "Ability", by_target,
                              "playmat.prompt.attachenergy")
        for card, by_target in evolve.items():
            self._offer_group(rows, decode, self.eid(card), ACTION_EVOLVE,
                              "Evolve", "Ability", by_target,
                              "playmat.prompt.selectapokemontoevolve")
        for card, action in play.items():
            # Benching needs no target, but a row with no target at all has no
            # click to resolve, so the bench itself stands in as a forced one.
            self._offer_group(rows, decode, self.eid(card), ACTION_PLAY_BASIC,
                              "PlayBasic", "Ability", {bench_pile: action})
        for target, action in retreat.items():
            self._offer_group(rows, decode, target, ACTION_RETREAT,
                              "Retreat", "Ability", {target: action})
        for target, action in promote.items():
            self._offer_group(rows, decode, target, ACTION_PROMOTE,
                              "Promote", "Ability", {target: action})

        if opp_active:
            active_entity = self.entity_of_slot(me.active) if me.active else None
            for action in attacks:
                if not active_entity:
                    continue
                self._offer_group(
                    rows, decode, active_entity, action.ability_id,
                    action.ability_id, "AbilitySelection",
                    {opp_active: action})

        # A promotion is owed, not chosen: the turn cannot continue around it,
        # so the client must not be given an end-turn button to escape with.
        forced = me.active is None and bool(promote)
        return {
            "counter": counter,
            "prompt": ("playmat.prompt.dragbenchtoactive" if forced
                       else "playmat.prompt.chooseaction"),
            "offerLength": 0,                 # no client-side auto-pass
            "startingTimestamp": 0,
            "forced": forced,                 # false lets Next end the turn
            "targetType": "",                 # never null: looked up as a key
            "optimalPlayMap": [],             # never null: iterated unguarded
            "selectionParams": {},
            "targetMap": rows,
        }, decode

    @staticmethod
    def decode_reply(selection, decode):
        """The client's echo -> the engine Action.

        The reply is built by core's Outgoing.SelectionWithTargetsAndActions as

            [[entityID, abilityID], [TargetResponse, ...]]

        where each TargetResponse is {"entityList": [id, ...], "name": ...}.
        The first pair identifies the row; the target list says which of that
        row's targets was picked, and picking is the whole difference between
        attaching an Energy to the Pokemon the player clicked and attaching it
        to an arbitrary one.

        A null selection is the player passing, which is also how the Next
        button ends a turn.
        """
        if not selection:
            return None
        try:
            entity_id, action_id = selection[0][0], selection[0][1]
        except (IndexError, TypeError, KeyError):
            return None
        by_target = decode.get((entity_id, action_id))
        if not by_target:
            return None

        for response in _target_entities(selection):
            if response in by_target:
                return by_target[response]
        # A forced single target is sent unselected, so the client may answer
        # with no target at all. With one candidate that is unambiguous; with
        # several it is not, and guessing would silently play the wrong move.
        if len(by_target) == 1:
            return next(iter(by_target.values()))
        return None

    # -- the opening animation --------------------------------------------

    def opening_animation(self):
        """A clean deal derived from the final board, not from the change log.

        Replaying the engine's opening Changes looked wrong for three separate
        reasons, all of which this avoids:

          - Mulligans are in that log. A deck thin on Basics redraws many
            times, so cards visibly flew out of the deck and straight back
            into it. The real game never animated that; it showed a summary
            afterwards. Here the churn is simply not shown - only the hand the
            player actually ends up with.
          - Every move arrived as its own message and played one at a time.
            The named sequences run their nested GroupedMoves in parallel with
            a small stagger, which is what makes a hand fan out instead of
            trickling.
          - Order. Working from the final state means each card is dealt once,
            to where it actually ended up.

        Returns items for emit_sequence: ("seq", name, [...]) or ("msg", ...).
        """
        items = []
        for index in range(len(self.state.players)):
            items.append(("msg", "Shuffled", {
                "gameID": self.game_id,
                "entityID": self.pile[(index, ZONE_DECK)],
            }))

        hands = []
        for index, player in enumerate(self.state.players):
            moves = []
            for cid in player.hand:
                if index == 0:                      # only our own hand is open
                    moves.append(("msg",) + self._introduce_msg(cid))
                moves.append(("msg",) + self._move_msg(
                    cid, self.pile[(index, ZONE_HAND)]))
            if moves:
                hands.append(("seq", "GroupedMove", moves))
        if hands:
            items.append(("seq", "DealInitialHands", hands))

        reveal = []
        for index, player in enumerate(self.state.players):
            for slot, _pile, is_active in self.slot_entities(index):
                if not slot.stack:
                    continue
                cid = slot.stack[-1]
                zone = ZONE_ACTIVE if is_active else ZONE_BENCH
                reveal.append(("msg",) + self._introduce_msg(cid, slot))
                reveal.append(("msg",) + self._move_msg(
                    cid, self.pile[(index, zone)]))
        if reveal:
            items.append(("seq", "IntroduceInitialPokemon", reveal))

        prizes = []
        for index, player in enumerate(self.state.players):
            moves = [("msg",) + self._move_msg(cid, self.pile[(index, ZONE_PRIZES)])
                     for cid in player.prizes]
            if moves:
                prizes.append(("seq", "GroupedMove", moves))
        if prizes:
            items.append(("seq", "DealInitialPrizeCards", prizes))
        return items

    def _introduce_msg(self, cid, slot=None):
        return ("EntityIntroduced", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "entityName": self.card_kind(cid),
            "attributeMap": self.card_attributes(cid, slot),
        })

    def _move_msg(self, cid, destination, duration=300):
        return ("EntityMoved", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "destinationID": destination,
            "positionInParent": -1,
            "animDuration": duration,
        })
