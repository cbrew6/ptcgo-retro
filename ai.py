"""
The AI opponent: one call that picks one legal action for one side.

Why this exists: the shipped client is a renderer and a restored offline PTCGO
has nobody sitting on the other side of the table. server.py needs a callable
that, handed a GameState and told whose turn it is, hands back something the
engine will accept - every time, for a whole match, without ever raising. A
crash here does not fail a test, it hangs a real player's game, so `choose`
catches everything, re-checks its own answer against legal_actions(), and
falls back to Pass rather than let a bug in a heuristic escape.

This is a priority ladder, not a search. A theme-deck game has a small number
of genuinely different decisions per turn and a beginner-strength opponent is
the target, so the whole thing is "look at the board, ask eight questions in
order, take the first one that answers yes". Everything a tuner would want to
change is a named constant at the top of the file rather than a number buried
in a comparison.

The ladder, in the order it is asked:

    0. A promotion is owed  - forced; send up the best attacker.
    1. Setup                - an Active that can realistically power an
                              attack, then a small bench.
    2. Attach an Energy     - to whatever it most advances.
    3. Evolve               - only when strictly better.
    4. Play a Basic         - while the bench is thin.
    5. Retreat              - when the Active cannot act, or is nearly dead
                              and the bench is better.
    6. Attack               - a knockout if one exists (cheapest such),
                              otherwise the most damage.
    7. Pass.

Two deliberate departures from the obvious reading of that list:

  * Attacking ends the turn, so the knockout check sits *below* the free
    development steps rather than above them. Attaching an Energy, evolving
    into something at least as strong, and dropping a Basic on the bench
    provably cannot reduce this turn's damage - the engine's cost solver only
    ever gains from more attached Energy, and nothing keys off bench size - so
    doing them first is free. The knockout is still preferred over every other
    attack once step 6 is reached.

  * A planned retreat is computed before step 2 even though it happens at
    step 5, because Energy attached to a Pokemon we are about to retreat is
    Energy thrown in the discard. When a retreat is planned, the outgoing
    Active stops being a candidate for the attachment.

What this AI is allowed to know: the board, its own hand, and which Energy
*types* appear in its own decklist (a real player knows their own list; they
just do not know its order). It never reads the opponent's hand, deck or
prizes, and it never touches state.rng - the game's randomness belongs to the
game, and drawing from it here would mutate the caller's state.

Known limitations, all inherited from the engine's scope rather than chosen
here: Trainers cannot be played, PokePowers/Abilities do not exist, and attack
game text is inert, so printed damage really is the whole of an attack's
value. Special Conditions are handled where they would change a decision
(evolving cures Asleep/Paralyzed; Confusion makes a non-lethal attack risky)
but nothing in scope inflicts them, so that code is currently unreachable.
"""

from __future__ import annotations

import engine
from engine import (
    COLORLESS,
    Attack,
    AttachEnergy,
    Evolve,
    Pass,
    PlayBasic,
    Promote,
    Retreat,
    SetupDone,
    SetupPlaceActive,
    SetupPlaceBench,
    can_pay_cost,
    damage_after_weakness,
    legal_actions,
)

# --------------------------------------------------------------------------
# tunables
# --------------------------------------------------------------------------
#
# Everything is denominated in damage so the weights can be compared with one
# another: a turn spent attaching Energy costs ENERGY_TEMPO damage, an attack
# being live right now is worth READY_BONUS damage, and so on.

ENERGY_TEMPO = 15            # what a turn spent powering up costs us
READY_BONUS = 100            # an attack usable now beats one that is not
ACTIVE_BONUS = 20            # Energy on the Active does work sooner
NO_ATTACK_PENALTY = -40      # a Pokemon with no reachable attack is dead weight

BENCH_TARGET = 3             # Pokemon in play we try to keep during the game
SETUP_BENCH_TARGET = 2       # bench placements made during setup

SETUP_ATTACK_WEIGHT = 2.0    # opener scoring: attack quality dominates ...
SETUP_HP_WEIGHT = 0.1        # ... HP is a tiebreak (100 HP == 10 damage) ...
SETUP_RETREAT_PENALTY = 3.0  # ... and a heavy retreat cost is a real liability

