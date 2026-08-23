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
ZONE_STADIUM, ZONE_OUT_OF_PLAY = "activeStadium", "outOfPlay"
PLAYMAT_ZONES = (ZONE_OUT_OF_PLAY, ZONE_STADIUM, "activeTrainer")
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
ATTR_ABILITY_SOURCE = 201870       # EntityID[] on the PLAYMAT: who is acting.
                                   # The Attack sequence reads [0] unguarded.
ATTR_ABILITIES = 200740            # PieAbilityDescription[]; without it the
                                   # card has no attack buttons at all

# PieAbilityDescription is [TypeHinting("abilityType")], so that field names a
# subclass and an unknown value throws in the message pump. These are the
# classes that actually exist; every abilityType in carddata is one of them,
# and anything else is dropped rather than gambled on.
ABILITY_TYPES = frozenset((
    "Attack", "PokeAbility", "PokePower", "PokeBody", "AncientTrait",
    "TechnicalMachine", "EnergyAbility", "StadiumAbility", "TrainerAbility",
    "PlayAbility", "RetreatAbility",
))



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
ACTION_TRAINER = "1e7c0b00-0000-4000-8000-000000000008"
ACTION_TOOL = "1e7c0b00-0000-4000-8000-000000000009"
ACTION_SETUP_DONE = "1e7c0b00-0000-4000-8000-00000000000b"

# The prompts the original server used, recovered from the localization DB -
# it still carries the server's own com.direwolfdigital.cake.rules.* namespace,
# which is direct evidence of what was sent rather than a plausible guess.
PROMPT_SETUP_ACTIVE = (
    "com.direwolfdigital.cake.rules.states.startgame.selectstartingpokemon")
PROMPT_SETUP_BENCH = "playmat.gamestart.promptbenchpokemon.new"

# The action offer deliberately uses a prompt the client SUPPRESSES.
# PiePromptListener.suppressedKeys holds eight ids it refuses to draw a banner
# for, this among them, and the original server used it for exactly that
# reason: during your own turn the board is the prompt. HasId compares
# case-insensitively, so the mixed case here matches the all-lowercase DB.
PROMPT_CHOOSE_ACTION = (
    "com.direwolfdigital.cake.rules.states.ActionPhase.SelectAction")

# engine Choice.prompt is a stable id for the renderer to key off, not text.
# Each maps to a real key from the client's shipped DB - a key that does not
# exist is not an error client-side, it is displayed verbatim, so every one of
# these was looked up rather than guessed.
PROMPT_COIN_FLIP = "com.direwolfdigital.cake.rules.states.startgame.coinflip"
PROMPT_CALL_FLIP = "playmat.gamestart.prompt.coinflipchoice"
# A prompt that draws no banner. CanShowPrompt (pie_d.cs:192338) requires
# Prompt != null AND a non-empty DisplayText, so an empty id passes the null
# check and fails the emptiness one - which is exactly "no banner", without
# borrowing an unrelated suppressed key to get there.
PROMPT_NONE = ""
PROMPT_NEW_ACTIVE = "playmat.prompt.dragbenchtoactive"
BUTTON_HEADS = "com.direwolfdigital.cake.rules.states.startgame.heads"
BUTTON_TAILS = "com.direwolfdigital.cake.rules.states.startgame.tails"
# The original server's own mulligan prompt, still in the shipped DB: "Your
# opponent had no Basic Pokemon and had to draw a new hand. Would you like to
# draw a card?" It asked once per mulligan, with Yes/No; asking once for a
# total keeps the same wording honest while scaling to any number of them.
PROMPT_MULLIGAN_DRAW = (
    "com.direwolfdigital.cake.rules.states.startgame.mulligancustomchoice")
BUTTON_YES = "com.direwolfdigital.cake.rules.states.startgame.mulliganchoicechoice1"
BUTTON_NO = "com.direwolfdigital.cake.rules.states.startgame.mulliganchoicechoice2"
# The numbered variant of the same question, "...for mulligan {0}?".
PROMPT_MULLIGAN_MULTI = PROMPT_MULLIGAN_DRAW + ".drawmultiple"
BUTTON_YES_REST = "playmat.mulligan.drawcards.drawallbutton"
BUTTON_NO_REST = "playmat.mulligan.drawcards.drawnonebutton"
PROMPT_MULLIGAN_REVEAL = "playmat.mulligan.dialog.body.opponent"
PROMPT_MULLIGAN_TITLE = "playmat.mulligan.dialog.carousel.header"

CHOICE_PROMPT_DEFAULT = "playmat.prompt.choosecards"
CHOICE_PROMPTS = {
    "healTarget":      "playmat.prompt.selectahurtpokemon",
    "discardFromHand": "playmat.prompt.selectcardtodiscard",
    "discardEnergy":   "playmat.prompt.chooseenergydiscard",
    "moveEnergy":      "playmat.prompt.chooseenergymove",
    "energyTarget":    "playmat.prompt.attachenergy",
    "evolveTarget":    "playmat.prompt.selectapokemontoevolve",
    "searchDeck":      "playmat.prompt.choosecardtoputintohand",
    "fromDiscard":     "playmat.prompt.choosecardtoputintohand",
    "fromHand":        "playmat.prompt.choosecards",
    "shuffleFromHand": "playmat.prompt.choosecards",
    "lookAtTop":       "playmat.prompt.choosecards",
    "gustTarget":      "playmat.prompt.selectanenemybenchedpokemon",
    "snipeTarget":     "playmat.prompt.choosebenchedpokemontodamage",
    "switchTo":        "playmat.prompt.selectabenchedpokemon",
    "scoopTarget":     "playmat.prompt.selectapokemon",
}


def _option_label(option):
    """A CHOICE_OPTION id as a button key.

    The engine's opaque options are words like "yes" / "no" / a colour. Yes and
    no have real keys; anything else has none, and the client renders an
    unknown key as itself - which for a bare word is a readable button rather
    than a broken one.
    """
    return {"yes": "common.dialog.yes",
            "no": "common.dialog.no"}.get(str(option).lower(), str(option))


