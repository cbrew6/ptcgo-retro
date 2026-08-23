"""
Generates playable decks and adds them to decks.json.

Both decks that existed were a single Basic species plus Energy, so a match
could only ever attach and attack: nothing evolved, because there was nothing
to evolve into. Evolution is a core mechanic the engine has always supported
and that no game had ever exercised.

This builds decks around complete evolution lines drawn from real card data -
a Basic, its Stage 1, and its Stage 2 where one exists, all from the same set
so the line actually connects - then fills to 60 with the Energy those
Pokemon's attacks ask for.

Deck legality here is ownership plus copy limits, not the deck-building rules
of a sanctioned format: the local collection grants four of every card, and
basic Energy is exempt from the four-copy limit (its set is "Free_Energy").
Every generated deck is played out by the AI against itself before being
written, because a deck that is legal and unplayable - no Basic to start, no
Energy the attacks can use - is worse than no deck at all.

Existing decks are never modified or removed, and decks.json is backed up
first. A deck whose name already exists is skipped.

Usage:
    python build_decks.py                  # show what it would add
    python build_decks.py --apply
    python build_decks.py --list-sets      # candidate sets, richest first
"""

import argparse
import collections
import copy
import json
import os
import random
import shutil
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import ai       # noqa: E402
import engine   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
DECKS = os.path.join(HERE, "decks.json")
CARDDATA = os.path.join(HERE, "carddata")

DECK_SIZE = 60
MAX_COPIES = 4
FREE_ENERGY = "Free_Energy"

# Cosmetics. Copied from an existing deck rather than invented: these are
# ArchetypeIDs the client resolves to real assets, and a made-up one renders
# nothing at all.
ATTR_COIN, ATTR_SLEEVE, ATTR_DECKBOX = 200670, 200680, 200690
ATTR_DECK_FORMATS = 10860
DEFAULT_FORMATS = ["Standard", "Modified", "Expanded", "Legacy", "Unlimited"]

# The engine reads attack costs as these names, and basic Energy provides them
# under the same names, so a line's colour is just the set of non-Colorless
# symbols its attacks ask for.
COLORLESS = "Colorless"


def load_decks():
    if not os.path.exists(DECKS):
        return []
    with open(DECKS, encoding="utf-8") as fh:
        return json.load(fh)


def basic_energy(db):
    """One basic Energy card per type it provides, preferring modern prints."""
    out = {}
    for card in db:
        if not card.is_basic_energy or not card.energy_options:
            continue
        types = {t for option in card.energy_options for t in option}
        if len(types) != 1:
            continue                       # rainbow/special, not a basic
        kind = next(iter(types))
        # Later sets print the same Energy again; any is fine, but pinning the
        # choice keeps generated decks reproducible run to run.
        best = out.get(kind)
        if best is None or (card.set_code or "") < (best.set_code or ""):
            out[kind] = card
    return out


def evolution_lines(db):
    """[(basic, stage1, stage2 or None)], only where the line really connects.

    A line is kept only when every step is in the same set. Across sets the
    names still match, but the client's own family data does not necessarily
    agree, and a Stage 1 that cannot be played onto its Basic is dead weight.
    """
    by_set = collections.defaultdict(list)
    for card in db:
        if card.is_pokemon and card.set_code:
            by_set[card.set_code].append(card)

    lines = []
    for set_code, cards in by_set.items():
        by_name = collections.defaultdict(list)
        for card in cards:
            by_name[card.name].append(card)
        for card in cards:
            if card.stage != "Stage1" or not card.evolves_from:
                continue
            basics = [c for c in by_name.get(card.evolves_from, [])
                      if c.stage == "Basic"]
            if not basics:
                continue
            twos = [c for c in cards
                    if c.stage == "Stage2" and c.evolves_from == card.name]
            lines.append((basics[0], card, twos[0] if twos else None))
    return lines


def line_colour(line):
    """The Energy symbols the line's attacks actually need.

    Colorless is deliberately not a colour: an attack costing only Colorless
    can be paid with anything, so it says nothing about what to put in the
    deck.
    """
    needed = set()
    for card in line:
        if card is None:
            continue
        for attack in card.attacks:
            for symbol, count in (attack.cost or {}).items():
                if count and symbol != COLORLESS:
                    needed.add(symbol)
    return needed


def line_cost(line):
    """Cheapest attack on the top of the line, as a total Energy count."""
    top = line[2] or line[1]
    costs = [sum((a.cost or {}).values()) for a in top.attacks]
    return min(costs) if costs else 99


def pick_lines(db, energies, count=3, seed=0):
    """Two lines that share one Energy type and can be powered.

    Sharing a colour matters: two lines wanting different Energy in a deck with
    no search or draw cards means half the Energy is dead in any given hand.
    """
    rng = random.Random(seed)
    usable = []
    for line in evolution_lines(db):
        colours = line_colour(line)
        if len(colours) != 1:
            continue                       # multicolour needs cards we lack
        colour = next(iter(colours))
        if colour not in energies:
            continue
        if not (line[2] or line[1]).attacks:
            continue
        if line_cost(line) > 3:
            continue                       # unpowerable without acceleration
        usable.append((colour, line))

    by_colour = collections.defaultdict(list)
    for colour, line in usable:
        by_colour[colour].append(line)

    out = []
    for colour, lines in sorted(by_colour.items()):
        if len(lines) < count:
            continue
        rng.shuffle(lines)
        # Complete Stage 2 lines first, then cheaper attackers. A deck with no
        # Stage 2 in it never exercises the second evolution step at all, which
        # is half of what these decks exist to make reachable.
        lines.sort(key=lambda ln: (ln[2] is None, line_cost(ln)))
        # Distinct species, or "three lines" is one line three times.
        chosen, names = [], set()
        for line in lines:
            if line[0].name in names:
                continue
            names.add(line[0].name)
            chosen.append(line)
            if len(chosen) == count:
                break
        if len(chosen) == count:
            out.append((colour, chosen))
    return out


