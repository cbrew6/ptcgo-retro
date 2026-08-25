"""
Says which cards in a deck will actually do something, and which will not.

The engine never offers a card whose text it cannot read - "blank beats wrong",
because letting a player spend their one Supporter of the turn on nothing is
worse than leaving it in hand. That is the right rule and it has one bad
consequence: from the player's side an unimplemented card is INDISTINGUISHABLE
FROM A FROZEN GAME. You click it, nothing happens, and there is no message
anywhere saying why.

Every "card X froze my game" report so far has turned out to be this. So rather
than guess per card, run this against a deck and get the list.

Three reasons a Trainer ends up inert, and they are worth telling apart:

    unimplemented   the text resolves, but no pattern in effects.py matches it.
                    Fixable by writing one - this is the normal case, and 250
                    of 345 Trainer names are here.
    unreadable      the card carries a localization key that is in no database
                    we have, so there is no English to match against. Two of
                    Switch's nineteen printings are like this. Nothing can be
                    done from here.
    per-printing    the SAME card is implemented in another set. Swapping the
                    printing fixes it, which is worth knowing before rebuilding
                    a deck.

Attacks are reported too, but they differ: an attack whose text is unreadable
still deals its printed damage, so it is approximate rather than dead.

Usage:
    python tools/deck_report.py
    python tools/deck_report.py --deck TDK
    python tools/deck_report.py --all
"""

import argparse
import collections
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import effects  # noqa: E402
import engine  # noqa: E402

CARD_DIR = os.path.join(HERE, "carddata")
DECKS = os.path.join(HERE, "decks.json")


def deck_guids(deck):
    """Every archetype id in a deck, with duplicates, however it is nested."""
    found = []

    def walk(node):
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, str) and len(node) == 36 and node.count("-") == 4:
            found.append(node)
    walk(deck.get("piles"))
    return found


def classify(card, rules, loc):
    """Why this card will or will not do anything."""
    if card.is_trainer:
        if card.guid in rules.trainer_effects:
            return "works"
        return "unreadable" if not effects.trainer_text(card, loc) \
            else "unimplemented"
    if card.is_pokemon:
        attacks = [a for a in card.abilities if a.is_attack and a.ability_id]
        if not attacks:
            return "works"
        known = sum(1 for a in attacks
                    if a.ability_id in rules.attack_damage
                    or a.ability_id in rules.attack_effects)
        if known == len(attacks):
            return "works"
        return "approximate" if known else "printed damage only"
    return "works"


def other_printings(db, rules, card):
    """Sets where this same card IS implemented, if any."""
    return sorted({c.set_code for c in db.by_name(card.name)
                   if c.guid in rules.trainer_effects and c.set_code}, )


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--deck", default="", help="deck name; default is all")
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args(argv)

    if not os.path.exists(DECKS):
        sys.exit("no decks.json")
    with open(DECKS, encoding="utf-8") as fh:
        decks = json.load(fh)

    db = engine.CardDB.from_directory(CARD_DIR)
    loc = effects.load_localization()
    if not loc:
        sys.exit("no localization database - card text cannot be read")
    rules = effects.rules_for(db)

    for deck in decks:
        name = deck.get("deckName") or "(unnamed)"
        if args.deck and name != args.deck:
            continue
        guids = deck_guids(deck)
        if not guids:
            continue
        buckets = collections.defaultdict(collections.Counter)
        unknown = 0
        for guid in guids:
            # CardDB.get RAISES for an id it does not hold, and a deck can
            # legitimately contain one: the browse-only sets are served to the
            # collection but never loaded into the engine, so their v5 ids are
            # unknown here by design.
            try:
                card = db.get(guid)
            except KeyError:
                card = None
            if card is None:
                unknown += 1
                continue
            buckets[classify(card, rules, loc)][card.name] += 1

        total = len(guids)
        works = sum(buckets["works"].values())
        print("\n=== %s - %d cards, %d fully working ==="
              % (name, total, works))
        if unknown:
            print("  %d cards are not in the playable pool at all "
                  "(browse-only sets)" % unknown)
        for state in ("unimplemented", "unreadable", "printed damage only",
                      "approximate"):
            rows = buckets.get(state)
            if not rows:
                continue
            print("\n  %s:" % state.upper())
            for card_name, count in rows.most_common():
                note = ""
                if state in ("unimplemented", "unreadable"):
                    card = next(iter(db.by_name(card_name) or ()), None)
                    if card is not None and card.is_trainer:
                        elsewhere = other_printings(db, rules, card)
                        if elsewhere:
                            note = ("   <- works in %s"
                                    % ", ".join(elsewhere[:4]))
                print("    %2dx %-28s%s" % (count, card_name, note))
        if not any(buckets.get(s) for s in
                   ("unimplemented", "unreadable", "printed damage only")):
            print("  every card in this deck does what it says.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
