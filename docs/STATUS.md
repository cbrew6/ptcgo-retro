# Status

Where the project actually is, what is known broken, and what is worth doing
next. Numbers are measured from the code in this repo, not estimated.

Last measured: 2026-09-05.

---

## Working end to end

Login, the main menu, the collection, the deck builder with save/load, pack
opening, the avatar wardrobe (1,333 reconstructed items), matchmaking, and a
**complete game** against an AI:

the coin call → the flip → the winner chooses → the deal with both decks
riffling → mulligans, revealed → each player chooses an Active face down →
benching → prizes → the reveal → turns, with Energy attachment, evolution,
retreat, Abilities, Trainers, attacks with real hit effects and damage numbers,
knockouts, promotion, prizes taken, and a win or a loss.

| Subsystem | State |
| --- | --- |
| Patcher / updater | bypassed (`ShouldPatch=false`) |
| Asset preload | working |
| Gateway handshake, session, login | working |
| Post-login data load | working — 54 handlers |
| Collection / deck builder | working |
| Matchmaking → game | working |
| Gameplay | working; card coverage is the limit |
| Landing page | **black** — see below |

---

## Card coverage

The pool is 9,940 playable archetypes across 61 sets. The *usable* pool is
smaller, and this is the single biggest thing standing between the project and
"you can build any deck and it works".

| | implemented | of |
| --- | --- | --- |
| Trainer names | 106 | 345 |
| Trainer printings | 555 | 1,120 |
| Attack printings | 4,668 | 12,212 |
| Attack damage modifiers | 841 keyed | |
| Continuous / static effects | 49 | |
| Activated Abilities | 10 | |
| Two-prize Pokémon | 609 recognised | |

Effects are keyed by printed English text. A card whose wording matches no
pattern is **never offered as a legal move** — it stays inert rather than
guessing. That is deliberate: a card that quietly does the wrong thing during a
game is worse than one that does nothing.

`python tools/pattern_misses.py` ranks unimplemented text by how far into an
existing pattern it gets, which separates "needs writing" from "already
implemented, fails on a comma".

---

## Known gaps, roughly by value

### Triggered abilities do not exist
Everything is activated or continuous. Nothing fires on an event, so Rocky
Helmet, Exp. Share and every "when this Pokémon is Knocked Out" card is
correctly unimplemented rather than half-working. This is the largest remaining
rules gap. The `Pending`/`Choice` machine would carry it unchanged; it needs a
`Rules.triggers` registry fired from the existing choke points in `engine.py`
(`_apply_damage`, `_resolve_knockouts`, `_checkup`, `_do_attach`).

### Auras are not modelled
`static_effects` sees only a Pokémon's own Abilities and Tools plus the
Stadium, so a benched Pokémon buffing the Active does nothing.

### Attaching Energy to a Basic picks the wrong animation
Diagnosed precisely, not yet fixed. The client chooses a motion prefab from the
sequence stack, and every correct attach row in its table requires the
destination's location name to end in `Attachment` —
`CurveMotionProvider.GetLocationNameFor` appends that only when the
destination's own parent is a Pokémon **card**. Attaching to an evolved Pokémon
now satisfies this (evolutions nest since the fix below). Attaching to a Basic
does not, and falls through to `P1_abilitySelect_active`, which lifts the card
and slides the Energy in behind it.

Running the real table through the client's own selection rule:

| attach → | prefab chosen |
| --- | --- |
| Basic, Active | `P1_abilitySelect_active` ✗ |
| Basic, bench (opponent) | `P2_hand_benchFaceDown` ✗ |
| Evolved, Active | `P1_active_attachEnergy` / `P2_hand_attachEnergyActive` ✓ |

The likely answer is that the original server named a never-introduced
attachment anchor under each Pokémon as the destination — the walk-up loop in
`GetLocationNameFor` skips un-introduced entities, which would yield exactly
`ToactivePokemonAreaAttachment`. Unverified.

### The landing page is black
The template must be a real Resources path. The four are `LandingPageLeft`,
`LandingPageRight`, `LandingPageLeftNoButtons`, `LandingPageRightNoButtons`
(plus `SplashMaintenanceWindow`); CTA slots are `UpsellButton` and
`CodeRedeemButton`, navigable scenes are `Shop`, `ShopCodeRedemption`,
`Tournament`, `Trade`, and the prefab crops a 1920×1080 UV window from a 2048px
texture. A No-Buttons template may carry no CTA. We serve 66 LandingPage
bundles / 167 real banners already.

### Smaller
- No bench step is offered after both players choose an Active.
- Active placement lags 1–2 s after the drag-drop.
- The opponent's name renders oddly — no `avatarProfile_name_*` gameOption is
  sent.
- No handler for `UpdateUserTimeoutStatus` (log noise only).
- Choice shapes are a flat list; reordering (Pokédex), face-up prizes (Town
  Map) and Devolution Spray need shapes the renderer does not have.
- Seven BW cards render flat out of 5,322 foils.
- A Pokémon with no legal move cannot be clicked — rows are only sent for legal
  actions, so an Active that cannot attack or retreat has no node. The real
  client showed the card and greyed the buttons.
- The AI is beginner strength and will discard a good hand to a draw Supporter.

---

## The card pool ceiling

`carddata/` covers 61 sets through SM4. A further **4,747 cards (SM5–SWSH10)**
exist in `carddata_browse/` — they render in the collection with real art and
real foils, and are legal in **no** format, guarded twice so they can never
enter a game.

They carry no HP, weakness, resistance, retreat cost, stage, evolves-from or
rarity. Those seven fields are the entire gap, and nothing in the local install
supplies them. Everything else is already available locally: set, collector
number, Pokémon type, supertype, Tool/Special-Energy flags and foil treatment
come from bundle names, and the donated `AttributeDB` holds the **real**
archetype and ability GUIDs, attack names, per-type energy costs, damage and
full English text for 5,578 attributable archetypes.

Deliberate decision: the cards are **art-only**, with no name claimed, because
no local source joins a collector number to a name. Guessing from a type
block's alphabetical order would be right often enough to be trusted and wrong
often enough to poison search.

**SWSH11, SWSH12 and Crown Zenith** are absent from every file this project
has access to — not a bundle, not a set record, not even a bundle name. They
existed in the real PTCGO; they are simply not in the caches available here.

---

## Recently fixed

- **Playing a Trainer froze the game.** The offer's only valid drop target was
  the player's own discard pile, so a Trainer could only be played by dragging
  it exactly there; anywhere else resolved nothing and the card snapped back.
  No real client session had ever sent a `PlayTrainer`. Hidden because the
  headless harness picks its target *out of* `validTargets` and so always aimed
  at the discard.
- **Evolutions sat beside the Basic** instead of on top of it — the evolution
  was moved to the area rather than onto the card below. Nesting them fixed the
  stacking and made attaches onto evolved Pokémon select the right motion.
- Prizes for rule-box Pokémon (609 cards were scored as one prize).
- The loose-art texture cache was unbounded; it is capped and ref-counted now.

---

## Verifying

```sh
python -m unittest discover -s tests      # 292 tests
python match_client.py --games 20         # whole games over the real socket
python build_decks.py                     # proves decks by playing them out
python tools/deck_report.py               # which cards in a deck actually work
python tools/motion_table.py --check      # audits animation stacks
python tools/foil_coverage.py             # foil resolution per set
```

A passing test is not automatically a meaningful one. `match_client.py` once
read field names the offer does not contain, answered null to every offer, and
reported 65 consecutive clean games while exercising nothing. Ask what a
passing test would have to *see* in order to pass.