def _loc(text):
    return {"id": text}


def _ability_description(ability):
    """One engine Ability as the client's PieAbilityDescription.

    carddata's ability JSON is already almost this shape - cost, damage,
    amountOperator and conditionExceptions are the Attack subclass's own
    fields - so only the localization wrappers have to come off.
    """
    described = {
        # "name" is not the discriminator here: PieAbilityDescription is
        # [TypeHinting("abilityType")], an INLINE field, not an envelope.
        "abilityType": ability.ability_type,
        "abilityID": ability.ability_id,
        "title": _loc(_loc_key(ability.title)),
        "gameText": _loc(_loc_key(ability.game_text)),
        "sortOrder": None,
        "buttonOverride": None,
        "bonusInfo": None,
        "ignoreInFiltering": False,
    }
    if ability.ability_type == "Attack":
        described.update({
            "cost": dict(ability.cost or {}),
            "damage": int(ability.damage or 0),
            "amountOperator": ability.amount_operator or "",
            "conditionExceptions": [],
        })
    return described


def _loc_n(key, **numbers):
    """A localization key with its {0}-style placeholders filled in.

    TextVariables.substNumbers is a literal string Replace, so the map KEY is
    the placeholder exactly as it appears in the text - "{0}", not "0". Python
    keyword arguments cannot be called "{0}", so they are passed as n0, n1 and
    translated here.
    """
    return {"id": key,
            "textVars": {"numberMap": {"{%s}" % name[1:]: value
                                       for name, value in numbers.items()}}}


def _loc_key(text):
    """A localization key as the client wants it.

    carddata wraps its keys for the exporter - "$$$com.direwolfdigital...$$$",
    sometimes quoted as well - and the wrapper is not part of the key. Lookups
    are case-insensitive (the shipped DB is entirely lowercase), so only the
    decoration has to come off.
    """
    if not text:
        return ""
    return text.strip().strip('"').strip("$")


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


