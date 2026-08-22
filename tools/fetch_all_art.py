"""
Bulk card-art fetcher: downloads art for every card the client knows about.

Built to run unattended for hours, so the design goal is "never dies and never
loses work" rather than "fast":

  * Resumable.   Art already on disk is skipped, and every per-card outcome is
                 written to art_state.json, so an interrupted run picks up
                 exactly where it stopped.
  * Crash-proof. Every card is processed inside its own try/except. A bad
                 image, a dropped connection or a set that doesn't exist
                 upstream costs one card, never the run.
  * Verified.    Each download is name-checked against the client's own card
                 data before it is saved, so a wrong set mapping produces a
                 skip rather than 200 cards of wrong art.

Sources, in order of preference:

  api.pokemontcg.io   card names + hi-res image URLs, one request per SET
                      (cached to tools/setcache/, so re-runs cost no API
                      calls at all). Its "large" image is 734x1024 - exactly
                      the height the client's textures want, so there is no
                      upscaling.
  limitlesstcg CDN    fallback when the API has no entry. 460x640, so it gets
                      upscaled and looks softer; used only when there is no
                      better option.

Usage:
    python tools/fetch_all_art.py                 # everything outstanding
    python tools/fetch_all_art.py BW1 XY6         # only these sets
    python tools/fetch_all_art.py --retry         # also re-try past failures
    python tools/fetch_all_art.py --dry-run       # plan only, no downloads
"""

import io
import json
import os
import random
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CARD_DIR = os.path.join(HERE, "carddata")
TOOLS = os.path.join(HERE, "tools")
SET_CACHE = os.path.join(TOOLS, "setcache")
STATE_PATH = os.path.join(TOOLS, "art_state.json")
LOOSE_ART = os.path.join(
    os.path.dirname(HERE),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "LooseArt",
)

# PTCGO set code -> pokemontcg.io set id.
#
# PTCGO uses its own codes and they match nobody else's. Each entry here is
# confirmed by the name check at download time: if a mapping were wrong, every
# card in that set would report MISMATCH rather than quietly saving the wrong
# picture.
SETS = {
    "HGSS1": "hgss1", "HGSS2": "hgss2", "HGSS3": "hgss3", "HGSS4": "hgss4",
    "COL": "col1", "DV": "dv1",
    "BW1": "bw1", "BW2": "bw2", "BW3": "bw3", "BW4": "bw4", "BW5": "bw5",
    "BW6": "bw6", "BW7": "bw7", "BW8": "bw8", "BW9": "bw9", "BW10": "bw10",
    "BW11": "bw11",
    "XY0": "xy0", "XY1": "xy1", "XY2": "xy2", "XY3": "xy3", "XY4": "xy4",
    "XY5": "xy5", "XY6": "xy6", "XY7": "xy7", "XY8": "xy8", "XY9": "xy9",
    "XY10": "xy10", "XY11": "xy11",
    "SM1": "sm1", "SM2": "sm2", "SM3": "sm3", "SM4": "sm4",
    "TwentiethAnn": "g1",          # Generations
    "SL": "sm35",                  # Shining Legends
    "TATM": "dc1",                 # Double Crisis (Team Aqua vs Team Magma)
    "Promo_HGSS": "hsp", "Promo_BW": "bwp", "Promo_XY": "xyp",
    "Promo_SM": "smp",
}

# Sets whose authentic art already ships in StreamingAssets. LooseArt is
# checked BEFORE the bundle system, so writing files for these would replace
# the real thing with a third-party scan. Never touch them.
LOCAL_ART_SETS = ("XY12", "BW_Energy", "HGSS_Energy", "XY_Energy",
                  "Free_Energy", "SM_Energy")

# Sets deliberately not attempted, and why. Recorded here rather than left as
# silent gaps, so it is obvious nothing was simply forgotten.
NO_SOURCE = {
    "NoSet": "placeholder archetypes - no name or number to search on",
    "RSP": "Championship promos (Champion's Festival) - not in any public set",
    "AvatarItems": "not cards",
    "RewardItems": "not cards",
}
for _tk in ("TK5A", "TK5B", "TK6A", "TK6B", "TK7A", "TK7B", "TK8A", "TK8B",
            "TK9A", "TK9B", "TK10A", "TK10B"):
    NO_SOURCE[_tk] = "Trainer Kit reprints - no standalone set upstream"