HURT_FRACTION = 1.0 / 3.0    # "badly hurt" == this much of its HP left or less
MAX_RETREAT_UNITS = 2        # never pay more Energy than this to retreat
RETREAT_ENERGY_VALUE = 20    # damage-equivalent of one Energy burned retreating
ESCAPE_VALUE = 30            # what saving a nearly-dead Pokemon is worth
CURE_BONUS = 40              # evolving cures Asleep/Paralyzed; worth a lot

MAX_SHORTFALL = 4            # stop counting past this many missing Energy


# --------------------------------------------------------------------------
# reading the board
# --------------------------------------------------------------------------

def _slot_by_id(state, slot_id):
    found = state.slot(slot_id)
    return found[1] if found else None


def _remaining_hp(state, slot):
    return state.max_hp(slot) - slot.damage


def _energy_option_sets(state, slot):
    """Per attached Energy card, the alternative symbol sets it can provide.

    This is the shape engine.can_pay_cost() wants. It is assembled from the
    public Card.energy_options rather than re-derived: an Energy whose text is
    not implemented offers [()] and pays for nothing, the same conservative
    answer the engine gives itself.
    """
    sets = []
    for cid in slot.energy:
        options = state.card(cid).energy_options
        sets.append(list(options) if options else [()])
    return sets


def _cost_size(attack):
    return sum(n for n in attack.cost.values() if n > 0)


def _shortfall(option_sets, attack, cap=MAX_SHORTFALL):
    """How many *more* ideally-coloured Energy this attack still needs.

    Zero means it can be used now. The count comes from asking the engine's
    own cost solver whether k hypothetical perfect Energy would close the gap,
    where "perfect" is an Energy that can be any colour the cost names - so
    the answer respects Colorless-takes-anything without re-implementing it.
    """
    wild = [(t,) for t, n in attack.cost.items() if n > 0 and t != COLORLESS]
    if not wild:
        wild = [(COLORLESS,)]
    for k in range(cap + 1):
        if can_pay_cost(list(option_sets) + [wild] * k, attack.cost):
            return k
    return cap + 1


def _attacker_value(state, slot, defender_slot, card=None):
    """(damage available now, damage worth waiting for) for a Pokemon in play.

    `card` overrides the Pokemon sitting on the slot, which is how a candidate
    evolution is scored before it is played: the Energy already attached stays
    on the slot through evolution, so the same option sets apply to both.
    """
    card = card if card is not None else state.pokemon(slot)
    defender = state.pokemon(defender_slot) if defender_slot is not None else None
    options = _energy_option_sets(state, slot)

    now, potential = 0, NO_ATTACK_PENALTY
    for attack in card.attacks:
        damage = (damage_after_weakness(card, defender, attack.damage)
                  if defender is not None else attack.damage)
        need = _shortfall(options, attack)
        if need == 0:
            now = max(now, damage)
        # An attack two Energy away is worth two turns less than the same
        # attack today; that is the whole of the "potential" idea.
        potential = max(potential, damage - ENERGY_TEMPO * need)
    return now, potential


def _own_energy_symbols(state, player):
    """Which Energy colours exist anywhere in this player's own cards.

    Decklist knowledge, not hidden information: a player knows they built a
    Fire deck. Only the set of *types* is read, never where a card is.
    """
    symbols = set()
    for instance in state.cards.values():
        if instance.owner != player:
            continue
        for option in state.db.get(instance.archetype).energy_options:
            symbols.update(option)
    return symbols


def _basic_score(state, player, card):
    """How good a Basic is to put into play, before any Energy is on it.

    An attack whose colour requirement our deck cannot supply is a bluff and
    is skipped, which is what stops the AI leading with a Pokemon it could
    never turn on. A Pokemon with no reachable attack at all still scores
    NO_ATTACK_PENALTY rather than zero, so a 30 HP body that can actually
    attack beats a big one that cannot.
    """
    symbols = _own_energy_symbols(state, player)
    best = NO_ATTACK_PENALTY
    for attack in card.attacks:
        if any(t not in symbols for t, n in attack.cost.items()
               if n > 0 and t != COLORLESS):
            continue
        best = max(best, attack.damage - ENERGY_TEMPO * _cost_size(attack))
    return (SETUP_ATTACK_WEIGHT * best
            + SETUP_HP_WEIGHT * card.max_hp
            - SETUP_RETREAT_PENALTY * card.retreat_cost)