def _flatten(items):
    """Depth-first walk of emit_items() form, yielding only the messages."""
    for kind, a, b in items:
        if kind == "seq":
            for inner in _flatten(b):
                yield inner
        else:
            yield ("msg", a, b)


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
        self.playmat_zone = {}                  # playmat-level zone -> GUID
        self.player_entity = {}                 # player index -> entity GUID
        self.known = set()                      # every entity the client has
        # The attack currently being resolved. The client's hit effect needs
        # the declaration (which attack, whose) and the damage that landed, and
        # those arrive as two separate Changes.
        self._attack = None
        self._attack_id = None
        self._single_areas = None

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
        # Without this the card has no abilities client-side, and
        # CreateButtons only makes a button when the offered action id appears
        # in the ENTITY's own ability list - so no attack ever rendered,
        # whether it was affordable or not.
        abilities = [_ability_description(a) for a in card.abilities
                     if a.ability_type in ABILITY_TYPES]
        if abilities:
            attrs.append({"name": ATTR_ABILITIES, "value": abilities})
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
        children = []
        for zone in PLAYMAT_ZONES:
            # Kept, because a Stadium has to be moved into one later and a
            # freshly minted id would name an entity the client never saw.
            zone_id = self.playmat_zone.setdefault(zone, str(uuid.uuid4()))
            children.append(_entity(
                zone_id, self.playmat_id, None, ENTITY_AREA,
                [{"name": ATTR_NAME_KEY, "value": _loc(zone)}]))
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

        Flat pairs, for callers that just want to push messages. Use
        animation_for() to get the same thing with its named sequences intact.
        """
        return [(name, body)
                for kind, name, body in _flatten(self.animation_for(changes))
                if kind == "msg"]

    def animation_for(self, changes):
        """The same translation, but keeping sequence structure.

        Returns emit_items() form: ("msg", name, body) or ("seq", name, items).
        Sequences are what make the board animate rather than snap - the named
        ones each add real choreography, and a hit with no Attack sequence
        around it just changes a number.
        """
        out = []
        for change in self._grouped(changes):
            if isinstance(change, tuple):     # an already-built sequence item
                out.append(change)
                continue
            handler = getattr(self, "_change_" + change.kind, None)
            if handler is None:
                continue
            for item in handler(change) or []:
                # Handlers mostly return plain (name, body); only the few that
                # need choreography return a full ("seq", ...) item.
                out.append(item if len(item) == 3 else ("msg",) + tuple(item))
        return out

    def _grouped(self, changes):
        """Changes, with runs that deserve one named sequence folded into it.

        Prizes are the case that matters. They are dealt once both boards are
        set up, so they arrive as six ordinary moves per player - and six loose
        EntityMoveds are six separate card flights, which is what "the prizes
        come out one at a time" looked like. DealInitialPrizeCards exists for
        exactly this: it overrides its GroupedMove children's stagger to 0.1s
        and turns the prize-count badges on afterwards.
        """
        out, run = [], []
        kos = []

        def flush_kos():
            """A knockout and the cards it takes out of play, as one sequence.

            The Knockout sequence does two things nothing else does: it walks
            its EntityMoved children to find the Pokemon leaving play and
            animates it into the knockout pile, and cleanupSelectAreaIfNeeded
            returns the attacker from the target-select area to its slot.
            Sent loose, the KO'd Pokemon simply appeared in the discard and the
            attacker never came home - which is "it went straight to discard
            with no knockout animation".
            """
            if not kos:
                return
            items = []
            for change in kos:
                destination = self.pile.get((change.player, change.to_zone))
                if destination is None or change.card is None:
                    continue
                items.append(("msg",) + self._move_msg(change.card, destination))
            del kos[:]
            if items:
                out.append(("seq", "Knockout", items))

        def flush():
            if not run:
                return
            moves = []
            for change in run:
                destination = self.pile.get((change.player, ZONE_PRIZES))
                if destination is None or change.card is None:
                    continue
                moves.append(("msg",) + self._move_msg(change.card, destination,
                                                       duration=0))
            del run[:]
            if moves:
                out.append(("seq", "DealInitialPrizeCards",
                            [("seq", "GroupedMove", moves)]))

        draws = []

        def flush_draws():
            if not draws:
                return
            items = []
            for change in draws:
                destination = self.pile.get((change.player, ZONE_HAND))
                if destination is None or change.card is None:
                    continue
                if change.player == 0:
                    items.append(("msg",) + self._introduce_msg(change.card))
                items.append(("msg",) + self._move_msg(change.card, destination))
            del draws[:]
            if items:
                # Draw buckets its children by card type for the draw fan.
                out.append(("seq", "Draw", items))

        # Which player currently owes a knockout run, so the moves that follow
        # a knockout are gathered and anything else closes it.
        knocked = [None]

        for change in changes:
            if change.kind == engine.CHANGE_KNOCKOUT:
                flush()
                flush_draws()
                flush_kos()
                knocked[0] = change.player
                continue
            if (knocked[0] is not None
                    and change.kind == engine.CHANGE_MOVE
                    and change.player == knocked[0]
                    and change.to_zone in (ZONE_DISCARD, ZONE_LOST)):
                kos.append(change)
                continue
            knocked[0] = None
            flush_kos()
            if (change.kind == engine.CHANGE_MOVE
                    and change.to_zone == ZONE_PRIZES
                    and change.from_zone == ZONE_DECK):
                flush_draws()
                run.append(change)
                continue
            if (change.kind == engine.CHANGE_MOVE
                    and change.to_zone == ZONE_HAND
                    and change.from_zone == ZONE_DECK):
                flush()
                draws.append(change)
                continue
            flush()
            flush_draws()
            out.append(change)
        flush()
        flush_draws()
        flush_kos()
        return out

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
            msgs.append(self._introduce_msg(change.card))
        msgs.append(self._move_msg(change.card, destination))
        return msgs

    def _change_attach(self, change):
        """Energy becomes a child of the Pokemon; there is no energy attribute."""
        if change.card is None or change.slot is None:
            return []
        target = self.entity_of_slot(self.resolve_slot(change.slot))
        if target is None:
            return []
        return [self._introduce_msg(change.card), self._move_msg(change.card, target)]

    def _change_attack(self, change):
        """Remembered, not sent.

        The client's hit effect needs the damage that actually landed, and that
        is only known once the damage Change arrives. Holding the declaration
        here lets _change_damage build one CakeAttackEffect carrying both.
        """
        self._attack = dict(change.detail or {})
        self._attack["slot"] = change.slot
        return []

    def _attack_effect(self, defender_cid, change):
        """CakeAttackEffect: the hit FX, the damage number, and the knockout.

        The knockout is decided CLIENT-side, as
        `damageAmount >= defender.currentHP`, which fixes the order this has to
        be sent in: before the HP attribute is updated. Send it after, and a
        60 damage hit on a 100 HP Pokemon compares 60 against the new current
        of 40 and animates a knockout that did not happen.
        """
        detail = change.detail or {}
        attacker_slot = self.resolve_slot((self._attack or {}).get("slot"))
        attacker_cid = attacker_slot.stack[-1] if attacker_slot else None
        attacker = self.card(attacker_cid) if attacker_cid is not None else None

        # The FX prefab is "Basic<Type>/HitFX_<type>_<weight>", picked from the
        # LAST entry, so an empty list falls back to Colorless rather than
        # breaking the lookup.
        damage_type = list(attacker.types) if attacker and attacker.types else []

        if detail.get("weakness"):
            modification = 1
        elif detail.get("resistance"):
            modification = 2
        elif change.amount > (detail.get("baseDamage") or 0):
            modification = 3
        elif change.amount < (detail.get("baseDamage") or 0):
            modification = 4
        else:
            modification = 0

        return ("EffectPlayed", {
            "gameID": self.game_id,
            # An effect is carried in the same name/value envelope as any
            # message, and "name" must be the first key.
            "effectMessage": {
                "name": "CakeAttackEffect",
                "value": {
                    "damageSource": self.eid(attacker_cid)
                                    if attacker_cid is not None else None,
                    "entityID": self.eid(defender_cid),
                    "weaknessTriggered": bool(detail.get("weakness")),
                    "resistanceTrigger": bool(detail.get("resistance")),
                    "damageType": damage_type,
                    "attackName": _loc_key((self._attack or {}).get("title")),
                    "damageAmount": int(change.amount or 0),
                    "damageModification": modification,
                    "visualType": 0,          # DamagingAction; 1 hides the FX
                },
            },
        })

    def _attack_source(self):
        """The attacking Pokemon's entity, for playmat attribute 201870."""
        slot = self.resolve_slot((self._attack or {}).get("slot"))
        return self.entity_of_slot(slot) if slot else None

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
        hp = ("AttributeModified", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "attribute": {"name": ATTR_HP, "value": current,
                          "originalValue": card.max_hp},
        })
        # Damage from an attack gets the whole hit: FX, a flying damage number
        # and the knockout animation. Damage from poison, burn or a confused
        # attacker has no attacker to play it from, so it just moves the bar.
        detail = change.detail or {}
        if detail.get("abilityID") and self._attack:
            effect = self._attack_effect(cid, change)
            source = self._attack_source()
            title = _loc_key(self._attack.get("title"))
            self._attack_id = self._attack.get("abilityID")
            self._attack = None
            items = []
            if source:
                # BEFORE the sequence, not inside it. The Attack sequence's
                # first statement is
                #     All[Playmat.GetAttribute(201870).Value[0]]
                # with no guard at all, so a playmat without that attribute
                # throws a NullReferenceException out of executeSequence - and
                # that exception escapes the message-pump coroutine, which
                # Unity then kills for good. Every later message piles up
                # unprocessed, which is why an attack was followed by a board
                # that accepted no clicks and a concede that did nothing.
                items.append(("msg", "AttributeModified", {
                    "gameID": self.game_id,
                    "entityID": self.playmat_id,
                    "attribute": {"name": ATTR_ABILITY_SOURCE,
                                  "value": [source]},
                }))
            children = []
            if source:
                # abilityBeginning() returns true only if an AbilityPlayedEffect
                # is among the children, and it gates the branch that lifts the
                # attacker out of its slot into the target-select area before
                # the hit plays. Without it the attacker never moves, so the
                # effect animates on a card still sitting on the board and
                # clips through it.
                children.append(("msg", "EffectPlayed", {
                    "gameID": self.game_id,
                    "effectMessage": {
                        "name": "AbilityPlayedEffect",
                        "value": {
                            "eID": source,
                            "abilityID": (self._attack_id or ""),
                            "abilityTitle": _loc(title),
                            "abilityType": "Attack",
                        },
                    },
                }))
            children.append(("msg",) + effect)
            children.append(("msg",) + hp)
            items.append(("seq", "Attack", children))
            return items
        return [hp]

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

    # Healing changes the same attribute damage does, and _change_damage
    # recomputes it from the live slot rather than from the change, so the two
    # are genuinely the same message.
    _change_heal = _change_damage

    def _change_play(self, change):
        """A Trainer was played. The move to the discard is its own Change."""
        if change.card is None:
            return []
        return [("seq", "TrainerCard", [("msg",) + self._introduce_msg(change.card)])]

    def _change_tool(self, change):
        """A Tool becomes a child of the Pokemon, like Energy does."""
        if change.card is None or change.slot is None:
            return []
        target = self.entity_of_slot(self.resolve_slot(change.slot))
        if target is None:
            return []
        return [("seq", "PlayTool",
                 [("msg",) + self._introduce_msg(change.card),
                  ("msg",) + self._move_msg(change.card, target)])]

    def _change_stadium(self, change):
        """A Stadium belongs to the playmat, not to either player's board."""
        if change.card is None:
            return []
        destination = self.playmat_zone.get(ZONE_STADIUM)
        if destination is None:
            return []
        return [("seq", "StadiumPresent",
                 [("msg",) + self._introduce_msg(change.card),
                  ("msg",) + self._move_msg(change.card, destination)])]

    def _change_ability(self, change):
        """An Ability activated. The board changes are separate Changes."""
        if change.slot is None:
            return []
        entity = self.entity_of_slot(self.resolve_slot(change.slot))
        if entity is None:
            return []
        return [("seq", "PokeAbility", [])]

    def _change_reveal(self, change):
        """Show cards to both players, one at a time, inside AlwaysReveal.

        RevealCardToAllEffect is the light one: the card flies to the present
        area, waits about half a second or a click, and comes back. The modal
        forms (RevealCardsToAllEffect, CakeRevealOpened) block the client's
        queue on model.c until a RevealClosed arrives, and model.c is the same
        sticky flag that disables the action button - not worth the risk for
        "your opponent reveals their hand".

        alwaysReveal is set because the point is to show the card even when
        the viewer could otherwise see it.
        """
        cards = (change.detail or {}).get("cards") or []
        items = []
        for cid in cards:
            # The card has to exist client-side and carry attributes, or there
            # is nothing to turn face up.
            items.append(("msg",) + self._introduce_msg(cid))
            items.append(("msg", "EffectPlayed", {
                "gameID": self.game_id,
                "effectMessage": {
                    "name": "RevealCardToAllEffect",
                    "value": {
                        "entityID": self.eid(cid),
                        "Return": True,
                        "alwaysReveal": True,
                    },
                },
            }))
        return [("seq", "AlwaysReveal", items)] if items else []

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
                    targets, hint="Optimal"):
        return {
            "entityID": entity_id,
            "selectableAction": {
                "gameID": self.game_id,
                "actionID": action_id,
                "description": description,
                "selectionType": selection_type,
                # Never "Unselectable" - PreferenceToStrength throws on it.
                # "Depleted" is the one that means "shown, but not usable".
                "actionHint": hint,
            },
            "targetInfoLst": targets,
        }

    def own_pokemon_entities(self, player):
        return [self.entity_of_slot(slot)
                for slot, _pile, _active in self.slot_entities(player)
                if slot.stack]

    def _offer_group(self, rows, decode, entity, action_id, description,
                     selection_type, by_target, prompt=None, selected=True,
                     hint="Optimal"):
        """One offered move, and how to read the answer back.

        by_target maps a target entity id to the engine Action that choosing it
        means. The point of keying on the target is that one (entity, action)
        pair can stand for several Actions - attaching an Energy to any of six
        Pokemon is one row with six targets, not six rows.

        `selected` decides whether the target list becomes a selection NODE,
        and it is not cosmetic. ActionsNode only builds a TargetInfoNode from
        TargetInformations with Selected == true; an unselected one is copied
        into predictedEntityTargetMap for hinting and the action is mapped to a
        NULL node. Picking up such a card then runs
        SelectAndAdvanceIfNotAbility, which advances onto the ActionsNode, sees
        NodeToAdvanceTo() is not an IEntityListSelection, and calls
        CancelToStart() - the card snaps back and nothing happens.

        That is exactly what "attaching moves the card but doesn't react to
        the Active" was: this used to send selected=False whenever there was
        only one target, so with a lone Active in play attaching had no node
        to drop onto at all.

        Attacks are the one case that genuinely wants selected=False: the
        defender is forced, the attack is chosen from a button rather than a
        drag, and a node there would make the player click the defender too.
        """
        targets = [t for t in by_target if t]
        if not targets:
            return
        rows.append(self._action_row(
            entity, action_id, description, selection_type,
            [self._target_info(targets, prompt, selected=selected)], hint=hint))
        decode[(entity, action_id)] = dict(by_target)

    def setup_selection(self, player, counter):
        """The real setup screen: choose an Active, then a Bench, in one offer.

        This is SelectionWithTargetsRequired, not the action offer. Its shape
        is unusual and every part of it is load-bearing:

          - targetMap is a DICT here, keyed by entity, and must have EXACTLY
            one key. With ignoreFirst set, the root node does
            `if (AvailableSelections.Count() != 1) throw` and those selections
            are precisely the targetMap keys, so two keys is a throw inside the
            message pump - fatal, and silent from here.
          - The SECOND TargetInformation becomes a CHILD of the first. That is
            the whole mechanism by which "Active, then Bench" is one offer
            rather than two, and it is why both answers come back together.
          - How many may be benched is numberToSelect on the bench entry.
            Attribute 201920 is only the layout divisor and constrains nothing.
          - The client lights up the drop zones but does NOT move the card. The
            server still has to send the EntityMoveds afterwards.

        Returns (body, cards) where cards is the ordered list of hand card ids
        the validTargets refer to, so the reply can be mapped back.
        """
        ps = self.state.players[player]
        basics = [cid for cid in ps.hand if self.card(cid).is_basic_pokemon]
        entities = [self.eid(cid) for cid in basics]
        free = max(0, self.state.rules.bench_size - len(ps.bench))
        owner = self.player_entity.get(player) or self.playmat_id

        active_info = {
            "name": "ActivePokemonTargetInformation",
            "selected": True,
            "accountID": None,
            "targetPrompt": PROMPT_SETUP_ACTIVE,
            "validTargets": list(entities),
            "numberToSelect": 1,
            "minimumToSelect": 1,
            "forced": True,
            "hintTargetMap": {},
        }
        # ONLY the Active. Chaining InitialBenchedTargetInformation after it is
        # how the real screen works, but finishing that step needs the client's
        # own Done button, and when that button does not appear the player is
        # left with a bench lit up and no way forward - a hard freeze, reported
        # twice. A chain with nothing after it advances straight to the reply,
        # so the Active always resolves on the drag, and benching is asked
        # separately as ordinary clickable rows.
        infos = [active_info]
        body = {
            "counter": counter,
            "prompt": PROMPT_SETUP_ACTIVE,
            "offerLength": 0,
            "startingTimestamp": 0,
            "forced": True,
            "ignoreFirst": True,
            "targetType": "",              # never the node name: see the docs
            "optimalPlayMap": [],
            "selectionParams": {},
            "sourceID": None,
            "targetMap": {owner: infos},
        }
        return body, basics

    # -- pending choices ---------------------------------------------------
    #
    # An effect that stops to ask something leaves state.pending set, and until
    # it is answered NOTHING else in the game is legal - players_to_act returns
    # only the asker. So a Choice the server cannot render is not a missing
    # feature, it is a hung match, which is why every option_kind has a path
    # here and the fallback answers rather than gives up.

    #: Button indexes of mulligan_selection, and what each answer means.
    MULLIGAN_NO, MULLIGAN_YES, MULLIGAN_NO_REST, MULLIGAN_YES_REST = range(4)

    def mulligan_selection(self, player, counter):
        """One mulligan's compensation, asked the way the original asked it.

        Not "how many would you like" - the original server asked once per
        mulligan, numbered, and the proof is in the client's own shipped
        strings: the prompt has a "{0}" and there is a separate .drawmultiple
        key for it. A 0..N button list was this project's invention.

        Four buttons, because the DB carries all four and 23 mulligans is a
        real opening: Yes and No answer THIS one, and "Yes/No to rest (N)"
        answer every remaining one at once. Those two only appear when there
        is more than one left to answer.

        Returns (body, remaining).
        """
        ps = self.state.players[player]
        owed = ps.owed_draws
        number = ps.mulligan_draw_number
        buttons = [_loc(BUTTON_NO), _loc(BUTTON_YES)]
        if owed > 1:
            buttons.append(_loc_n(BUTTON_NO_REST, n0=owed))
            buttons.append(_loc_n(BUTTON_YES_REST, n0=owed))
        return {
            "counter": counter,
            # The numbered variant only makes sense when there was more than
            # one to begin with; with a single mulligan "for mulligan 1" reads
            # oddly and the plain key says the same thing.
            "prompt": (_loc_n(PROMPT_MULLIGAN_MULTI, n0=number)
                       if ps.owed_draws_total > 1 else _loc(PROMPT_MULLIGAN_DRAW)),
            "offerLength": 0,
            "startingTimestamp": 0,
            "sortType": "",
            "buttons": buttons,
            "sourceEntity": None,
            "kind": "",
        }, owed

    def promote_selection(self, player, counter):
        """Choose a new Active after a knockout.

        NOT an action offer. CheckShouldEndTurn (pie_d.cs:31489) does
        player1Active.get_Entity().Children.get_Item(0) with no guard, and an
        action offer while the Active slot is empty is exactly when that
        throws - which is the state a promotion is asked in. The client has a
        selection kind for this, KnockoutPokemonTargetInformation, with its own
        command in the factory, so use that instead.

        Returns (body, [slot ids]) in the order of validTargets.
        """
        ps = self.state.players[player]
        owner = self.player_entity.get(player) or self.playmat_id
        slots, entities = [], []
        for slot in ps.bench:
            entity = self.entity_of_slot(slot)
            if entity:
                slots.append(slot.slot_id)
                entities.append(entity)
        body = {
            "counter": counter,
            "prompt": PROMPT_NEW_ACTIVE,
            "offerLength": 0,
            "startingTimestamp": 0,
            # Compulsory: the game cannot continue without an Active, and
            # there is no button that could decline it.
            "forced": True,
            "ignoreFirst": True,
            "targetType": "",
            "optimalPlayMap": [],
            "selectionParams": {},
            "sourceID": None,
            "targetMap": {owner: [{
                "name": "KnockoutPokemonTargetInformation",
                "selected": True,
                "accountID": None,
                "targetPrompt": PROMPT_NEW_ACTIVE,
                "validTargets": entities,
                "numberToSelect": 1,
                "minimumToSelect": 1,
                "forced": True,
                "hintTargetMap": {},
            }]},
        }
        return body, slots

    def decode_promote_reply(self, selection, slots):
        """The slot id named by a promotion reply, or None."""
        by_entity = {}
        for slot_id in slots:
            slot = self.resolve_slot(slot_id)
            entity = self.entity_of_slot(slot) if slot else None
            if entity:
                by_entity[entity] = slot_id
        for response in (selection or {}).get("targetResponses") or []:
            for entity in (response or {}).get("entityList") or []:
                if entity in by_entity:
                    return by_entity[entity]
        return None

    def call_flip_selection(self, counter):
        """Call heads or tails. Kind "CoinFlipChoice", so the client's own
        command raises both coins (InitialUp) and sets YouPickHeadsOrTails.

        This is not decoration: nothing else raises the coins at the start of a
        game, so a flip sent without it animates a coin that is still down -
        which is why the flip was invisible.
        """
        return {
            "counter": counter,
            # Empty on purpose. PiePromptListener.CanShowPrompt requires a
            # non-empty DisplayText, so "" draws no banner at all - and the two
            # buttons say Heads and Tails, which is the whole question.
            "prompt": PROMPT_NONE,
            "offerLength": 0,
            "startingTimestamp": 0,
            "sortType": "",
            # Index 0 is heads, 1 is tails.
            "buttons": [_loc(BUTTON_HEADS), _loc(BUTTON_TAILS)],
            "sourceEntity": None,
        }

    def coin_flip_items(self, winner, heads):
        """The opening coin flip, as an InitialCoinFlip sequence.

        The InitialCoinFlip sequence reads its child's result and pushes it
        onto BOTH coin animators, which is what makes one flip show on both
        sides of the board.

        `source` is not optional and is not decorative: the effect's command
        constructor does All.get_Item(source) with no guard, so an id the
        client has never seen throws a KeyNotFoundException inside the message
        pump. It has to be an entity from a board that has already been sent -
        the player entity of whoever won the flip.
        """
        source = self.player_entity.get(winner) or self.playmat_id
        effect = ("msg", "EffectPlayed", {
            "gameID": self.game_id,
            "effectMessage": {
                "name": "MultipleCoinFlipWithContextEffect",
                "value": {
                    # 0 is heads; anything else is tails.
                    "resultLst": [0 if heads else 1],
                    "title": _loc(PROMPT_COIN_FLIP),
                    "source": source,
                    "targets": [],
                    "gameText": _loc(PROMPT_COIN_FLIP),
                },
            },
        })
        return [("seq", "InitialCoinFlip", [effect])]

    def choice_selection(self, choice, counter):
        """A Choice as a client selection. Returns (name, body, decode).

        Cards and Pokemon go through SelectionWithTargetsRequired, the same
        message the setup screen uses - one targetMap key, ignoreFirst, and a
        single EntityListTargetInformation. Cards sitting in a zone the player
        cannot see are sent as RevealEntityListTargetInformation instead, which
        carries their attributes inline so the client can draw cards it was
        never told about; that is the deck-search dialog.

        Opaque options have no entities at all and become a button list.
        """
        owner = self.player_entity.get(choice.player) or self.playmat_id
        prompt = CHOICE_PROMPTS.get(choice.prompt, CHOICE_PROMPT_DEFAULT)

        if choice.option_kind == engine.CHOICE_OPTION:
            body = {
                "counter": counter,
                "prompt": prompt,
                "offerLength": 0,
                "startingTimestamp": 0,
                "sortType": "",
                "buttons": [_loc(_option_label(o)) for o in choice.options],
                "sourceEntity": None,
                "kind": "",
            }
            return "CustomChoiceRequired", body, list(choice.options)

        if choice.option_kind == engine.CHOICE_SLOT:
            entities, options = [], []
            for slot_id in choice.options:
                slot = self.resolve_slot(slot_id)
                entity = self.entity_of_slot(slot) if slot else None
                if entity:
                    entities.append(entity)
                    options.append(slot_id)
            reveal = None
        else:
            entities, options, reveal = [], [], {}
            for cid in choice.options:
                entity = self.eid(cid)
                entities.append(entity)
                options.append(cid)
                # The client cannot render a card it has never been introduced
                # to, and cards in the deck or the prize pile never were.
                if choice.zone not in OPEN_ZONES:
                    reveal[entity] = self.card_attributes(cid)
            if not reveal:
                reveal = None

        info = {
            "name": ("RevealEntityListTargetInformation" if reveal
                     else "EntityListTargetInformation"),
            "selected": True,
            "accountID": None,
            "targetPrompt": prompt,
            "validTargets": entities,
            "numberToSelect": max(1, choice.maximum),
            "minimumToSelect": choice.minimum,
            "forced": choice.minimum > 0,
            "hintTargetMap": {},
        }
        if reveal:
            info["revealEntities"] = reveal
        body = {
            "counter": counter,
            "prompt": prompt,
            "offerLength": 0,
            "startingTimestamp": 0,
            "forced": choice.minimum > 0,
            "ignoreFirst": True,
            "targetType": "",
            "optimalPlayMap": [],
            "selectionParams": {},
            "sourceID": None,
            "targetMap": {owner: [info]},
        }
        return "SelectionWithTargetsRequired", body, options

    def decode_choice_reply(self, selection, options, choice):
        """The picks named by a SelectionWithTargets reply, as engine ids."""
        by_entity = {}
        for option in options:
            if choice.option_kind == engine.CHOICE_SLOT:
                slot = self.resolve_slot(option)
                entity = self.entity_of_slot(slot) if slot else None
            else:
                entity = self.eid(option)
            if entity:
                by_entity[entity] = option
        picks = []
        for response in (selection or {}).get("targetResponses") or []:
            for entity in (response or {}).get("entityList") or []:
                if entity in by_entity and by_entity[entity] not in picks:
                    picks.append(by_entity[entity])
        return tuple(picks[:max(1, choice.maximum)])

    def decode_setup_reply(self, selection, basics):
        """(active card, [bench cards]) from a SelectionWithTargets response.

        The response carries one EntityListTargetResponse per TargetInformation
        that was marked selected, in the order the array declared them - so
        entry 0 is the Active and entry 1 is the Bench. Returns (None, []) for
        anything unrecognised rather than raising: this is off-machine input
        and re-offering is recoverable where an exception is not.
        """
        by_entity = {self.eid(cid): cid for cid in basics}
        responses = (selection or {}).get("targetResponses") or []
        picked = []
        for response in responses:
            ids = (response or {}).get("entityList") or []
            picked.append([by_entity[e] for e in ids if e in by_entity])
        active = picked[0][0] if picked and picked[0] else None
        bench = picked[1] if len(picked) > 1 else []
        # A card cannot be both the Active and benched, and the client has been
        # seen to echo the Active back inside the bench list.
        bench = [cid for cid in bench if cid != active]
        return active, bench

    def _setup_offer(self, player, counter):
        """Benching, as ordinary clickable rows.

        The Active is chosen on the real setup screen (setup_selection); this
        is only the step after it. It deliberately does NOT use the chained
        InitialBenchedTargetInformation node, because finishing that needs the
        client's own Done button and a player who does not get one is frozen
        with a lit bench and no way forward.

        Every Basic left in hand is a row, and so is "done" - which hangs off
        the Active, the one entity in play that carries no other rows during
        setup, so nothing has to mix selection types.
        """
        rows, decode = [], {}
        bench_pile = self.pile.get((player, ZONE_BENCH))
        me = self.state.players[player]
        active_entity = (self.entity_of_slot(me.active) if me.active else None)

        for action in engine.legal_actions(self.state, player):
            if isinstance(action, engine.SetupPlaceBench):
                spots = {bench_pile: action, self.eid(action.card): action}
                for entity in self.own_pokemon_entities(player):
                    if entity and entity != active_entity:
                        spots[entity] = action
                self._offer_group(rows, decode, self.eid(action.card),
                                  ACTION_SETUP_BENCH, "PlayBasic", "Ability",
                                  spots)
            elif isinstance(action, engine.SetupDone) and active_entity:
                self._offer_group(rows, decode, active_entity,
                                  ACTION_SETUP_DONE, "EndTurn", "Ability",
                                  {active_entity: action})
        return {
            "counter": counter,
            "prompt": "playmat.prompt.choosepokemonforbench",
            "offerLength": 0,
            "startingTimestamp": 0,
            "forced": False,
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
        trainer, tool, ability = {}, {}, {}
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
            elif isinstance(action, engine.PlayTrainer):
                trainer[action.card] = action
            elif isinstance(action, engine.AttachTool):
                target = self.entity_of_slot(self.resolve_slot(action.slot))
                tool.setdefault(action.card, {})[target] = action
            elif isinstance(action, engine.UseAbility):
                # A real printed abilityID, so it belongs on the Pokemon with
                # AbilitySelection - the same treatment an attack gets.
                target = self.entity_of_slot(self.resolve_slot(action.slot))
                ability.setdefault(target, {})[action.ability_id] = action
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
            # Benching names no target, but a row with no targets builds no
            # node and cannot be dropped on at all. The drop lands on whatever
            # is under the cursor - the bench area when it is empty, one of the
            # benched Pokemon when it is not - so every one of those resolves
            # to the same action, and so does the card itself, which is what
            # dragEnded falls back to selecting.
            spots = {bench_pile: action, self.eid(card): action}
            for entity in self.own_pokemon_entities(player):
                if entity:
                    spots[entity] = action
            self._offer_group(rows, decode, self.eid(card), ACTION_PLAY_BASIC,
                              "PlayBasic", "Ability", spots)
        for target, action in retreat.items():
            self._offer_group(rows, decode, target, ACTION_RETREAT,
                              "BaseRetreat", "Ability", {target: action})
        for target, action in promote.items():
            self._offer_group(rows, decode, target, ACTION_PROMOTE,
                              "Promote", "Ability", {target: action})
        for card, action in trainer.items():
            # A Trainer names no target when it is played; anything it needs to
            # know it asks for afterwards, as a Choice.
            self._offer_group(rows, decode, self.eid(card), ACTION_TRAINER,
                              "TrainerCard", "Ability",
                              {self.pile.get((player, ZONE_DISCARD)): action})
        for card, by_target in tool.items():
            self._offer_group(rows, decode, self.eid(card), ACTION_TOOL,
                              "PlayTool", "Ability", by_target,
                              "playmat.prompt.selectapokemon")
        for target, by_ability in ability.items():
            for ability_id, action in by_ability.items():
                card = self.card(self.resolve_slot(action.slot).stack[-1])
                printed = next((a for a in card.abilities
                                if a.ability_id == ability_id), None)
                self._offer_group(
                    rows, decode, target, ability_id,
                    _loc_key(printed.title) if printed else ability_id,
                    "AbilitySelection", {target: action})

        # NOTHING but attacks goes on the Active during a turn. An end-turn row
        # here hijacked the click that asks for the attack menu: with no Energy
        # attached there were no attack rows, so clicking the Active to see its
        # attacks silently ended the turn instead. Ending the turn is the
        # client's own button, which does appear during a turn.
        active_entity = self.entity_of_slot(me.active) if me.active else None
        if opp_active and active_entity:
            # Only attacks that can actually be used.
            #
            # Offering the rest with actionHint "Depleted" was tried and is
            # wrong for this client: AbilityButtonRenderer.Render picks its
            # textures from the energy type and the GX/VSTAR flags and has no
            # affordability path at all, so an unusable attack draws a button
            # that looks identical to a usable one, does nothing when pressed,
            # and leaves the selection half-advanced. A button that lies is
            # worse than a button that is absent.
            #
            # The full attack list is still visible on the card itself - that
            # is what attribute 200740 is for - so nothing is hidden from the
            # player, only from the action menu.
            for action in attacks:
                attack = self.card(me.active.stack[-1]).attack(action.ability_id)
                self._offer_group(
                    rows, decode, active_entity, action.ability_id,
                    _loc_key(attack.title) if attack else action.ability_id,
                    "AbilitySelection", {opp_active: action},
                    selected=False)

        # A promotion is owed, not chosen: the turn cannot continue around it,
        # so the client must not be given an end-turn button to escape with.
        return {
            "counter": counter,
            "prompt": PROMPT_CHOOSE_ACTION,
            "offerLength": 0,                 # no client-side auto-pass
            "startingTimestamp": 0,
            # ALWAYS false. forced:true makes the root's MayCancel false, which
            # is the only way to get the button captioned "End Turn" - but it
            # makes MayAdvance false at the same time, so the button is drawn
            # and does nothing ("I should be turned off right now...").
            # forced:true on an action offer is a soft lock, not a stricter
            # prompt. Promotions, the one thing that genuinely cannot be
            # declined, are asked with their own selection instead.
            "forced": False,
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
        # Fall through to the single-candidate case below, which also covers
        # an attack offered only to be refused.
        # A forced single target is sent unselected, so the client may answer
        # with no target at all. With one candidate that is unambiguous; with
        # several it is not, and guessing would silently play the wrong move.
        if len(by_target) == 1:
            return next(iter(by_target.values()))
        return None

    # -- the opening animation --------------------------------------------

    def _single_card_areas(self):
        """Pile entities that hold exactly one card and lay it out by index."""
        if self._single_areas is None:
            self._single_areas = {
                self.pile.get((index, ZONE_ACTIVE))
                for index in range(len(self.state.players))
            } - {None}
        return self._single_areas

    def mulligan_items(self):
        """Every mulliganed hand, revealed the way the client expects.

        A mulligan is a public event: the hand with no Basic is shown to the
        opponent before it goes back in the deck. Suppressing it - which this
        did, because the raw redraws looked like cards flying in and out of the
        deck - hid a rule rather than a rendering problem.

        MulliganRevealCardsEffect carries the hands INLINE as attributes and
        introduces those entities itself, so no EntityIntroduced is needed for
        cards that are back in the deck by now. It goes inside the Mulligan
        sequence, which blocks the rest of the opening until the dialog is
        dismissed - otherwise the deal animates behind it.
        """
        # Grouped BY PLAYER, not one per mulligan. entityIDPiles is a list of
        # hands and the dialog is a carousel that pages through them, so one
        # effect carrying all of a player's mulligans is ONE dialog with next
        # and back. Sending one effect each meant fifteen mulligans were
        # fifteen separate dialogs to dismiss.
        piles = {}
        for change in self.opening:
            if change.kind != engine.CHANGE_MULLIGAN:
                continue
            hand = (change.detail or {}).get("hand") or []
            pile = {self.eid(cid): self.card_attributes(cid) for cid in hand}
            if pile:
                piles.setdefault(change.player, []).append(pile)

        items = []
        for player, hands in sorted(piles.items()):
            items.append(("msg", "EffectPlayed", {
                "gameID": self.game_id,
                "effectMessage": {
                    "name": "MulliganRevealCardsEffect",
                    "value": {
                        "player": self.account(player),
                        "entityIDPiles": hands,
                        "prompt": _loc(PROMPT_MULLIGAN_REVEAL),
                        "revealTitle": _loc(PROMPT_MULLIGAN_TITLE),
                        "revealSource": self.player_entity.get(player),
                    },
                },
            }))
        return [("seq", "Mulligan", items)] if items else []

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

        The mulligans themselves ARE shown - see mulligan_items. Only the
        card-by-card churn of the redraws is left out; the hands that were
        mulliganed are revealed properly, in the client's own carousel.

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
        # After the deal, deliberately. DealInitialHands is what lowers both
        # coins, so a reveal before it leaves the coin sitting on screen behind
        # the dialog - and the mulligans read as a summary of what happened
        # rather than an interruption, which is how the real game showed them.
        items.extend(self.mulligan_items())

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

        # No prizes here. The engine deals them only once BOTH boards are set
        # up - which is the real rule, since a card placed during setup can
        # never become a prize - so at this point every prize pile is empty.
        # They arrive later as ordinary move Changes and are grouped into
        # DealInitialPrizeCards by animation_for.
        return items

    def _introduce_msg(self, cid, slot=None):
        return ("EntityIntroduced", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "entityName": self.card_kind(cid),   # never null, or Introduce throws
            "attributeMap": self.card_attributes(cid, slot),
        })

    def _move_msg(self, cid, destination, duration=300, position=None):
        if position is None:
            # The Active area holds exactly one Pokemon, and SingleCardArea
            # lays it out by index. Appending with -1 left it sitting off
            # centre, because "wherever the list happens to put it" is not the
            # same as "the one slot there is".
            position = 0 if destination in self._single_card_areas() else -1
        return ("EntityMoved", {
            "gameID": self.game_id,
            "entityID": self.eid(cid),
            "destinationID": destination,
            "positionInParent": position,        # negative appends
            # Milliseconds, and it does NOT control the card's flight time -
            # that comes from a CurveMotion prefab chosen by the source and
            # destination zone. Its only effect is delaying the game-log line.
            "animDuration": duration,
        })
