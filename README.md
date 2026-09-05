# ptcgo-retro

A from-scratch reimplementation of the **server** side of *Pokémon Trading Card
Game Online*, for the final client (**v2.95.0.5815**), after TPCI shut the
service down on 2023-06-05.

You can play a complete game against an AI: the deal, mulligans, the coin flip,
choosing an Active and Bench, attaching Energy, evolving, retreating,
Abilities, Trainers, attacking with real damage and hit effects, knockouts,
promotion, prizes, and a win or a loss.

**The client contains no rules.** It cannot know that you may attach one Energy
per turn — it is a renderer and an input device. Everything the game *is* lives
in this server, which is why most of this repository is a rules engine rather
than a protocol shim.

```
                 you                              this repo
        ┌──────────────────┐   TLS 39389/39390  ┌──────────────────┐
        │  PTCGO client    │ ←────────────────→ │ server.py        │  protocol
        │  (Unity 2018.4)  │                    │ match.py         │  translation
        │  renderer only   │ ←──── HTTP 8081 ──→│ engine.py        │  the rules
        └──────────────────┘   asset_server.py  │ effects.py       │  the cards
                                                └──────────────────┘
```

---

## What you need

- The official client, **v2.95.0.5815**. The download page is gone; an archived
  copy from 2023-03-26 is on the [Wayback Machine][client]. Other versions
  differ in protocol details and hardcoded ports.
- Python 3 (developed on 3.12). `Pillow` for the art tools.

[client]: https://web.archive.org/web/20230326080907/https://www.pokemon.com/us/pokemon-tcg/play-online/download/

Nothing here ships game data. `carddata/`, `donor/`, `certs/` and the asset
index are all generated locally from your own install — see
[Setup](#setup-from-a-fresh-clone).

---

## Status

Measured, not estimated — every number below comes from the code in this repo.

| | |
| --- | --- |
| Tests | **292** (`python -m unittest discover -s tests`) |
| Protocol handlers | 54 |
| Playable cards | **9,940** archetypes across 61 sets (7,367 Pokémon, 1,120 Trainers, 369 Energy) |
| Browsable-only cards | 4,747 more (SM5–SWSH10) — art and types, no rules |
| Assets | 2,602 bundles / 29,795 asset names; foils resolve for 99.9% of cards |

**Working:** login, main menu, collection, deck builder, deck save/load, pack
opening, the avatar wardrobe, matchmaking, and complete games against an AI.

**Card coverage** is the honest limit:

| | implemented |
| --- | --- |
| Trainer names | 106 of 345 |
| Trainer printings | 555 of 1,120 |
| Attack printings | 4,668 of 12,212 (38%) |
| Two-prize Pokémon | 609 recognised |
| Continuous effects | 49 |
| Activated Abilities | 10 |

A card whose text matches no pattern stays **inert rather than guessing** — it
is never offered as a legal move. Blank beats wrong: a card that silently does
something different from what it says is worse than one that does nothing.

Full detail, known bugs and roadmap: **[docs/STATUS.md](docs/STATUS.md)**.

---

## Setup from a fresh clone

**1. Certificates** — self-signed, `subject == issuer`, with SANs for
`127.0.0.1`, `localhost` and `tcgo-gateway.direwolfdigital.com`:

```sh
openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/server.key \
  -out certs/server.crt -days 3650 -config certs/san.cnf -sha256
```

**2. Card data** — exported from the archetype blobs your client already
shipped, in `StreamingAssets/<hostname>/`. See *Reading the archetype blobs* in
[CLAUDE.md](CLAUDE.md).

**3. Asset index** — `python bundle_index.py`. Without it the client's
`DoesAssetExistInManifest` gate rejects every request and the game renders
black, with no error.

**4. Point the client at localhost.** Write an override `cake.cfg` into the
client's `persistentDataPath` (it is read *before* the shipped one, so your
install stays clean):

```
%USERPROFILE%\AppData\LocalLow\The Pokémon Company International\Pokemon Trading Card Game Online\cake.cfg
```

```ini
hostname=127.0.0.1                  # was tcgo-gateway.direwolfdigital.com
versionURL=http://127.0.0.1:8081/   # was https://pie-live-dist.s3.amazonaws.com/
assetURL=http://127.0.0.1:8081/     # was https://dfsqwbwcu8r1a.cloudfront.net/
ShouldPatch=false                   # skips the dead updater
```