def _best(actions, key, rng=None):
    """The highest-scoring action; ties broken by rng when one is supplied.

    With no rng the first tied action in legal_actions() order wins, so
    choose() is reproducible even when the caller does not inject one.
    """
    if not actions:
        return None
    scored = [(key(action), action) for action in actions]
    top = max(score for score, _ in scored)
    tied = [action for score, action in scored if score == top]
    if rng is not None and len(tied) > 1:
        return rng.choice(tied)
    return tied[0]


# --------------------------------------------------------------------------
# the individual decisions
# --------------------------------------------------------------------------

def _plan_promotion(state, player, promotes, rng):
    """A knockout forced this. Send up whatever can hit back hardest."""
    defender = state.players[1 - player].active

    def key(action):
        slot = _slot_by_id(state, action.slot)
        now, potential = _attacker_value(state, slot, defender)
        return (now, potential, _remaining_hp(state, slot),
                -state.pokemon(slot).retreat_cost)

    return _best(promotes, key, rng)


def _plan_setup(state, player, legal, rng):
    ps = state.players[player]

    def score(action):
        return _basic_score(state, player, state.card(action.card))

    actives = [a for a in legal if isinstance(a, SetupPlaceActive)]
    if actives:
        return _best(actives, score, rng), "setup: lead with the best attacker"

    benches = [a for a in legal if isinstance(a, SetupPlaceBench)]
    if benches and len(ps.bench) < SETUP_BENCH_TARGET:
        return _best(benches, score, rng), "setup: fill the bench"

    # More bodies than SETUP_BENCH_TARGET is not free - every one of them is a
    # Pokemon the opponent can eventually take a prize on - so stop here.
    return SetupDone(player), "setup: done"


def _attach_score(state, player, action):
    """What attaching this Energy to this slot is worth, in damage.

    Zero means "this Energy does nothing here": every attack the target has is
    exactly as far away with it as without it. That is the check that keeps a
    Water Energy off a Pokemon whose only attack costs two Fire.
    """
    slot = _slot_by_id(state, action.slot)
    if slot is None:
        return 0
    card = state.card(action.card)
    defender = state.players[1 - player].active
    attacker = state.pokemon(slot)

    before = _energy_option_sets(state, slot)
    after = before + [list(card.energy_options) if card.energy_options else [()]]

    helped, best = False, 0
    for attack in attacker.attacks:
        need = _shortfall(after, attack)
        if need >= _shortfall(before, attack):
            continue                       # no closer to using this attack
        helped = True
        damage = (damage_after_weakness(attacker, state.pokemon(defender),
                                        attack.damage)
                  if defender is not None else attack.damage)
        best = max(best, damage - ENERGY_TEMPO * need
                   + (READY_BONUS if need == 0 else 0))

    if not helped:
        return 0
    if state.players[player].active is slot:
        best += ACTIVE_BONUS
    return max(best, 1)       # real progress always beats holding the card


def _plan_attach(state, player, legal, skip_slots, rng):
    candidates = [a for a in legal
                  if isinstance(a, AttachEnergy) and a.slot not in skip_slots]
    scores = {a: _attach_score(state, player, a) for a in candidates}
    useful = [a for a in candidates if scores[a] > 0]
    if not useful:
        return None
    return _best(useful, lambda a: scores[a], rng)


def _evolve_gain(state, player, action):
    """How much better this evolution makes the slot, or None if it does not.

    Strict for the Active: trading away an attack we could have used this turn
    for a bigger body is a beginner's mistake, and refusing costs nothing -
    the evolution card is still in hand next turn. A blocked Active (Asleep or
    Paralyzed) is the exception, because it has no attack to trade away and
    evolving is the only thing that clears the condition.
    """
    slot = _slot_by_id(state, action.slot)
    if slot is None:
        return None
    old = state.pokemon(slot)
    new = state.card(action.card)
    defender = state.players[1 - player].active
    is_active = state.players[player].active is slot

    old_now, old_potential = _attacker_value(state, slot, defender)
    new_now, new_potential = _attacker_value(state, slot, defender, card=new)

    blocked = is_active and bool(slot.conditions
                                 & {engine.ASLEEP, engine.PARALYZED})
    cures = is_active and bool(slot.conditions
                               & {engine.ASLEEP, engine.PARALYZED,
                                  engine.CONFUSED})

    if is_active and not blocked and new_now < old_now:
        return None
    hp_gain = new.max_hp - old.max_hp
    if hp_gain <= 0 and new_potential <= old_potential and not cures:
        return None

    return (hp_gain
            + 2 * (new_now - old_now)
            + (new_potential - old_potential)
            + (CURE_BONUS if cures else 0))


