"""
Plays a whole match against the local server over the real socket.

`test_client.py` proves the handshake; this proves the game. It speaks the same
WARG framing the Unity client does, logs in, queues a match, answers the coin
call, and then responds to every offer until the server declares a winner - so
a protocol change can be checked in ten seconds without launching the game,
alt-tabbing, and clicking through four menus.

That matters more here than it normally would. The client swallows its own
exceptions: a malformed entity throws inside a Unity update loop, the coroutine
dies, and the visible result is a blank card or a frozen board rather than an
error. By the time a human notices, the cause is twenty messages back. This
harness instead asserts on the entity tree directly, so the failure names
itself.

What it checks, beyond "the server did not crash":

  - every face-up card carries the attributes the client dereferences without
    guarding (see tests/test_match.py for which, and why each one matters).
    Those cards are NOT in the served board: it arrives with everything still
    in the deck and face down, and cards are revealed one at a time by
    EntityIntroduced as the deal animates. Checking only the board therefore
    checks nothing - exactly the sort of hollow pass this file exists to
    catch - so every EntityIntroduced is checked as it arrives.
  - StartSequence/StopSequence bracketing is balanced, since a mismatch kills
    the client's message pump for the rest of the game
  - SerializedGameState arrives exactly once, wrapped, and never bare

Usage:
    python match_client.py                 # play one match, summarise
    python match_client.py --verbose       # log every message
    python match_client.py --games 20      # soak
    python match_client.py --dump state.json
"""

import argparse
import collections
import hashlib
import json
import random
import socket
import ssl
import struct
import sys
import time

HEADER = struct.Struct(">III")
GATEWAY = ("127.0.0.1", 39389)
USERNAME = "testtrainer"
PASSWORD = "hunter2"

# Attributes the client reads without a null check. Kept here as literals
# rather than imported from match.py so the harness fails when the server stops
# sending one, instead of quietly agreeing with it.
ATTR_ARCHETYPE_ID = 10000
ATTR_ASSET_NAME = 10020
ATTR_NAME_KEY = 10140
ATTR_POKEMON_TYPES = 200570
ATTR_CARD_TYPE = 200300
ATTR_BENCH_SLOTS = 201920

ENTITY_POKEMON = "com.direwolfdigital.cake.rules.entities.Pokemon"
ENTITY_TRAINER = "com.direwolfdigital.cake.rules.entities.TrainerCard"
ENTITY_ENERGY = "com.direwolfdigital.cake.rules.entities.Energy"
CARD_ENTITIES = (ENTITY_POKEMON, ENTITY_TRAINER, ENTITY_ENERGY)


class ProtocolError(Exception):
    pass


# --------------------------------------------------------------------------
# framing
# --------------------------------------------------------------------------

def connect(host, port):
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False           # the client pins nothing either
    ctx.verify_mode = ssl.CERT_NONE
    raw = socket.create_connection((host, port), timeout=15)
    return ctx.wrap_socket(raw, server_hostname=host)