plus the `*AppSecret` lines copied from your own shipped config. Also mirror
`StreamingAssets\tcgo-gateway.direwolfdigital.com\` to
`StreamingAssets\127.0.0.1\` — some client paths derive from the hostname.

**To revert:** delete that `cake.cfg`.

**5. Run it.** The server must be up *before* the client logs in.

```sh
python server.py       # or: start-server.cmd
```

> **"Error loading preload data" always means the server is down.** The client
> fetches its config over HTTP before anything else. It is never an art problem.

---

## How it is put together

Four layers, deliberately separated. The split is the reason the rules are
testable without the game running.

| Module | Lines | Knows about |
| --- | --- | --- |
| `engine.py` | 3,020 | The rules. No sockets, no protocol, no client. `new_game`, `legal_actions`, `apply(state, action) -> (state, changes)` |
| `effects.py` | 3,782 | What individual cards do, driven by their printed English text |
| `match.py` | 2,510 | The only place the engine and the client meet — engine card → entity GUID, state → entity tree, `Change` → animation |
| `server.py` | 3,107 | The WARG wire protocol, sessions, matchmaking |
| `ai.py` | 825 | The opponent. Never raises inside a live match |

Supporting: `asset_server.py` (CDN stand-in), `bundle_index.py` (asset names),
`match_client.py` (headless client that plays whole games over the real
socket), `patch/` (a Mono.Cecil hook that lets the client load loose PNGs).

Two documents carry the hard-won knowledge:

- **[CLAUDE.md](CLAUDE.md)** — working notes. Traps, reverse-engineering
  technique, and a long list of things that cost real time to learn. Read it
  before changing anything.
- **[docs/client-protocol-notes.md](docs/client-protocol-notes.md)** — 2,927
  lines of protocol reference with every claim marked **VERIFIED / INFERRED /
  UNKNOWN**. Trust that marking.

### The one thing to understand first

`engine.apply()` returns a list of `Change` objects. `match.animation_for()`
turns each into client messages by dispatching on its kind:

```python
handler = getattr(self, "_change_" + change.kind, None)
```

**An unhandled kind is dropped in silence.** The engine applies it, the
server's board is correct, every test passes, and the client is simply never
told. `tests/test_match.py:ChangeCoverageTests` now fails on any kind that is
neither animated nor explicitly listed as not needing animation, so a new one
forces the decision instead of vanishing.

---

## Adding things

### A card effect

Effects are keyed by the card's **printed English rules text**, not by its
name, so one pattern covers every reprint — and a reprint whose wording changed
stays inert instead of inheriting the wrong behaviour.

```python
@trainer(r"Discard your hand and draw " + N + r" cards\.")
def _discard_hand_draw(m, card):
    """Professor Sycamore, Professor Juniper."""
    count = int(m.group(1))

    def effect(state, ctx, changes):
        ps = state.players[ctx["player"]]
        for cid in list(ps.hand):
            engine.move_card(state, cid, ZONE_DISCARD, changes)
        engine.draw_cards(state, ctx["player"], count, changes)
    return effect
```

That one decorator implemented both Professors and every printing of them.

Work the cheap shelf first: **`python tools/pattern_misses.py`** ranks
unimplemented card text by how far into an existing pattern it gets, which
separates "needs writing" from "already implemented, fails on a comma". It
found that Rare Candy — a top-five staple — had been implemented all along and
failed on a *missing space*.

Decorators available: `@trainer`, `@attack_effect`, `@attack_damage`,
`@static`, `@ability`. Effects that ask a question return a `Choice`; the
`step()` helper resumes them where they left off.

### A protocol message

Handlers dispatch by method name, so adding one is adding a method:

```python
class GameSession:
    def on_SomeMessageName(self, value):
        ...
```

The server logs `no handler for '<Name>'` for anything unimplemented — that log
is the main tool for finding what to build next.

### An animation

Card flight is chosen by a `CurveMotion` prefab looked up from the **sequence
stack**, not by any duration field you send. `tools/motion_table.py` dumps the
client's real lookup table out of `resources.assets` and audits whether the
stacks a game emits actually match a row.

---

## Verifying a change

```sh
python -m unittest discover -s tests      # 292 tests
python match_client.py --games 20         # plays whole games over the real socket
python build_decks.py                     # proves generated decks by playing them
```

`match_client.py` is the important one — it is a real client on a real socket,
and it fails the run if it never actually played. An earlier version read field
names the offer does not contain, answered null to everything, and reported 65
consecutive clean games while exercising nothing.

---

## Relationship to Spirit-PTCGO

[Spirit-PTCGO](https://github.com/Bratah123/Spirit-PTCGO) is the other active
PTCGO server project, and it is further along in several areas — it writes
Unity AssetBundles with UnityPy, has a working landing page, cosmetics
injection, tournaments and an economy.

The two projects made **opposite core decisions**, and that is what makes them
worth combining rather than duplicating:

| | ptcgo-retro | Spirit-PTCGO |
| --- | --- | --- |
| Card identity | the **real** archetype GUIDs (UUIDv4) from the client's own data | minted `uuid5("spirit.ptcgo.<id>")` |
| Card behaviour | one regex over printed text covers every reprint | one Python file per card |
| Card data | exported from the local install | imported from an external card API |
| Art | the original bundles | downloaded card images |
| Strength | rules engine, protocol archaeology, animation | asset pipeline, presentation, breadth of cards |

Neither is strictly better; they solve different halves. What transfers in each
direction, and how, is written up in **[docs/INTEROP.md](docs/INTEROP.md)**.

---

## Scope

This is a preservation project for a game that no longer has servers. It
contains **no official server code, assets, art, card data, or credentials**,
and it talks to no Pokémon or TPCI host. Everything was recovered from a client
the author already owned, and every piece of game data is generated locally by
tools in this repo from your own installation.

Pokémon and Pokémon Trading Card Game Online are trademarks of The Pokémon
Company International and Nintendo. This project is unaffiliated with, and
unendorsed by, TPCI, Nintendo, or Dire Wolf Digital.
