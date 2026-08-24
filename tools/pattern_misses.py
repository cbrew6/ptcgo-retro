"""
Finds card text that ALMOST matches a pattern effects.py already has.

Coverage gaps are not all the same size. Some cards need a new effect written;
others are already implemented and fail on a comma, a plural, or - the case
that prompted this - a missing space. Rare Candy sat unplayable behind

    the Basic Pokémon(?: to evolve it)?      pattern
    the Basic Pokémon to evolve it.          shipped text

for as long as the pattern existed, because `pattern.match` either matches or
does not and there is nothing in between to notice. Those are the cheapest
cards in the pool and they are invisible next to the ones that genuinely need
work, so this separates them.

How it measures: grow the pattern SOURCE one character at a time and keep the
longest prefix that still compiles and still matches the text. The fraction of
the pattern consumed is the score, and the characters just past the cut are
where the two disagree. Prefixes that do not compile - a half-open group - are
skipped rather than counted as failures.

This is a diagnostic, not a checker. A high score means "look at this one
next", never "this card is nearly right": two cards can share ninety percent
of a sentence and still do entirely different things, which is exactly why
effects.py anchors its patterns at both ends.

Usage:
    python tools/pattern_misses.py
    python tools/pattern_misses.py --kind attacks --top 40
    python tools/pattern_misses.py --min 0.8
"""

import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import effects  # noqa: E402
import engine  # noqa: E402

CARD_DIR = os.path.join(HERE, "carddata")


def longest_prefix(source, text) -> int:
    """How many characters of the pattern source match before it stops.

    Deliberately not short-circuited on the first failure: a longer prefix can
    start matching again after a shorter one failed, because an optional group
    only becomes satisfiable once its closing paren is included.
    """
    best = 0
    for size in range(1, len(source) + 1):
        try:
            fragment = re.compile(source[:size])
        except re.error:
            continue                      # a group that is not closed yet
        if fragment.match(text):
            best = size
    return best


def unimplemented_trainers(db, loc, rules):
    seen = {}
    for card in db:
        if not card.is_trainer or card.guid in rules.trainer_effects:
            continue
        text = effects.trainer_text(card, loc)
        if text:
            seen.setdefault(card.name, (text, 0))
            seen[card.name] = (text, seen[card.name][1] + 1)
    return seen


def unimplemented_attacks(db, loc, rules):
    seen = {}
    for card in db:
        if not card.is_pokemon:
            continue
        for entry in card.abilities:
            if not entry.is_attack or not entry.ability_id:
                continue
            if (entry.ability_id in rules.attack_damage
                    or entry.ability_id in rules.attack_effects):
                continue
            text = effects.ability_text(entry, loc)
            if not text:
                continue
            key = text
            seen.setdefault(key, (text, 0))
            seen[key] = (text, seen[key][1] + 1)
    return seen


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--kind", choices=("trainers", "attacks"),
                    default="trainers")
    ap.add_argument("--top", type=int, default=20)
    ap.add_argument("--min", type=float, default=0.5,
                    help="only report a score at least this high")
    args = ap.parse_args(argv)

    db = engine.CardDB.from_directory(CARD_DIR)
    loc = effects.load_localization()
    if not loc:
        sys.exit("no localization database - nothing to read")
    rules = effects.build_rules(db, loc=loc)

    if args.kind == "trainers":
        table = effects.TRAINERS + effects.STATICS
        candidates = unimplemented_trainers(db, loc, rules)
    else:
        table = effects.ATTACK_DAMAGE + effects.ATTACK_EFFECTS
        candidates = unimplemented_attacks(db, loc, rules)

    sources = [p.pattern for p, _builder in table]
    print("%d unimplemented %s, %d patterns to compare against"
          % (len(candidates), args.kind, len(sources)))

    rows = []
    for name, (text, count) in candidates.items():
        best_score, best_source = 0.0, None
        for source in sources:
            matched = longest_prefix(source, text)
            score = matched / float(len(source))
            if score > best_score:
                best_score, best_source, best_at = score, source, matched
        if best_source is not None and best_score >= args.min:
            rows.append((best_score, best_at, name, text, best_source, count))

    rows.sort(reverse=True)
    for score, at, name, text, source, count in rows[:args.top]:
        print("\n%-26s %.0f%% of the pattern, %d printing(s)"
              % (name[:26], 100 * score, count))
        print("   TEXT  %s" % text[:150])
        print("   PAT   %s" % source[:150])
        print("   STOPS %r" % source[max(0, at - 8):at + 40])
    if not rows:
        print("\nnothing above the threshold")
    return 0


if __name__ == "__main__":
    sys.exit(main())
