"""
Restores set symbols and the Sun & Moon basic energies.

Two gaps that make the client look broken rather than incomplete:

  Set symbols.   The client asks for "setIcons/{set}" to draw the little
                 expansion symbol beside every card and in the set filter
                 list. The shipped setIcons bundle carries exactly four
                 (_default_set_icon, avatar, dp1, free_energy); the other 61
                 came from the CDN. With them missing the filter list renders
                 as a column of blanks, which reads as a broken filter rather
                 than a missing image.

                 A set symbol carries no gameplay information - it is the
                 expansion's printed logo, identical wherever it appears - so
                 sourcing it publicly is safe in a way a card face is not.

  SM energies.   Every other era ships its basic energies as local bundles
                 (BW_Energy, HGSS_Energy, XY_Energy, Free_Energy). Sun & Moon
                 is the one series that does not, so its nine basic energies
                 were blank - visible in every deck that plays them.

                 They come from Sun & Moon base, numbers 164-172, which is a
                 verified set+number lookup with a name check, not a match on
                 name alone. Basic energy also carries no attacks, HP or text,
                 so there is nothing for a wrong printing to misstate.

Usage:
    python tools/fetch_set_icons.py [--dry-run]
"""

import io
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOOSE_ART = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)
SETS_CACHE = os.path.join(HERE, "tools", "setcache", "_sets.json")

os.environ.pop("SSLKEYLOGFILE", None)

import importlib.util                                        # noqa: E402
_spec = importlib.util.spec_from_file_location(
    "fetch_all_art", os.path.join(HERE, "tools", "fetch_all_art.py"))
faa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(faa)

SETS_API = "https://api.pokemontcg.io/v2/sets?pageSize=250&select=id,name,ptcgoCode,images"

# PTCGO's SM_Energy #1-9 against Sun & Moon base. Written out rather than
# derived, so the pairing is reviewable: the order matches, but "the order
# matches" is not something to rely on silently.
SM_ENERGY = {
    1: ("164", "Grass Energy"),
    2: ("165", "Fire Energy"),
    3: ("166", "Water Energy"),
    4: ("167", "Lightning Energy"),
    5: ("168", "Psychic Energy"),
    6: ("169", "Fighting Energy"),
    7: ("170", "Darkness Energy"),
    8: ("171", "Metal Energy"),
    9: ("172", "Fairy Energy"),
}

# Already in the shipped setIcons bundle; leave them alone.
BUNDLED_ICONS = ("free_energy", "avatar", "dp1", "_default_set_icon")


def log(msg):
    faa.log(msg)


def all_sets():
    """id -> symbol URL, cached."""
    if os.path.exists(SETS_CACHE):
        try:
            with open(SETS_CACHE, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass
    body = json.loads(faa.get(SETS_API))
    out = {}
    for s in body.get("data", []):
        out[s["id"]] = (s.get("images") or {}).get("symbol")
    os.makedirs(os.path.dirname(SETS_CACHE), exist_ok=True)
    with open(SETS_CACHE, "w", encoding="utf-8") as fh:
        json.dump(out, fh)
    return out


def save(stem, data):
    out = os.path.join(LOOSE_ART, stem + ".png")
    tmp = out + ".part"
    with open(tmp, "wb") as fh:
        fh.write(data)
    os.replace(tmp, out)


def icons(dry):
    symbols = all_sets()
    made = skipped = 0
    for ptcgo, sid in sorted(faa.SETS.items()):
        stem = "setIcons_%s" % ptcgo.lower()
        if ptcgo.lower() in BUNDLED_ICONS:
            continue
        if os.path.exists(os.path.join(LOOSE_ART, stem + ".png")):
            skipped += 1
            continue
        url = symbols.get(sid)
        if not url:
            log("  %-14s no symbol upstream for %s" % (ptcgo, sid))
            continue
        if dry:
            made += 1
            continue
        try:
            data = faa.get(url, binary=True)
        except Exception as exc:
            log("  %-14s symbol failed: %s" % (ptcgo, str(exc)[:60]))
            continue
        # Saved as-is: a set symbol is a small transparent image, not a card
        # face, so it must NOT go through the 1024x1024 card layout.
        save(stem, data)
        made += 1
        time.sleep(0.25)
    log("set symbols: %d %s, %d already present"
        % (made, "to fetch" if dry else "written", skipped))


def energies(dry):
    path = os.path.join(HERE, "tools", "setcache", "sm1.json")
    if not os.path.exists(path):
        log("no sm1 cache - run fetch_all_art.py first")
        return
    with open(path, encoding="utf-8") as fh:
        idx = json.load(fh)
    made = 0
    for num, (upstream_num, expected) in sorted(SM_ENERGY.items()):
        stem = "SM_Energy_%03d" % num
        if os.path.exists(os.path.join(LOOSE_ART, stem + ".png")):
            continue
        entry = idx.get(upstream_num)
        if not entry:
            log("  SM_Energy/%d: sm1 #%s not upstream" % (num, upstream_num))
            continue
        remote, url = entry[0], entry[1]
        if not faa.names_agree(expected, remote):
            log("  SM_Energy/%d: sm1 #%s is %r, expected %r - skipping"
                % (num, upstream_num, remote, expected))
            continue
        if dry:
            made += 1
            continue
        try:
            data = faa.to_card_texture(faa.get(url, binary=True))
        except Exception as exc:
            log("  SM_Energy/%d failed: %s" % (num, str(exc)[:60]))
            continue
        save(stem, data)
        made += 1
        time.sleep(faa.IMAGE_DELAY)
    log("SM basic energies: %d %s" % (made, "to fetch" if dry else "written"))


def main(argv):
    dry = "--dry-run" in argv
    os.makedirs(LOOSE_ART, exist_ok=True)
    icons(dry)
    energies(dry)


if __name__ == "__main__":
    main(sys.argv[1:])