def build_pile(lines, energy_card):
    """A 60-card list of archetype GUIDs.

    Counts follow the shape a real list uses - more Basics than Stage 1s, more
    Stage 1s than Stage 2s - because evolutions are dead cards until their
    pre-evolution is in play.
    """
    pile = []
    for basic, stage1, stage2 in lines:
        pile += [basic.guid] * MAX_COPIES
        pile += [stage1.guid] * (3 if stage2 else MAX_COPIES)
        if stage2 is not None:
            pile += [stage2.guid] * 2
    pile += [energy_card.guid] * (DECK_SIZE - len(pile))
    return pile


def deck_is_playable(db, pile, games=6):
    """Play it out against itself. Returns (ok, reason).

    A deck can satisfy every counting rule and still be unplayable, and the
    only honest way to find out is to play it.
    """
    for seed in range(games):
        try:
            state, _ = engine.new_game(db, [list(pile), list(pile)], seed=seed)
        except Exception as exc:                  # a deck must never do this
            return False, "new_game failed: %s" % exc
        rng = random.Random(seed)
        for _ in range(4000):
            if state.over:
                break
            acting = engine.players_to_act(state)
            if not acting:
                return False, "nobody to act and the game is not over"
            try:
                state, _ = engine.apply(state, ai.choose(state, acting[0], rng))
            except Exception as exc:
                return False, "apply failed: %s" % exc
        else:
            return False, "game did not finish in 4000 actions"
    return True, ""


def cosmetics_from(decks):
    """Reuse an existing deck's coin, sleeve and deck box.

    These are ArchetypeIDs the client resolves to real assets; inventing one
    renders nothing. Copying is also what makes a generated deck look like the
    others in the deck manager rather than obviously synthetic.
    """
    for deck in decks:
        attrs = {a["name"]: a for a in deck.get("attributes") or []}
        if ATTR_DECKBOX in attrs and ATTR_SLEEVE in attrs:
            return [copy.deepcopy(attrs[n])
                    for n in (ATTR_COIN, ATTR_SLEEVE, ATTR_DECKBOX)
                    if n in attrs]
    return []


def make_deck(name, pile, cosmetics):
    return {
        "deckID": str(uuid.uuid4()),
        "deckName": name,
        "attributes": cosmetics + [{
            "name": ATTR_DECK_FORMATS,
            "value": list(DEFAULT_FORMATS),
            "originalValue": list(DEFAULT_FORMATS),
        }],
        "piles": {"CakePile": pile},
    }


def describe(db, pile):
    counts = collections.Counter(pile)
    rows = []
    for guid, n in counts.most_common():
        card = db.get(guid)
        rows.append("%dx %s (%s %s)" % (
            n, card.name, card.set_code,
            card.stage or ("Energy" if card.is_energy else "")))
    return rows


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--decks", type=int, default=3)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--list-sets", action="store_true")
    args = ap.parse_args(argv)

    db = engine.CardDB.from_directory(CARDDATA)
    energies = basic_energy(db)
    print("%d cards, basic Energy for %d types" % (len(db), len(energies)))

    if args.list_sets:
        counts = collections.Counter()
        for line in evolution_lines(db):
            counts[line[0].set_code] += 1
        for set_code, n in counts.most_common(25):
            print("   %-14s %d complete lines" % (set_code, n))
        return 0

    candidates = pick_lines(db, energies, seed=args.seed)
    if not candidates:
        print("no usable evolution lines found")
        return 1

    existing = load_decks()
    have = {d.get("deckName") for d in existing}
    cosmetics = cosmetics_from(existing)

    built = []
    for colour, lines in candidates:
        if len(built) >= args.decks:
            break
        name = "%s Evolutions" % colour
        if name in have:
            print("skipping %r - a deck with that name already exists" % name)
            continue
        pile = build_pile(lines, energies[colour])
        if len(pile) != DECK_SIZE:
            print("skipping %r - built %d cards" % (name, len(pile)))
            continue
        ok, reason = deck_is_playable(db, pile)
        if not ok:
            print("skipping %r - %s" % (name, reason))
            continue
        built.append(make_deck(name, pile, cosmetics))
        print("\n%s" % name)
        for row in describe(db, pile):
            print("   %s" % row)

    if not built:
        print("\nnothing new to add")
        return 0

    if not args.apply:
        print("\n(dry run - pass --apply to add %d deck(s) to decks.json)"
              % len(built))
        return 0

    shutil.copy2(DECKS, DECKS + ".bak")
    with open(DECKS, "w", encoding="utf-8") as fh:
        json.dump(existing + built, fh, indent=1)
    print("\nadded %d deck(s); previous decks.json saved as decks.json.bak"
          % len(built))
    print("restart the server for it to reload them")
    return 0


if __name__ == "__main__":
    sys.exit(main())
