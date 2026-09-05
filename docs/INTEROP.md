# Working with Spirit-PTCGO

[Spirit-PTCGO](https://github.com/Bratah123/Spirit-PTCGO) (GPL-3.0, Python) is
the other active PTCGO server project. This document is for maintainers of
either project deciding what is worth lifting from the other.

It is written to be useful rather than diplomatic: both projects have areas
where the other is plainly ahead.

---

## The core difference

The two projects answered the same question — *where does a card's identity and
behaviour come from?* — in opposite ways.

**Spirit-PTCGO** imports cards from an external card database and mints its own
identities:

```python
# spirit/tools/import_set.py
return str(uuid.uuid5(uuid.NAMESPACE_DNS, f"spirit.ptcgo.{card_id}"))
```

Each card then gets a hand-written Python file with its stats and a scripted
effect. This is fast to extend, covers the whole SWSH block including Crown
Zenith, and is why Spirit can add cards that never existed in PTCGO at all.

**ptcgo-retro** exports cards from the client's own shipped archetype blobs,
keeping the **real** archetype GUIDs, and drives behaviour from each card's
printed English text:

```python
@trainer(r"Discard your hand and draw " + N + r" cards\.")
```

One pattern covers every reprint. Nothing is hand-written per card, and a
reprint whose wording changed stays inert rather than inheriting the wrong
behaviour.

Neither approach dominates. Spirit trades authenticity for breadth; this
project trades breadth for authenticity and for the ability to be *sure* a card
does what it says.

| | ptcgo-retro | Spirit-PTCGO |
| --- | --- | --- |
| Card GUIDs | real, UUIDv4, from the client | minted, UUIDv5 |
| Card stats | from the local install | external card API |
| Card art | original Unity bundles | downloaded images |
| Card behaviour | regex over printed text | one script per card |
| Foil masks | original, 99.9% resolved | procedurally generated |
| Bundles | read only | **read and write (UnityPy)** |
| Landing page | black | **working** |
| Rules engine | deep, engine/protocol split, 292 tests | broad, per-card |
| Animation | motion table decoded, per-change sequences | mostly stubs |

---

## What ptcgo-retro offers Spirit

### 1. A rules engine that is testable without the game

`engine.py` has no sockets, no protocol and no client knowledge:
`new_game`, `legal_actions`, `apply(state, action) -> (state, changes)`. Every
rules assumption is a named field on `Rules`, and the registries are empty by
default, so a stock engine is inert. 292 tests run with no client and no
network.

### 2. Text-driven effects instead of per-card scripts

555 Trainer printings are covered by 106 patterns, because one regex catches
every reprint. `tools/pattern_misses.py` ranks unimplemented card text by how
far into an existing pattern it gets — it found that Rare Candy had been
implemented all along and was failing on a missing space.

This composes with Spirit's approach rather than replacing it: text patterns
handle the long tail cheaply, per-card scripts handle the genuinely unique.

### 3. Real archetype and ability GUIDs

Spirit's `spirit.ptcgo.*` UUIDv5 identities cannot join to anything from the
original game. This project keeps the real ones, and the donated `AttributeDB`
carries real `archID` and `abilityID` values, attack names, per-type energy
costs, damage and full English text for **5,578** archetypes beyond what either
project has records for.

If Spirit ever wants its cards to line up with original PTCGO data — decklists,
collections, saved games, anything a real player exported — this is the missing
join, and it can be done by matching name plus attack text.

### 4. Protocol and animation archaeology

- `docs/client-protocol-notes.md` — 2,927 lines, every claim marked
  **VERIFIED / INFERRED / UNKNOWN**.
- `CLAUDE.md` — the traps. Several are the kind that cost days: a bare
  `GameMessage` is processed twice and `SerializedGameState` *throws* on the
  replay, killing the coroutine that drains the queue; `ParallelSequence`
  always throws; ~40 effect classes have no consumer and are silently dropped;
  `MulliganChoiceRequired` has a node kind of `""` and stalls forever.
- `tools/motion_table.py` — reads the client's real `CurveMotion` lookup table
  out of `resources.assets` and audits whether the stacks a game emits match a
  row. Card flight is chosen by the **sequence stack**, not by `animDuration`,
  which controls nothing but a game-log delay.

### 5. An offer's targets are its drop zones

Worth stating on its own because it is not obvious and it silently breaks
gameplay: a `SelectableAction` row's `validTargets` are the only entities a
dragged card may be dropped on. An action that needs no target still has to
list somewhere a player would actually aim, or the card snaps back and the game
appears frozen.

---

## What Spirit offers ptcgo-retro

### 1. Writing Unity AssetBundles (the big one)

`re_tools/create_card_bundle.py` loads a template bundle with **UnityPy**,
clones its Texture2D prototype, swaps in PNGs and rewrites the container
mapping; `auto_bundle.py` drives it with mtime-based rebuilds.

This project only *reads* bundles, with a hand-rolled UnityWeb/LZMA/DXT parser
that cannot open UnityFS at all. Adopting UnityPy would let it retire the
Mono.Cecil loose-art patch entirely — that patch exists only because the client
could not otherwise display art we hold as PNGs, and it brought its own
problems on a 32-bit client.

### 2. The landing-page contract

`dynamic_pages.py` has the template names, CTA slots, navigable scenes and the
UV rect read from the prefab. This project's landing page is black.

### 3. Foil-mask structure

`foil_mask_gen.py` documents the original masks' layout — engraving in the
**green** channel, coverage in **alpha**, ~40% duty cycle, art window at UV
`(0.17, 0.09, 0.83, 0.50)`. Useful as *measurements* even for a project that
prefers original masks to generated ones.

### 4. The "opponent is deciding" notice

Spirit sends `ObserverCustomChoiceOfferMessage` to show that the opponent is
picking heads or tails. This project's notes wrote that message off as
spectator-only, and its go-first announcement is missing as a result.

### 5. Breadth

Tournaments, an economy, versus seasons, cosmetics injection, custom packs and
theme decks, and an admin dashboard. None of it exists here.

---

## Practical notes for merging

**Licensing.** Spirit-PTCGO is GPL-3.0. Code moving *into* it must be
GPL-3.0-compatible. ptcgo-retro therefore needs an explicit licence before any
of it can be taken — unlicensed code is all-rights-reserved and cannot legally
be incorporated, regardless of intent.

**Naming.** Where the two projects merge, Spirit-PTCGO's names are the ones
that survive.

**Do not merge the card corpora.** They are keyed on incompatible identities —
real UUIDv4 on one side, minted UUIDv5 on the other — and mixing them produces
a pool where some cards can join to original game data and some cannot, with
nothing marking which is which. Pick one identity scheme per deployment. If the
goal is fidelity, the real GUIDs are recoverable for far more cards than either
project currently uses.

**Do not merge the asset postures either.** ptcgo-retro ships no game data at
all — every byte is generated locally from the user's own installation by tools
in the repo. That is a deliberate choice, and it is the reason this repository
can be public.

**Easiest first exchange**, in both directions:

| direction | item | why it is easy |
| --- | --- | --- |
| → Spirit | `tools/motion_table.py` | standalone; reads the client, no dependencies on this project |
| → Spirit | `docs/client-protocol-notes.md` | documentation, immediately usable |
| → retro | UnityPy bundle writing | a library swap plus a template bundle |
| → retro | landing-page constants | four names, four scenes, one UV rect |