API = "https://api.pokemontcg.io/v2/cards?q=set.id:{sid}&pageSize=250&page={page}"
LIMITLESS = ("https://limitlesstcg.nyc3.cdn.digitaloceanspaces.com"
             "/tpci/{code}/{code}_{num:03d}_R_EN_LG.png")

# Official codes for the limitless fallback, which indexes by them.
LIMITLESS_CODE = {
    "hgss1": "HS", "hgss2": "UL", "hgss3": "UD", "hgss4": "TM",
    "col1": "CL", "dv1": "DRV",
    "bw1": "BLW", "bw2": "EPO", "bw3": "NVI", "bw4": "NXD", "bw5": "DEX",
    "bw6": "DRX", "bw7": "BCR", "bw8": "PLS", "bw9": "PLF", "bw10": "PLB",
    "bw11": "LTR",
    "xy0": "KSS", "xy1": "XY", "xy2": "FLF", "xy3": "FFI", "xy4": "PHF",
    "xy5": "PRC", "xy6": "ROS", "xy7": "AOR", "xy8": "BKT", "xy9": "BKP",
    "xy10": "FCO", "xy11": "STS",
    "sm1": "SUM", "sm2": "GRI", "sm3": "BUS", "sm4": "CIN", "sm35": "SLG",
    "g1": "GEN", "dc1": "DCR",
    "hsp": "HSP", "bwp": "BWP", "xyp": "XYP", "smp": "SMP",
}

UA = "Mozilla/5.0 (compatible; ptcgo-local personal archive)"
IMAGE_DELAY = 0.35        # between image downloads
API_DELAY = 1.5           # between API calls (only ~40 of these in total)
RETRIES = 4
BACKOFF = (3, 10, 30, 60)
TIMEOUT = 60

ATTR_NAME, ATTR_NUM = 200630, 200780

# Geometry. See fetch_art.py for the full derivation; in short, the client's
# own textures are 1024x1024 with the card in a centred column and white
# either side, and the display quad crops to that column. Fit to full height,
# keep the source's aspect, centre, pad with white. Do NOT stretch to fill:
# that is what makes cards render noticeably too wide.
CARD_TEXTURE = (1024, 1024)
PAD_COLOUR = (255, 255, 255)

# Foil layers are masks, not artwork. With nothing bound the shader samples
# leftover reflection state and smears a sheen across the card, so a fully
# transparent mask ("no foil here") is written alongside every card.
FOIL_MASKS = ("wp_std", "wp_ph", "wp_pcd", "wp_secondary")
NEUTRAL_FOIL = (0, 0, 0, 0)

# A leftover SSLKEYLOGFILE from a Wireshark/mitmproxy session makes Python
# refuse to open ANY TLS connection when the path is not writable, which shows
# up here as every set failing with "Permission denied". Bulk downloading has
# no use for TLS key logging, so drop it rather than inherit it.
os.environ.pop("SSLKEYLOGFILE", None)

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is required: pip install Pillow")


def norm(s):
    """Reduce a card name to what is actually comparable across sources.

    PTCGO strips punctuation and accents ("PokeBall", "CharizardEX") where
    public databases keep them ("Poké Ball", "Charizard-EX"), so a naive
    comparison rejects every Poké/Pokédex/Pokémon card in the game. Fold
    accents to their base letters, spell out the gender symbols the same way
    PTCGO does, then keep letters and digits only. Still strict enough to
    catch a genuinely wrong set mapping.
    """
    s = (s or "")
    s = s.replace("♀", "female").replace("♂", "male")
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


# Card names carry characters cp1252 cannot represent, and a Windows console
# defaults to cp1252. Printing one raises UnicodeEncodeError - which, from
# inside the download loop, kills a multi-hour run over a character in a
# message. Ask for UTF-8, and still never trust print() with a bare call.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


# Rank markers. These are the difference between two GENUINELY different
# cards printed in the same set - Charizard and Charizard-EX are not the same
# picture - so a name difference that consists only of one of these is a real
# mismatch and must stay rejected.
RANK_MARKERS = ("ex", "gx", "break", "lvx", "star", "prime", "legend",
                "delta", "shiny", "light", "dark", "team", "v", "vmax")

