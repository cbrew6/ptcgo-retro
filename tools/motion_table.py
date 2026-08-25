"""
The client's CurveMotion lookup table, and whether our stacks match it.

This exists because of one line in the client:

    CurveMotion GetPathFor(Stack<string> sequenceStack)
    {
        a.C c = new a.C(sequenceStack);
        prefabLoader.GetLookup().TryGetFirst(c, out var entry);
        GameObject original = Resources.Load<GameObject>(entry.ResourcePath);
        GameObject gameObject = UnityEngine.Object.Instantiate(original);
    }

`TryGetFirst` is not checked. When nothing matches, `entry` is default, its
ResourcePath is null, Resources.Load returns null, and Instantiate(null) raises

    ArgumentException: The Object you want to instantiate is null

inside the animation coroutine - which Unity then kills. Nothing drains the
message queue after that, so every later message is silently ignored and the
game is frozen with the card sitting on the board.

So a sequence stack that matches no row is NOT a cosmetic fallback to a generic
curve, which is what I assumed for a long time. It is fatal. That assumption is
why "N freezes the game" took so many attempts: the effect was correct, the
messages were correct, and the crash was in choosing an animation for one of
them.

The table lives in resources.assets as one row per prefab: the resource path,
then the keys that row matches on. Rows range from four keys
(AttachEnergy | FromHand | ToActivePokemonAreaAttachment | Player1) down to two
(ToDeck | Player1), and a row matches when every key it names is present in the
stack - so a longer stack matches more specific rows, and a stack too sparse to
satisfy even the loosest row is the crash above.

Player1/Player2 are supplied by the client from the entity's owner rather than
from the stack, so they are ignored when matching here.

Usage:
    python tools/motion_table.py                 # dump the table
    python tools/motion_table.py --check         # audit what a game emits
    python tools/motion_table.py --check --deck-cards N Colress Switch
"""

import argparse
import collections
import os
import random
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import ai  # noqa: E402
import effects  # noqa: E402
import engine  # noqa: E402
import match  # noqa: E402

CARD_DIR = os.path.join(HERE, "carddata")
RESOURCES = os.path.join(
    os.path.dirname(HERE), "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data", "resources.assets")

# Supplied by the client from the entity's owner, not pushed onto the stack.
IGNORED = ("Player1", "Player2")

PREFAB = re.compile(rb"cardPathAnimations/[A-Za-z0-9_]+")
WORD = re.compile(rb"[A-Za-z_][A-Za-z0-9_]{2,50}")


def load_table(path=RESOURCES):
    """[(prefab, frozenset(keys))], one row per registration."""
    if not os.path.exists(path):
        return []
    with open(path, "rb") as fh:
        blob = fh.read()
    rows = []
    for found in PREFAB.finditer(blob):
        prefab = found.group(0).decode("latin1").split("/")[-1]
        keys = []
        for word in WORD.findall(blob[found.end():found.end() + 300]):
            text = word.decode("latin1")
            if text.startswith("cardPathAnimations"):
                break
            keys.append(text)
        keys = [k for k in keys[1:] if k not in IGNORED]
        if keys:
            rows.append((prefab, frozenset(keys)))
    return rows


def matches(rows, stack):
    """Every row this stack satisfies. Empty means the client throws.

    Case-insensitively, because the area names are not consistently cased:
    the table carries both ToActivePokemonArea and ToactivePokemonArea, and
    both Fromhand and FromHand. Comparing exactly reported the whole game as
    broken - including Draw, which plainly works - so the case is noise rather
    than signal here.
    """
    have = {k.lower() for k in stack}
    return [(prefab, keys) for prefab, keys in rows
            if {k.lower() for k in keys} <= have]


