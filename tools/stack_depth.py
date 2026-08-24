"""
Measures the sequence stack every message we send would arrive with.

Why this matters, from a former Dire Wolf engineer describing the original
server:

    On the server the games had specific phases they went through, specific
    actions that occurred, etc, and that context put a wrapper on the messages
    sent to the client. So on the server it might look something like
    Draw Phase { Draw a Card { Move Card from Source Pile to Target Pile } }
    The innermost action is pushing out very basic messages like
    IntroduceEntity but the outer actions are wrapping it in start/end
    sequence wrappers.

That checks out against the client. `CurveMotionProvider.GetPathFor` takes the
WHOLE `Stack<string>` and does a most-specific-first lookup for the motion
prefab, and the client builds stacks like

    new Stack<string>(new string[3] { "AttachCard", "FromReveal", "ToRevealSelection" })

with a helper pushing action name, then `From<Location>`, then `To<Location>`.

So the depth is the whole point. A card's flight is chosen by matching the
stack, and a stack that is too shallow does not fail - it falls through to a
generic curve. Every card flying with the same motion is what that looks like,
and there is nothing in any log to say so.

How a stack is assembled, and the two rules that decide the number here:

  - `ConsumeQueuedMessages` gives every TOP-LEVEL command a fresh empty stack,
    and each enclosing named sequence pushes its own Name. So the frames from
    us are exactly the sequence names a message is nested inside.
  - `EntityMoved` pushes `From<loc>` and `To<loc>` around the motion itself,
    which is +2 and comes from the client rather than from us.

So an EntityMoved inside one named sequence arrives 3 deep, matching the
client's own literal above. One sent bare arrives 2 deep and can only match a
generic entry. Anything that is not a move gets exactly the frames we wrap it
in - which is why an unwrapped effect message has no choreography at all.

This plays real games and reports what we actually emit, rather than reading
the handlers, because the handlers are not where the nesting is decided - the
grouping in `_grouped` and the opening builders contribute frames too.

Usage:
    python tools/stack_depth.py
    python tools/stack_depth.py --games 5 --verbose
"""

import argparse
import collections
import os
import random
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import ai  # noqa: E402
import effects  # noqa: E402
import engine  # noqa: E402
import match  # noqa: E402

CARD_DIR = os.path.join(HERE, "carddata")

# Messages the client pushes From/To around, so they get +2 frames it supplies
# itself. Everything else arrives with exactly the frames we wrapped it in.
MOVES = {"EntityMoved"}

# What a move needs to reach the client's own shape: an action frame plus the
# two location frames. Below this it can only match a generic curve.
WANT_MOVE_DEPTH = 3

# Sequences _grouped builds directly, without going through a _change_ handler.
GROUPED_SEQUENCES = {"Knockout", "DealInitialPrizeCards", "Draw",
                     "DealInitialHands", "GroupedMove"}


def build_match(db, rules, seed):
    """A match on two real decks. Match owns the engine state and the opening."""
    pokemon = [c for c in db if c.is_pokemon and c.stage == "Basic"]
    pokemon.sort(key=lambda c: (c.set_code or "", c.collector_number or 0))
    basic = pokemon[0]
    energy = next(c for c in db if c.is_basic_energy and c.energy_options)
    deck = [basic.guid] * 20 + [energy.guid] * 40
    return match.Match("audit", ["acct-a", "acct-b"], db,
                       [deck, list(deck)], seed=seed, rules=rules)


def walk(items, path=()):
    """Yield (sequence path, message name) for every leaf message."""
    for item in items or ():
        if not isinstance(item, tuple) or not item:
            continue
        if item[0] == "seq":
            for found in walk(item[2], path + (item[1],)):
                yield found
        elif item[0] == "msg":
            yield path, item[1]