# Cards where the two sources describe the same printing so differently that
# no rule relates them. Listed one by one rather than by loosening the check.
NAME_ALIASES = {
    "BW6/117": "Blend Energy GrassFirePsychicDarkness",
    "BW6/118": "Blend Energy WaterLightningFightingMetal",
}


def names_agree(mine, theirs, key=None):
    """Do these two names describe the same printing?

    Exact match after normalising is the common case. The rest are cards where
    one source carries a qualifier the other drops - PTCGO says
    "BattleCompressor" where upstream says "Battle Compressor Team Flare
    Gear", and "SpecialDarknessEnergy" where upstream says "Darkness Energy".
    One name being a prefix or suffix of the other covers those.

    The guard is RANK_MARKERS: if the ONLY thing separating the two names is a
    rank suffix then they are different cards that happen to share a base
    name, and that must still be rejected. Otherwise this rule would happily
    accept Charizard-EX's art for plain Charizard.
    """
    a, b = norm(mine), norm(theirs)
    if a == b:
        return True
    if key and key in NAME_ALIASES and norm(NAME_ALIASES[key]) == b:
        return True
    if not a or not b:
        return False
    if a.startswith(b) or b.startswith(a) or a.endswith(b) or b.endswith(a):
        extra = a[len(b):] if len(a) > len(b) else b[len(a):]
        if not extra or extra in RANK_MARKERS:
            return False
        return True
    return False


def log(msg):
    try:
        print(msg, flush=True)
    except Exception:
        enc = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)
        except Exception:
            pass          # a run must never die for want of a log line


# ---------------------------------------------------------------- state ----

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            log("  (state file unreadable - starting a fresh one)")
    return {}


def save_state(state):
    tmp = STATE_PATH + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=0, sort_keys=True)
        os.replace(tmp, STATE_PATH)
    except Exception as exc:
        log("  (could not save state: %s)" % exc)


# ------------------------------------------------------------- fetching ----

def get(url, binary=False, retries=RETRIES):
    """GET with retries. Raises only after every attempt has failed."""
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                body = r.read()
            return body if binary else body.decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise                       # genuinely absent; do not retry
            last = exc
        except Exception as exc:
            last = exc
        if attempt < retries - 1:
            time.sleep(BACKOFF[min(attempt, len(BACKOFF) - 1)]
                       + random.uniform(0, 2))
    raise last


def set_index(sid, retries=RETRIES):
    """number -> (name, image url) for one set, cached on disk.

    Cached because the API is the one rate-limited part of this: a re-run
    after an interruption must not spend its budget re-downloading metadata
    it already has.
    """
    os.makedirs(SET_CACHE, exist_ok=True)
    path = os.path.join(SET_CACHE, sid + ".json")
    if os.path.exists(path):
        try:
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:
            pass

    cards, page = [], 1
    while True:
        body = json.loads(get(API.format(sid=sid, page=page),
                              retries=retries))
        data = body.get("data", [])
        cards.extend(data)
        if len(data) < 250:
            break
        page += 1
        time.sleep(API_DELAY)

    index = {}
    for c in cards:
        images = c.get("images") or {}
        index[str(c.get("number"))] = [
            c.get("name"), images.get("large") or images.get("small")]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(index, fh)
    return index


# --------------------------------------------------------------- images ----