def _plan_evolve(state, player, legal, rng):
    gains = {}
    for action in legal:
        if isinstance(action, Evolve):
            gain = _evolve_gain(state, player, action)
            if gain is not None and gain > 0:
                gains[action] = gain
    if not gains:
        return None
    return _best(list(gains), lambda a: gains[a], rng)


def _plan_play_basic(state, player, legal, rng):
    """Keep a few bodies in play. A bench of one is a game lost to one KO."""
    ps = state.players[player]
    if len(ps.in_play) >= BENCH_TARGET:
        return None
    candidates = [a for a in legal if isinstance(a, PlayBasic)]
    if not candidates:
        return None
    return _best(candidates,
                 lambda a: _basic_score(state, player, state.card(a.card)), rng)


def _plan_retreat(state, player, legal, rng):
    """Swap the Active out when it cannot fight, weighing the Energy burned.

    Two reasons only: it has nothing it can attack with, or it is nearly dead
    and something healthier can do the job. Both need the incoming Pokemon to
    be a real improvement - retreating into a second problem is worse than
    standing still, because the Energy paid is gone either way.
    """
    retreats = [a for a in legal if isinstance(a, Retreat)]
    if not retreats:
        return None
    ps = state.players[player]
    active = ps.active
    if active is None:
        return None
    defender = state.players[1 - player].active
    active_now, _ = _attacker_value(state, active, defender)
    hurt = _remaining_hp(state, active) <= state.max_hp(active) * HURT_FRACTION

    # legal_actions() offers one Retreat per (destination, payment); only the
    # cheapest payment for each destination is interesting.
    cheapest = {}
    for action in retreats:
        units = sum(state.card(cid).energy_units for cid in action.energy)
        key = (len(action.energy), units)
        if action.slot not in cheapest or key < cheapest[action.slot][0]:
            cheapest[action.slot] = (key, action, units)

    best, best_key = None, None
    for slot_id, (_, action, units) in cheapest.items():
        if units > MAX_RETREAT_UNITS:
            continue
        incoming = _slot_by_id(state, slot_id)
        incoming_now, _ = _attacker_value(state, incoming, defender)
        gain = incoming_now - active_now

        if active_now == 0 and incoming_now > 0:
            worth = True                    # stuck: anything that hits is better
        elif hurt and incoming_now >= active_now and \
                _remaining_hp(state, incoming) > _remaining_hp(state, active):
            worth = gain + ESCAPE_VALUE >= RETREAT_ENERGY_VALUE * units
        else:
            worth = False
        if not worth:
            continue

        key = (gain, _remaining_hp(state, incoming), -units)
        if best_key is None or key > best_key:
            best, best_key = action, key
    return best


def _plan_attack(state, player, legal, verify=False, rng=None):
    """(action, is_knockout) for the best attack available, or (None, False).

    Damage is compared through damage_after_weakness so a Weakness is never
    missed. With `verify` set, the chosen knockout is confirmed by applying it
    to a copy of the state and looking for the defender's slot: the arithmetic
    here is a second copy of the engine's damage rules, and the day somebody
    implements Rules.attack_effects it will be the copy that is wrong.
    """
    attacks = [a for a in legal if isinstance(a, Attack)]
    if not attacks:
        return None, False
    active = state.players[player].active
    defender = state.players[1 - player].active
    if active is None or defender is None:
        return None, False

    attacker_card = state.pokemon(active)
    defender_card = state.pokemon(defender)
    remaining = _remaining_hp(state, defender)

    damage, cost = {}, {}
    for action in attacks:
        ability = attacker_card.attack(action.ability_id)
        if ability is None:            # cannot happen; ignore it rather than die
            continue
        damage[action] = damage_after_weakness(attacker_card, defender_card,
                                               ability.damage)
        cost[action] = _cost_size(ability)
    if not damage:
        return None, False

    lethal = [a for a in damage if damage[a] >= remaining]
    if lethal and verify:
        lethal = [a for a in lethal
                  if _confirm_knockout(state, a, defender.slot_id)]

    if lethal:
        # Cheapest knockout first. Attacking never discards Energy in this
        # engine, so the tiebreak is about matching what a player expects
        # rather than saving a resource - but it is the right habit for the
        # day an attack cost means something.
        return _best(lethal, lambda a: (-cost[a], damage[a]), rng), True

    # Attacking while Confused can knock our own Active out for nothing. If
    # the attack is not lethal and the tails would kill us, skip it. (Nothing
    # in scope inflicts Confusion, so this cannot fire yet.)
    if engine.CONFUSED in active.conditions and \
            state.rules.confusion_self_damage >= _remaining_hp(state, active):
        return None, False

    return _best(list(damage), lambda a: (damage[a], -cost[a]), rng), False