def instrument(m, rows, phase):
    """Wrap every _change_* handler so its output is attributed to it.

    Attribution has to happen at the handler, not by walking the finished
    tree, because animation_for appends handler items at top level - by the
    time the tree exists there is nothing left to say which handler built
    which branch. Anything _grouped folds into a sequence of its own bypasses
    the handlers entirely and is recorded under "(grouped)".
    """
    for attribute in dir(m):
        if not attribute.startswith("_change_"):
            continue
        original = getattr(m, attribute)
        if not callable(original):
            continue
        kind = attribute[len("_change_"):]

        def wrapper(change, _original=original, _kind=kind):
            items = _original(change) or []
            # A placement made before the setup reveal is not a play. Those
            # are deliberately bare - IntroduceInitialPokemon turns them over
            # and does their choreography - so attribute them separately
            # rather than letting them read as a miss.
            if phase["setup"]:
                _kind = "(setup)"
            for item in items:
                # Handlers may return a bare (name, body) pair, which
                # animation_for promotes to a top-level message.
                shaped = item if len(item) == 3 else ("msg",) + tuple(item)
                for path, name in walk([shaped]):
                    rows[(_kind, path, name)] += 1
            return items
        setattr(m, attribute, wrapper)


def audit(games, seed0):
    db = engine.CardDB.from_directory(CARD_DIR)
    rules = effects.rules_for(db)
    rows = collections.Counter()

    for game in range(games):
        seed = seed0 + game
        m = build_match(db, rules, seed)
        # Builds the entity tree AND the pile map every mover resolves against;
        # the server sends this before any animation, and opening_animation
        # cannot address a destination without it.
        m.serialized_state(predeal=True)
        # The opening is built by its own path, not by animation_for.
        for path, name in walk(m.opening_animation()):
            rows[("(opening)", path, name)] += 1

        seen_before = sum(rows.values())
        revealed = [False]
        phase = {"setup": True}
        instrument(m, rows, phase)
        rng = random.Random(seed)
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
            produced = m.animation_for(changes)
            # The server reveals the opponent's setup once both boards are
            # done, and that is what clears Match.setup_hidden. Without it the
            # latch stays set for the whole game and every play still looks
            # like a placement - which made this tool under-report its own fix.
            if not revealed[0] and all(p.setup_done for p in m.state.players):
                revealed[0] = True
                phase["setup"] = False
                for path, name in walk(m.reveal_setup_items(1)):
                    rows[("(reveal)", path, name)] += 1
            # Whatever the handlers did not account for came out of _grouped.
            handled = sum(rows.values()) - seen_before
            total = sum(1 for _ in walk(produced))
            for path, name in walk(produced):
                pass
            if total > handled:
                # Record the grouped-only leaves by difference of shape.
                for path, name in walk(produced):
                    if path and path[0] in GROUPED_SEQUENCES:
                        rows[("(grouped)", path, name)] += 1
            seen_before = sum(rows.values())
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--games", type=int, default=3)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    rows = audit(args.games, args.seed)
    if not rows:
        sys.exit("nothing emitted - the harness did not play")

    print("%-16s %-30s %-24s %5s  %s"
          % ("from", "sequence path", "message", "depth", "verdict"))
    for (source, path, name), count in sorted(
            rows.items(), key=lambda kv: (-kv[1], kv[0])):
        frames = len(path)
        total = frames + (2 if name in MOVES else 0)
        if source == "(setup)":
            ok, verdict = True, "setup placement, bare by design"
        elif name in MOVES:
            ok = total >= WANT_MOVE_DEPTH
            verdict = "" if ok else "SHALLOW - generic curve"
        else:
            ok = frames > 0
            verdict = "" if ok else "no sequence at all"
        if args.verbose or not ok:
            shown = " / ".join(path) if path else "(none)"
            print("%-16s %-30s %-24s %5d  %s x%d"
                  % (source[:16], shown[:30], name[:24], total, verdict, count))

    print("\n%d distinct shapes, %d messages" % (len(rows), sum(rows.values())))
    moves = sum(c for (s, p, n), c in rows.items() if n in MOVES)
    thin = sum(c for (s, p, n), c in rows.items()
               if n in MOVES and len(p) + 2 < WANT_MOVE_DEPTH
               and s != "(setup)")
    print("moves: %d, too shallow to match anything specific: %d (%.0f%%)"
          % (moves, thin, 100.0 * thin / moves if moves else 0))
    bare = sum(c for (s, p, n), c in rows.items() if n not in MOVES and not p)
    print("non-move messages with no enclosing sequence: %d" % bare)

    worst = collections.Counter()
    for (source, path, name), count in rows.items():
        shallow = (len(path) + 2 < WANT_MOVE_DEPTH) if name in MOVES \
            else not path
        if shallow and source != "(setup)":
            worst[source] += count
    if worst:
        print("\nwhere the shallow ones come from:")
        for source, count in worst.most_common():
            print("   %-18s %d" % (source, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