def stacks_for(m, changes):
    """The FULL stack each card flight arrives with, locations included.

    The sequence frames come from what we nest the move in; the two location
    frames come from the client, which pushes

        "From" + GetLocationNameFor(entity)
        "To"   + GetLocationNameFor(destination)

    around every move. Those are reconstructed here from the change's zones,
    which works because the engine's zone names ARE the client's unlocalized
    area names - that is deliberate, so a Change needs no translation table.

    Judging a stack on our frames alone is meaningless: almost every row in
    the table names a location, so nothing would ever match and the audit
    would report the whole game as broken. It did, the first time.
    """
    by_card = {}
    for change in changes:
        if change.kind == engine.CHANGE_MOVE and change.card is not None:
            by_card.setdefault(change.card, (change.from_zone, change.to_zone))
        elif change.kind == engine.CHANGE_ATTACH and change.card is not None:
            # Attaching parents the card to a Pokemon, which is what makes
            # GetLocationNameFor append "Attachment".
            was = by_card.get(change.card, (None, None))[0]
            by_card[change.card] = (was, "attachment")

    def walk(items, path):
        for item in items or ():
            if not isinstance(item, tuple) or not item:
                continue
            if item[0] == "seq":
                for found in walk(item[2], path + (item[1],)):
                    yield found
            elif item[0] == "msg" and item[1] == "EntityMoved":
                yield path, item[2].get("entityID")
    return walk, by_card


def zone_frames(zones):
    """("FromX", "ToY") for a move, or () when the zones are unknown."""
    source, target = zones
    frames = []
    if source:
        frames.append("From" + source)
    if target == "attachment":
        frames.append("ToactivePokemonAreaAttachment")
    elif target:
        frames.append("To" + target)
    return tuple(frames)


def build(db, rules, extra_names, seed):
    pokemon = [c for c in db if c.is_pokemon and c.stage == "Basic"]
    pokemon.sort(key=lambda c: (c.set_code or "", c.collector_number or 0))
    energy = next(c for c in db if c.is_basic_energy and c.energy_options)
    deck = [pokemon[0].guid] * 20 + [energy.guid] * 32
    for name in extra_names:
        card = next((c for c in db.by_name(name)
                     if c.guid in rules.trainer_effects), None)
        if card is not None:
            deck += [card.guid] * 4
    deck += [energy.guid] * max(0, 60 - len(deck))
    return match.Match("motion", ["a", "b"], db, [deck, list(deck)],
                       seed=seed, rules=rules)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--games", type=int, default=4)
    ap.add_argument("--deck-cards", nargs="*",
                    default=["N", "Colress", "Switch", "ProfessorJuniper"])
    args = ap.parse_args(argv)

    rows = load_table()
    if not rows:
        sys.exit("no resources.assets - the motion table cannot be read")
    print("motion table: %d rows, %d distinct key sets"
          % (len(rows), len({k for _p, k in rows})))
    if not args.check:
        for prefab, keys in sorted({(p, k) for p, k in rows}):
            print("   %-36s %s" % (prefab, " | ".join(sorted(keys))))
        return 0

    db = engine.CardDB.from_directory(CARD_DIR)
    rules = effects.rules_for(db)
    unmatched = collections.Counter()
    total = 0

    for game in range(args.games):
        m = build(db, rules, args.deck_cards, 21 + game)
        m.serialized_state(predeal=True)
        rng = random.Random(21 + game)
        revealed = False
        for _ in range(600):
            if m.state.winner is not None:
                break
            actors = engine.players_to_act(m.state)
            if not actors:
                break
            action = ai.choose(m.state, actors[0], rng)
            if action is None:
                break
            try:
                m.state, changes = engine.apply(m.state, action)
            except engine.IllegalAction:
                break
            items = m.animation_for(changes)
            walk, by_card = stacks_for(m, changes)
            eid_to_zones = {m.eid(cid): z for cid, z in by_card.items()}
            for path, eid in walk(items, ()):
                total += 1
                full = path + zone_frames(eid_to_zones.get(eid, (None, None)))
                if not matches(rows, full):
                    unmatched[full] += 1
            if not revealed and all(p.setup_done for p in m.state.players):
                revealed = True
                m.reveal_setup_items(1)

    print("\n%d card flights, %d with a stack matching no row"
          % (total, sum(unmatched.values())))
    if unmatched:
        print("\nTHESE THROW ArgumentException AND FREEZE THE GAME:")
        for path, count in unmatched.most_common():
            print("   %-48s x%d"
                  % (" / ".join(path) if path else "(no sequence at all)",
                     count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