def recv_exact(sock, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return bytes(buf)


def read_frame(sock):
    head = recv_exact(sock, 12)
    if head is None:
        return None
    length, rid, _flags = HEADER.unpack(head)
    body = recv_exact(sock, length - 8) if length > 8 else b""
    return rid, json.loads(body.decode()) if body else None


def write_frame(sock, obj, rid=0):
    payload = json.dumps(obj, separators=(",", ":")).encode()
    sock.sendall(HEADER.pack(len(payload) + 8, rid, 0) + payload)


def envelope(name, value=None):
    return {"name": name, "value": value}


# --------------------------------------------------------------------------
# entity tree checks
# --------------------------------------------------------------------------

def walk(entity):
    if not entity:
        return
    yield entity
    for child in entity.get("children") or []:
        for e in walk(child):
            yield e


def attrs_of(entity):
    return {a["name"]: a for a in (entity.get("attributes") or [])}


def check_card(name, eid, attributes):
    """The unguarded dereferences a *revealed card* is subject to.

    `attributes` is the attributeMap of an EntityIntroduced, or the attributes
    of an entity in the served board. None means face down, which is a
    legitimate state carrying no requirements at all.
    """
    if attributes is None or name not in CARD_ENTITIES:
        return []
    at = {a["name"]: a for a in attributes}
    problems = []
    if ATTR_NAME_KEY not in at:
        problems.append("%s %s: no 10140; HandSort.Compare throws inside "
                        "List.Sort and the hand empties every frame"
                        % (name, eid))
    if ATTR_ARCHETYPE_ID not in at:
        problems.append("%s %s: no 10000" % (name, eid))
    if name == ENTITY_POKEMON:
        types = at.get(ATTR_POKEMON_TYPES, {}).get("value")
        if not types:
            problems.append(
                "%s %s: no 200570; CardImageRenderer.getDefaultPerCardType "
                "does EnergyType.Value with no HasValue check, so this card "
                "never requests its art and renders blank" % (name, eid))
        elif not isinstance(types, list) or \
                not all(isinstance(t, str) for t in types):
            problems.append("%s %s: 200570 is %r, wanted a string array"
                            % (name, eid, types))
    return problems


def check_entities(root):
    """Structural checks on the served board.

    The board is sent pre-deal, so almost every card in it is face down by
    design; the per-card checks live in check_card and are driven by
    EntityIntroduced instead.
    """
    problems = []
    for entity in walk(root):
        name = entity.get("entityName") or "?"
        eid = entity.get("entityID")

        if not isinstance(entity.get("children"), list):
            problems.append("%s %s: children is not a list (Entities."
                            "initialize reads .Length on it)" % (name, eid))

        problems.extend(check_card(name, eid, entity.get("attributes")))

        # A bench declares its slot count or BenchLayout divides by zero.
        at = attrs_of(entity)
        key = at.get(ATTR_NAME_KEY, {}).get("value")
        if isinstance(key, dict) and key.get("id") == "bench":
            if not at.get(ATTR_BENCH_SLOTS, {}).get("value"):
                problems.append("bench %s: no 201920; layout goes NaN" % eid)
    return problems


# --------------------------------------------------------------------------
# the session
# --------------------------------------------------------------------------

class Harness:
    def __init__(self, verbose=False, rng=None):
        self.verbose = verbose
        # A deterministic first-action policy drives the server down one narrow
        # path. Soaking wants variety, so an rng may be supplied to pick among
        # the offered actions instead.
        self.rng = rng
        self.sock = None
        self.counts = collections.Counter()
        self.sequence_depth = 0
        self.sequence_errors = []
        self.state_seen = 0
        self.bare_state = False
        self.board = None
        self.problems = []
        self.revealed = 0
        self.actions_taken = 0
        self.revealed_pokemon = 0
        self.result = None
        self.turns = 0

    # -- plumbing ----------------------------------------------------------

    def send(self, name, value=None, rid=0):
        if self.verbose:
            print("   -> %s" % name)
        write_frame(self.sock, envelope(name, value), rid)

    def next_message(self, timeout=20):
        """One logical game message, with SequenceMessage unwrapped.

        Returns (name, value). Sequence bookkeeping happens here because the
        bracketing is a protocol invariant worth failing on, not something the
        callers should each remember to check.
        """
        self.sock.settimeout(timeout)
        frame = read_frame(self.sock)
        if frame is None:
            raise ProtocolError("server closed the connection")
        _rid, obj = frame
        name = (obj or {}).get("name")
        value = (obj or {}).get("value")

        if name == "SequenceMessage":
            inner = (value or {}).get("msg") or {}
            name, value = inner.get("name"), inner.get("value")
        elif name in ("SerializedGameState",):
            # Bare game messages are processed twice by the client and a bare
            # SerializedGameState permanently kills its message pump.
            self.bare_state = True

        self.counts[name] += 1
        if name == "StartSequence":
            self.sequence_depth += 1
        elif name == "StopSequence":
            self.sequence_depth -= 1
            if self.sequence_depth < 0:
                self.sequence_errors.append("StopSequence with no StartSequence")
        if name == "SerializedGameState":
            self.state_seen += 1
            self.board = (value or {}).get("entities")
            self.problems.extend(check_entities(self.board))
        elif name == "EntityIntroduced":
            # Where face-up cards actually appear. Checked here rather than on
            # the board, which is all face down by the time it is sent.
            v = value or {}
            self.revealed += 1
            if v.get("entityName") == ENTITY_POKEMON:
                self.revealed_pokemon += 1
            self.problems.extend(check_card(v.get("entityName"),
                                            v.get("entityID"),
                                            v.get("attributeMap")))

        if self.verbose:
            brief = json.dumps(value)[:160] if value else ""
            print("   <- %-38s %s" % (name, brief))
        return name, value

    def wait_for(self, wanted, timeout=25):
        """Read until one of `wanted` arrives, returning (name, value)."""
        wanted = set(wanted)
        deadline = time.time() + timeout
        while time.time() < deadline:
            name, value = self.next_message(timeout=max(1, deadline - time.time()))
            if name in wanted:
                return name, value
        raise ProtocolError("timed out waiting for %s" % ", ".join(sorted(wanted)))

    # -- phases ------------------------------------------------------------

    def login(self):
        gw = connect(*GATEWAY)
        write_frame(gw, envelope("RequestConnectionServiceWithVersion",
                                 {"clientVersion": "2.95.0.5815"}))
        _rid, obj = read_frame(gw)
        endpoint = obj["value"]["connectionEndPoint"]
        gw.close()

        host, port = endpoint.rsplit(":", 1)
        self.sock = connect(host, int(port))
        self.send("RequestSession", {"connectionInfo": {
            "hostName": "harness", "countryCode": "en_US",
            "clientParameters": {"clientVersion": "2.95.0.5815",
                                 "clientPlatform": "WindowsPlayer"}}})
        self.wait_for(["GrantedSession"])
        self.send("RequestLogin")
        self.wait_for(["RequestedAuthType"])
        self.send("StartAuthentication", {"authType": "sha1"})
        self.wait_for(["RequestedUsername"])
        self.send("RequestSaltForUser", {"username": USERNAME})
        _n, salt = self.wait_for(["DigestSalt"])
        digest = hashlib.sha1(
            (PASSWORD + ":" + salt["salt"]).encode()).hexdigest()
        self.send("AuthenticateDigest",
                  {"username": USERNAME, "digest": digest})
        _n, ok = self.wait_for(["AuthenticationSuccessful"])
        return ok["account"]["accountID"]

    def pick_deck(self, name=None):
        """A deck to queue with.

        Defaults to the one with the most distinct cards rather than the first
        60-card list, because the first one was a single species plus Energy -
        a deck that cannot evolve, cannot bench a second attacker, and so
        drives almost none of the offer code.
        """
        self.send("GetDeckList")
        _n, value = self.wait_for(["OnlineDecksFound"])
        decks = [d for d in (value.get("decks") or [])
                 if len((d.get("piles") or {}).get("CakePile") or []) >= 60]
        if not decks:
            raise ProtocolError("no 60-card deck to queue with")
        if name:
            for deck in decks:
                if deck.get("deckName") == name:
                    return deck
            raise ProtocolError("no deck named %r" % name)
        return max(decks, key=lambda d: len(set(d["piles"]["CakePile"])))

    def queue(self, deck):
        self.send("RequestQueueMatch", {
            "queueName": "casual",
            "deck": deck,
            "clientOptions": {"aiName": "Trainer", "difficulty": "intermediate"},
        })
        name, value = self.wait_for(["MatchFound", "MatchQueueJoinFailed"])
        if name == "MatchQueueJoinFailed":
            raise ProtocolError("server refused the queue: %r" % value)
        return value["gameID"]

    def play(self, go_first=True, max_actions=400):
        """Answer the coin call, then respond to offers until the game ends."""
        self.send("PlayerReady", {})
        self.wait_for(["GoFirstChoiceRequired"])
        self.send("GameCustomChoice", {"selection": 0 if go_first else 1,
                                       "counter": 1})

        for _ in range(max_actions):
            name, value = self.wait_for(
                ["SelectionWithTargetsAndActionsRequired",
                 "GameCompletedMessage"], timeout=30)
            if name == "GameCompletedMessage":
                self.result = value
                return
            self.turns += 1
            options = value.get("targetMap") or []
            # Reply shape mirrors what the real client sends: a selection
            # naming one offered action, or null to end the turn.
            selection = self._select(options)
            self.send("SelectionWithTargetsAndActions",
                      {"selection": selection,
                       "counter": value.get("counter")})
        raise ProtocolError("match did not finish in %d actions" % max_actions)

    def _select(self, target_map):
        """Choose one offered action, or pass when there is nothing.

        The reply shape is not ours to invent - core's
        Outgoing.SelectionWithTargetsAndActions builds

            [[entityID, abilityID], [TargetResponse, ...]]

        with each TargetResponse {"entityList": [id, ...], "name": ...}, and
        the server decodes exactly that. An earlier version of this method sent
        a flat {"targetID", "actionID"} dict and read fields ("targetID",
        "actions") that appear nowhere in the offer, so it found no choices,
        answered null every time, and every "clean" soak game was really the
        harness passing its way to a loss without exercising a single action.

        The harness is not trying to play well - it is trying to drive the
        server through as much of its own code as possible. With an rng it
        picks at random, which is what makes a soak explore more than the one
        path a fixed policy walks; without one it is deterministic, so a
        failure can be re-run.
        """
        choices = []
        for row in target_map or []:
            action = (row or {}).get("selectableAction") or {}
            action_id = action.get("actionID")
            entity_id = row.get("entityID")
            if not action_id or not entity_id:
                continue
            targets = []
            for info in row.get("targetInfoLst") or []:
                targets.extend((info or {}).get("validTargets") or [])
            choices.append((entity_id, action_id, targets))
        if not choices:
            return None
        if self.rng is not None:
            # Passing is a legal move the server must handle too, so it stays
            # in the pool rather than being unreachable.
            if self.rng.random() < 0.1:
                return None
            entity_id, action_id, targets = self.rng.choice(choices)
            target = self.rng.choice(targets) if targets else None
        else:
            entity_id, action_id, targets = choices[0]
            target = targets[0] if targets else None

        self.actions_taken += 1
        responses = []
        if target is not None:
            responses.append({"entityList": [target],
                              "name": "EntityListTargetResponse"})
        return [[entity_id, action_id], responses]

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass


# --------------------------------------------------------------------------

def run_one(verbose=False, dump=None, rng=None, go_first=True, deck_name=None):
    h = Harness(verbose=verbose, rng=rng)
    try:
        h.login()
        deck = h.pick_deck(deck_name)
        h.queue(deck)
        h.play(go_first=go_first)
    finally:
        h.close()
    if dump and h.board:
        with open(dump, "w", encoding="utf-8") as fh:
            json.dump(h.board, fh, indent=1)
    return h


def report(h):
    print("\n--- messages ---")
    for name, count in h.counts.most_common(18):
        print("   %-42s %d" % (name, count))

    print("\n--- checks ---")
    ok = True

    def check(label, passed, detail=""):
        nonlocal ok
        ok = ok and passed
        print("   [%s] %s%s" % ("PASS" if passed else "FAIL", label,
                                (" - " + detail) if detail else ""))

    check("SerializedGameState sent exactly once", h.state_seen == 1,
          "saw %d" % h.state_seen)
    check("SerializedGameState was wrapped, not bare", not h.bare_state,
          "a bare one permanently kills the client's message pump")
    check("sequences balanced",
          h.sequence_depth == 0 and not h.sequence_errors,
          "depth %d %s" % (h.sequence_depth, "; ".join(h.sequence_errors)))
    check("cards were actually revealed", h.revealed_pokemon > 0,
          "%d entities introduced, %d Pokemon - zero would make the check "
          "below vacuous" % (h.revealed, h.revealed_pokemon))
    check("every revealed card is renderable", not h.problems,
          "%d problem(s)" % len(h.problems))
    for problem in h.problems[:12]:
        print("        %s" % problem)
    if len(h.problems) > 12:
        print("        ... and %d more" % (len(h.problems) - 12))
    check("game reached a result", h.result is not None)
    if h.result:
        params = h.result.get("additionalParameters") or {}
        check("result carries GameResult", "GameResult" in params,
              "the summary dialog indexes it unguarded")
        print("        result: %s" % json.dumps(params)[:200])
    # Without this the soak can pass while proving nothing: an earlier version
    # read field names the offer does not contain, found no choices, and
    # answered null to every offer. Every game was "clean" and no action was
    # ever exercised.
    check("the harness actually played", h.actions_taken > 0,
          "%d of %d offers answered with an action" % (h.actions_taken, h.turns))
    print("\n   %d offers, %d answered with an action"
          % (h.turns, h.actions_taken))
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--games", type=int, default=1)
    ap.add_argument("--dump", help="write the served board to this file")
    ap.add_argument("--seed", type=int,
                    help="play randomly from this seed instead of always "
                         "taking the first offered action")
    ap.add_argument("--deck", help="queue with this deck by name")
    ap.add_argument("--quiet", action="store_true",
                    help="only report failures (for soaking)")
    args = ap.parse_args(argv)

    failures = 0
    problems = collections.Counter()
    for i in range(args.games):
        rng = random.Random(args.seed + i) if args.seed is not None else None
        if args.games > 1 and not args.quiet:
            print("\n=== game %d/%d ===" % (i + 1, args.games))
        try:
            h = run_one(verbose=args.verbose, dump=args.dump, rng=rng,
                        go_first=(i % 2 == 0), deck_name=args.deck)
        except (ProtocolError, OSError) as exc:
            print("   game %d ERROR: %s" % (i + 1, exc))
            failures += 1
            continue
        for problem in h.problems:
            problems[problem.split(":", 1)[-1].strip()[:90]] += 1
        if args.quiet:
            bad = (h.problems or h.result is None or h.sequence_depth != 0
                   or h.bare_state or h.state_seen != 1)
            if bad:
                print("   game %d FAILED" % (i + 1))
                report(h)
                failures += 1
        elif not report(h):
            failures += 1

    if problems:
        print("\n--- distinct board problems seen ---")
        for text, count in problems.most_common(20):
            print("   %4d  %s" % (count, text))
    print("\n%d/%d games clean" % (args.games - failures, args.games))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