def to_card_texture(data):
    """Lay a card out the way the client's own textures are laid out."""
    src = Image.open(io.BytesIO(data))
    if src.mode != "RGB":
        # Flatten onto white so transparent corners match the padding rather
        # than turning black in game.
        src = src.convert("RGBA")
        flat = Image.new("RGB", src.size, PAD_COLOUR)
        flat.paste(src, mask=src.split()[3])
        src = flat
    tw, th = CARD_TEXTURE
    w = max(1, min(tw, int(round(th * src.width / float(src.height)))))
    canvas = Image.new("RGB", CARD_TEXTURE, PAD_COLOUR)
    canvas.paste(src.resize((w, th), Image.LANCZOS), ((tw - w) // 2, 0))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG")
    return buf.getvalue()


BLANK_FOIL = None


def write_foil_masks(ptcgo_set, number):
    """Blank out every foil layer for one card. Encoded once, then reused."""
    global BLANK_FOIL
    if BLANK_FOIL is None:
        buf = io.BytesIO()
        Image.new("RGBA", CARD_TEXTURE, NEUTRAL_FOIL).save(buf, format="PNG")
        BLANK_FOIL = buf.getvalue()
    written = 0
    for mask in FOIL_MASKS:
        for suffix in ("", "_Foil2"):
            path = os.path.join(
                LOOSE_ART,
                "%s_%s%s_%03d.png" % (ptcgo_set, mask, suffix, number))
            if not os.path.exists(path):
                with open(path, "wb") as fh:
                    fh.write(BLANK_FOIL)
                written += 1
    return written


# ----------------------------------------------------------------- cards ----

def local_cards(ptcgo_set):
    """(number, name) for every card in one set, from the client's own data."""
    path = os.path.join(CARD_DIR, ptcgo_set + ".json")
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return []
    out, seen = [], set()
    for a in data.get("archetypes", []):
        at = {x["n"]: (x.get("v") or {}) for x in a["attrs"]}
        num = at.get(ATTR_NUM, {}).get("i")
        name = at.get(ATTR_NAME, {}).get("s")
        if num is None or not name or num in seen:
            continue           # duplicates are reprints of the same picture
        seen.add(num)
        out.append((num, name))
    return sorted(out)


def lookup(index, number):
    """Find a card by number, tolerating how numbering is written.

    Public data stores numbers as printed ("4", "004", "RC5"); the client
    stores a plain integer. Try the obvious spellings before giving up.
    """
    for key in (str(number), "%03d" % number, "%02d" % number):
        if key in index:
            return index[key]
    return None


def fetch_card(ptcgo_set, number, expected, index, sid):
    out = os.path.join(LOOSE_ART, "%s_%03d.png" % (ptcgo_set, number))
    if os.path.exists(out):
        write_foil_masks(ptcgo_set, number)
        return "ok", "already on disk"

    entry = lookup(index, number)
    if entry:
        remote, url = entry[0], entry[1]
        if not names_agree(expected, remote, "%s/%d" % (ptcgo_set, number)):
            return ("mismatch",
                    "we have %r, upstream #%d is %r" % (expected, number, remote))
    else:
        # Not in the API's set listing. The limitless CDN indexes purely by
        # number, so it sometimes has art for cards the API is missing - but
        # with nothing to name-check against, take it only as a last resort.
        code = LIMITLESS_CODE.get(sid)
        if not code:
            return "absent", "no entry upstream"
        url = LIMITLESS.format(code=code, num=number)

    if not url:
        return "absent", "no image url upstream"

    try:
        data = get(url, binary=True)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "absent", "image 404"
        return "failed", "HTTP %s" % exc.code
    except Exception as exc:
        return "failed", str(exc)[:120]

    try:
        png = to_card_texture(data)
    except Exception as exc:
        return "failed", "decode: %s" % str(exc)[:100]

    tmp = out + ".part"
    with open(tmp, "wb") as fh:
        fh.write(png)
    os.replace(tmp, out)            # never leave a half-written PNG behind
    write_foil_masks(ptcgo_set, number)
    return "ok", "%d KB" % (len(png) // 1024)


# ------------------------------------------------------------------ main ----

def main(argv):
    retry = "--retry" in argv
    dry = "--dry-run" in argv
    only = [a for a in argv if not a.startswith("--")]

    os.makedirs(LOOSE_ART, exist_ok=True)
    state = load_state()
    started = time.time()

    considered = only or sorted(
        os.path.splitext(f)[0] for f in os.listdir(CARD_DIR)
        if f.endswith(".json"))
    skipped = []
    for s in considered:
        if s in LOCAL_ART_SETS:
            skipped.append((s, "authentic art already shipped locally"))
        elif s in NO_SOURCE:
            skipped.append((s, NO_SOURCE[s]))
        elif s not in SETS:
            skipped.append((s, "no set mapping - add one to SETS"))
    targets = [s for s in considered
               if s in SETS and s not in LOCAL_ART_SETS and s not in NO_SOURCE]

    if skipped:
        log("Not attempted:")
        for s, why in skipped:
            log("  %-14s %s" % (s, why))
        log("")

    total = sum(len(local_cards(s)) for s in targets)
    log("%d sets, %d cards. Art -> %s" % (len(targets), total, LOOSE_ART))
    log("State -> %s (safe to interrupt; re-run to resume)\n" % STATE_PATH)
    if dry:
        for s in targets:
            log("  %-14s %4d cards -> %s" % (s, len(local_cards(s)), SETS[s]))
        return

    tally = {"ok": 0, "mismatch": 0, "absent": 0, "failed": 0}
    counted = {"done": 0}

    def do_card(s, sid, index, number, expected):
        """One card. Nothing raised in here may reach the caller."""
        counted["done"] += 1
        done = counted["done"]
        key = "%s/%d" % (s, number)
        prev = state.get(key)
        if prev:
            head = prev.split(":")[0]
            if head == "ok" or not retry:
                tally[head] = tally.get(head, 0) + 1
                return
        try:
            status, detail = fetch_card(s, number, expected, index, sid)
        except Exception as exc:
            status, detail = "failed", "unexpected: %s" % str(exc)[:100]

        state[key] = status if status == "ok" else "%s:%s" % (status, detail)
        tally[status] = tally.get(status, 0) + 1

        if status == "ok":
            if detail != "already on disk":
                time.sleep(IMAGE_DELAY)
        else:
            log("    %-18s %-9s %s" % (key, status, detail))

        if done % 50 == 0:
            rate = done / max(1e-6, time.time() - started)
            left = (total - done) / rate if rate else 0
            log("  ... %d/%d  ok=%d mismatch=%d absent=%d failed=%d  ~%dm left"
                % (done, total, tally["ok"], tally["mismatch"],
                   tally["absent"], tally["failed"], left / 60))
            save_state(state)

    def do_set(s, patient=False):
        """Index one set and fetch every card in it.

        Returns False if the set could not be indexed. That metadata call is
        the one place where a single failure costs a whole set, so it gets a
        much longer retry budget than an individual image - and if it still
        fails, the set is deferred to a second pass rather than written off.
        """
        sid = SETS[s]
        cards = local_cards(s)
        try:
            index = set_index(sid, retries=10 if patient else 6)
        except Exception as exc:
            log("%-14s SET FAILED (%s)%s"
                % (s, str(exc)[:70],
                   " - giving up on its %d cards" % len(cards) if patient
                   else " - deferred, will retry at the end"))
            if patient:
                counted["done"] += len(cards)
            return False
        log("%-14s %s  (%d cards, %d upstream)"
            % (s, sid, len(cards), len(index)))
        for number, expected in cards:
            try:
                do_card(s, sid, index, number, expected)
            except Exception as exc:
                # Belt and braces: not even the bookkeeping may end the run.
                tally["failed"] = tally.get("failed", 0) + 1
                log("    %s/%s  failed  loop: %s" % (s, number, str(exc)[:80]))
        return True

    deferred = []
    try:
        for s in targets:
            try:
                if not do_set(s):
                    deferred.append(s)
            except Exception as exc:
                log("%-14s SET ERROR (%s) - continuing" % (s, str(exc)[:80]))
                deferred.append(s)
            save_state(state)

        if deferred:
            log("\nRetrying %d set(s) that could not be indexed: %s"
                % (len(deferred), ", ".join(deferred)))
            time.sleep(30)
            for s in deferred:
                try:
                    do_set(s, patient=True)
                except Exception as exc:
                    log("%-14s SET ERROR (%s)" % (s, str(exc)[:80]))
                save_state(state)
    except KeyboardInterrupt:
        log("\ninterrupted - progress saved, re-run to resume")
    finally:
        save_state(state)

    mins = (time.time() - started) / 60
    log("\nDone in %.0f min: %d saved, %d name mismatch, %d not upstream, "
        "%d failed" % (mins, tally["ok"], tally["mismatch"], tally["absent"],
                       tally["failed"]))
    if tally["failed"]:
        log("Re-run with --retry to have another go at the failures.")


if __name__ == "__main__":
    main(sys.argv[1:])