def _confirm_knockout(state, action, defender_slot_id):
    """Does the engine agree this attack removes that Pokemon from play?

    apply() deep-copies, so this look-ahead cannot disturb the caller's state.
    A knocked-out Pokemon is discarded, so its slot id stops resolving.
    """
    try:
        after, _ = engine.apply(state, action)
    except Exception:               # an unexpected refusal is not a knockout
        return False
    return after.slot(defender_slot_id) is None


# --------------------------------------------------------------------------
# the ladder
# --------------------------------------------------------------------------

def _decide(state, player, legal, rng):
    """Walk the priority ladder; return (action, reason)."""
    if not legal:
        return None, "nothing legal to do"

    promotes = [a for a in legal if isinstance(a, Promote)]
    if promotes:
        return _plan_promotion(state, player, promotes, rng), "forced promotion"

    if state.phase == engine.PHASE_SETUP:
        return _plan_setup(state, player, legal, rng)

    ps = state.players[player]

    # Worked out first, acted on at step 5: Energy attached to a Pokemon we
    # are about to retreat goes to the discard along with it, and a retreat
    # would throw away a knockout we could take this turn.
    _, lethal_available = _plan_attack(state, player, legal)
    retreat = None if lethal_available else _plan_retreat(state, player, legal, rng)
    skip_slots = (ps.active.slot_id,) if (retreat and ps.active) else ()

    attach = _plan_attach(state, player, legal, skip_slots, rng)
    if attach is not None:
        return attach, "attach Energy where it advances an attack"

    evolve = _plan_evolve(state, player, legal, rng)
    if evolve is not None:
        return evolve, "evolve into something strictly better"

    play_basic = _plan_play_basic(state, player, legal, rng)
    if play_basic is not None:
        return play_basic, "put another body on the bench"

    if retreat is not None:
        return retreat, "retreat to a Pokemon that can fight"

    attack, knockout = _plan_attack(state, player, legal, verify=True, rng=rng)
    if attack is not None:
        return attack, "attack for the knockout" if knockout else "attack for damage"

    return Pass(player), "nothing worth doing"


def _fallback(legal, player):
    """The safest answer when the heuristics could not produce one."""
    for action in legal:
        if isinstance(action, Pass):
            return action
    if legal:
        return legal[0]
    # Nothing is legal for this player at all - it is not their turn, or the
    # game is over. Pass is the right *shape* of answer; apply() will refuse
    # it, which is strictly better than handing None to a live server.
    return Pass(player)


def choose(state, player, rng=None):
    """Pick one legal action for `player`. Never raises, never returns None.

    `rng` breaks exact ties only, so the same state and the same generator
    state always produce the same action.
    """
    return choose_with_reason(state, player, rng)[0]


def choose_with_reason(state, player, rng=None):
    """As choose(), plus a short string saying which rung of the ladder fired.

    Worth having: it is what a server log needs to explain a move, and what a
    tuner reads to find out which heuristic to blame.
    """
    legal = []
    try:
        legal = legal_actions(state, player)
        action, reason = _decide(state, player, legal, rng)
        # A heuristic is allowed to be wrong. It is not allowed to invent an
        # action, so anything legal_actions() did not offer is discarded here.
        if action is not None and action in legal:
            return action, reason
        return _fallback(legal, player), "fallback: %s" % (reason or "no choice")
    except Exception as exc:                   # deliberate: never reach the server
        return _fallback(legal, player), "fallback after error: %r" % (exc,)
