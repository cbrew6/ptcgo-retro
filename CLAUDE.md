# CLAUDE.md

Working notes for this repo. Read this before changing anything — most of it
is knowledge that cost real time to recover and is not obvious from the code.

## What this is

A local server for the **Pokémon Trading Card Game Online** client
(v2.95.0.5815), which lost its servers on 2023-06-05. Everything was
reverse-engineered from the client binaries on this machine. Goal: play
offline.

Working: login, collection, deck building, card art, foils, the avatar
wardrobe, pack opening, and **matches** - a game starts, deals, offers legal
moves, and an AI opponent plays back. The rules live in `engine.py`; the client
holds none.

**The user does not intend to share this and does not want to conflict with
TPCI.** Nothing here should be published or redistributed.

## Environment

Windows. The client is a Unity 2018.4.11f1 / Mono game.

| What | Where |
| --- | --- |
| Repo | `%APPDATA%\Pokémon Trading Card Game Online\ptcgo-local\` |
| Client install | `%APPDATA%\Pokémon Trading Card Game Online\PokemonTradingCardGameOnline\` |
| Managed DLLs | `<install>\Pokemon Trading Card Game Online_Data\Managed\` |
| StreamingAssets | `<install>\Pokemon Trading Card Game Online_Data\StreamingAssets\` |
| persistentDataPath | `%USERPROFILE%\AppData\LocalLow\The Pokémon Company International\Pokemon Trading Card Game Online\` |
| Client log | `<persistentDataPath>\output_log.txt` |

Note the `é` in the path — it breaks PowerShell scripts written as UTF-8
without BOM. Glob around it: `Get-Item "$env:APPDATA\*Trading Card Game Online\..."`.

## Running

```
start-server.cmd          # or: python server.py
```

Server must be up before the client tries to log in. Then launch the client
normally. Everything is logged — inbound frames, outbound frames, and
`no handler for '<Name>'` for anything unimplemented.

`test_client.py` replays the handshake without the game.

**"Error loading preload data" always means the server is down.** The client
fetches its config over HTTP before anything else; with no config there is no
`AssetBundleManager` and preload throws. It is never an art or data problem.

### Restarting it correctly — this wasted an hour

A scheduled task `PTCGO Local Server` runs it hidden via
`run-server-hidden.vbs` (window style 0; S4U would need admin). **Stopping the
task is not enough:** it kills the `cmd.exe` wrapper and leaves the `python`
child holding 39389/39390/8081, so the next start silently fails to bind and
exits, and the *old build keeps serving*. Two rounds of fixes were tested
against code that was never running.

```powershell
Stop-ScheduledTask -TaskName "PTCGO Local Server"
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -like '*server.py*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force }
Start-ScheduledTask -TaskName "PTCGO Local Server"
```

Then confirm from the log that a *new* `gateway listening` line appeared.
Comparing its timestamp against the traffic it is serving is the check that
catches this.

Launching the server from a tool-session shell does not work either — it is
reaped when that shell is torn down.

## First-time setup (after a fresh clone)

`carddata/` and `certs/` are gitignored. To rebuild:

1. **Certs** — self-signed, SANs for `127.0.0.1`, `localhost`,
   `tcgo-gateway.direwolfdigital.com`, `subject == issuer`:
   ```
   openssl req -x509 -newkey rsa:2048 -nodes -keyout certs/server.key \
     -out certs/server.crt -days 3650 -config certs/san.cnf -sha256
   ```
2. **Card data** — export from the client's shipped archetype blobs. See
   "Reading the archetype blobs" below.
3. **Client config** — write an override `cake.cfg` into persistentDataPath
   (the client reads that before the shipped one, so the install stays clean):
   ```
   hostname=127.0.0.1
   versionURL=http://127.0.0.1:8081/
   assetURL=http://127.0.0.1:8081/
   ShouldPatch=false
   ```
   plus the `*AppSecret` lines copied from the shipped config. Also mirror
   `StreamingAssets\tcgo-gateway.direwolfdigital.com\` to
   `StreamingAssets\127.0.0.1\` (some paths derive from the hostname).

## Architecture

- `server.py` — gateway (39389, TLS), game server (39390, TLS). All message
  handlers are `GameSession.on_<MessageName>`; dispatch is by method name, so
  adding a handler is just adding a method.
- `asset_server.py` — CDN stand-in on 8081 (plain HTTP, not TLS — Unity's curl
  would reject a self-signed cert). Serves the bundle manifest, dynamic
  config, motd, and `.unity3d` bundles.
- `bundle_index.py` — extracts asset names from the shipped `.unity3d`
  bundles into `bundle_index.json`. Required: without it `assets[]` is empty
  and nothing renders.
- `patch/` — loose-art patch (Mono.Cecil IL injection). See `patch/README.md`.
- `tools/fetch_all_art.py` — bulk art fetcher; all 4,532 cards, resumable.
- `tools/fix_missing_art.py` — variant printings + Trainer Kit reprints.
- `tools/fetch_art.py` — single-card fetcher (`--from-log`), kept for one-offs.
- `tools/find_cache.ps1` — scans all drives for a donor `bundleCache`.
- `tools/bundle_textures.py` — decodes Texture2D out of the **UnityWeb**
  bundles the client shipped with. It cannot read the donated **UnityFS** ones;
  `bundle_index.py` can.
- `tools/unshadow_foils.py` — moves aside LooseArt masks that a real bundle can
  now serve. Request-driven: it enumerates what the client will ask for rather
  than guessing a namespace from a bundle name, which is what it got wrong
  twice. Nothing is deleted; files move to `_looseart_shadowed_foils/`.
- `tools/foil_coverage.py` — rebuilds every card's exact foil request from the
  decompiled client and resolves it against the real manifest. This is how to
  answer "are the foils working" without looking at cards one at a time.
- `build_cache.py` — writes an on-disk archetype cache. **Currently dead
  code**: it targets `WargArchetypesSource`, which nothing in this build
  constructs. Kept only as a reference for the file format. Note it is what
  created `persistentDataPath\archetypes\` - the *client* never writes that
  directory, so do not expect a donor cache to contain one.

Gameplay, added later and deliberately layered:

- `engine.py` — the rules. Pure Python, no sockets, no protocol, no client
  knowledge. `new_game`, `legal_actions`, `apply` returning `(state, changes)`.
  Every rules assumption is a named field on `Rules`.
- `match.py` — the only place the engine and the client meet. Engine card id →
  entity GUID, engine state → `SerializedGameState` tree, engine `Change` →
  mutation messages.
- `effects.py` — the card effects, keyed by archetype GUID and abilityID.
  Driven by the card's ENGLISH RULES TEXT (attribute 200310 for Trainers, the
  per-ability text in 200740 for attacks), resolved against the shipped
  localization DB — not by card name, so one pattern covers every printing and
  a reprint whose wording changed stays inert rather than inheriting the wrong
  behaviour. `rules_for(db)` builds the populated `Rules`; the registries on
  `Rules` are all empty by default so a stock engine is inert.
- `ai.py` — the opponent. `choose(state, player, rng)` returns one legal
  action, re-checks its own answer against `legal_actions`, and falls back to
  `Pass` rather than ever raising inside a live match.
- `match_client.py` — a headless client that plays a whole match over the real
  socket and asserts on what comes back. This is the fastest way to check any
  protocol change; see "Status / next".
- `build_decks.py` — generates decks with real evolution lines and only
  Trainers the engine can resolve, and proves each one by playing it out
  before writing. Never touches the user's own decks.
- `docs/client-protocol-notes.md` — 2,400 lines of verified client protocol:
  selection messages, the 61 named animation sequences, which effect classes
  are live and which are dead, end-of-game parameters. Claims are marked
  VERIFIED / INFERRED / UNKNOWN; trust that marking.
- `tests/` — `python -m unittest discover -s tests`, 246 tests. The engine is
  testable without the game, which is the entire point of the split.

## Protocol essentials

Frame: `[int32 BE length][uint32 BE requestID][uint32 BE flags][payload]`,
where `length` counts requestID + flags + payload (`payload = length - 8`).

Flags: `0x01` compressed (deflate, skip 2 leading bytes), `0x02` protobuf,
`0x04` ping/pong, `0x10` connection error, `0x20` ack, `0x40` reconnect.

JSON payload: `{"name": "<C# class name>", "value": {...}}`, fields named by
each class's `[JsonName]`. The client resolves `name` against classes marked
`[DwdJsonMessage]`.

Protobuf payload: wrapped in `dwd.Protobuf.ProtoMessage` —
field 1 = full .NET type name, field 2 = a tag number, field `<tag>` = the
body (arrives as a protobuf-net *extension*). Tag is writer's choice; we use
100.

The gateway port **39389 is hardcoded in the client**, not configurable.

## Traps — read these

**A `dwd.Protobuf.*` type existing does NOT mean the message is protobuf.**
`CollectionCountFound` has one, but nothing registers it as a
`ProtobufCounterpart`, so sending it as protobuf made
`ProtobufProcessor.Convert()` return the raw protobuf object and
`WargSocket.Read()` threw `InvalidCastException` — which killed the read
thread and dropped the session with no error message. Always confirm a
`[ProtobufCounterpart(typeof(...))]` registration exists first.

**Arrays must be present and non-null.** The client iterates them unguarded.
Empty is fine; null is a `NullReferenceException`.

**Blocking loops gate the loading bar.** Several commands do
`while (model == null) yield return null;`. If you see the bar stick at a
percentage, find the message whose reply populates that model
(`GetDynamicVersions` and `GetGuidOverride` were two).

**`sausage-core` is largely dead code in this build.** `WargArchetypesSource`
is never constructed. The live archetype path is in `pie-src` and is per-set.
Verify a class is actually instantiated before building against it — this cost
an entire wrong implementation.

**Don't invent localization releases.** Returning a dummy release to dodge
error `2700200` writes garbage into the client's local DB. Serve the real
strings (see below) instead.

**Empty arrays can silently disable whole subsystems.** Every art request is
gated behind `DoesAssetExistInManifest(assetName)`, which reads an asset-name
map built solely from each bundle descriptor's `assets[]`. Shipping that empty
meant the client never requested a single bundle - everything rendered black,
with no error and no failed request. "Nothing happened at all" is the signature
of a gate like this, not of a broken loader.

**Alias collisions overwrite silently.** The client builds its lookup as
`assetPaths[name] = descriptor`, so the last writer wins. Registering every
underscore-prefix as an alias let foil bundles claim `XY12/011` alongside the
art bundle, and a request for the card face returned a foil mask. Bundles with
a `wp_<mask>` segment must never claim the bare set prefix. This was only
visible on XY12 - the one set with both art and foil locally.

**The client reports its own crashes** as `LogClientError`, with a full stack
in `debugInfo.Stack`. This is the fastest debugging tool available — the
server log usually explains a client-side failure before `output_log.txt`
does.

## Reverse-engineering techniques

**Decompiling.** `ilspycmd` with `DOTNET_ROLL_FORWARD=LatestMajor` (it targets
.NET 6, which isn't installed). Decompile to a **single flat file** —
obfuscated namespaces `b` and `B` collide on a case-insensitive filesystem and
silently overwrite each other in per-file output.

**Obfuscated strings.** Encrypted in a `<PrivateImplementationDetails>` class
with ~1200 static decryptor methods. Don't reverse the cipher — load the
assemblies by reflection and invoke the decryptors. Yields ~7,557 strings and
makes the protocol readable. (`dumpstr.ps1` pattern.)

**Resolving an obfuscated field to its attribute id.** `P.F` is a static class
of `AttributeDefinition` fields whose names are reused dozens of times with
different types, so no call site can be read off the decompiled text. Walk the
`.cctor` IL for `ldc.i4 <id>; newobj AttributeDefinition; stsfld <field>` and
join any other method's `ldsfld` operands on the metadata token. That is exact.
Reflection cannot load the assemblies from the game directory - the path
contains "é" and the loader mangles it - so copy them somewhere ASCII first.

**Obfuscated field names.** Many fields share a name (`A`, `a`, `B`) and
differ only by type. Aligning decompiler declaration order to reflection order
**does not work** — it mismatched on 38 of 191 fields and produced a wrong
answer. Instead read the method's IL:
`MethodBase.GetMethodBody().GetILAsByteArray()`, take the operand of each
`ldsfld` (opcode `0x7E`), resolve with `Module.ResolveField(token)`. Exact,
and gives the live value too.

**Reading the archetype blobs.** `StreamingAssets\<hostname>\<SET>` files are
BinaryFormatter-serialized `dwd.Protobuf.Collection.ArchetypesFound`. .NET
Framework's BinaryFormatter refuses them (obfuscated fields differ only in
case → CLS check). Mono doesn't care, but we're not on Mono. Workaround: an
`ISerializable` shim class plus a `SerializationBinder` that maps every
`dwd.*` type to the shim — `ISerializable` bypasses the member-binding check.
Bind `List<dwd.Protobuf.X>` to `List<Shim>` and let enums (`+Type`) resolve
normally. That produced all 9,940 cards.

**Reading .unity3d bundles.** `UnityWeb` container: header (signature, format,
unity version/revision, then minimumStreamedBytes + headerSize + level sizes as
big-endian int32), then at `headerSize` an LZMA-alone stream. Inside, the
`m_Container` entries are little-endian length-prefixed ASCII padded to 4
bytes - that is where asset names live. Textures are DXT1/DXT5: wrap the mip-0
bytes in a synthetic DDS header and Pillow decodes them, which is how the card
geometry was measured. `bundle_index.py` does the name extraction.

**Measuring texture layout.** Detect the card edge from the YELLOW border, not
from non-white columns. The padding around the card is white, so a
black-padding assumption reports the whole square as artwork and sends you
down the wrong path.

**Localization.** The prebuilt `LocalizationDB-UTF16.db` in StreamingAssets is
stamped `user_version=3` but this build's config wants 4, so `PieDB.Init`
wipes it on every launch — it is permanently stale. Strings therefore have to
come from the server: read the prebuilt DB directly and serve its ~27,550
rows as one release.

## Card data

`carddata/*.json`, one file per set, `{set, checksum, archetypes:[{lo, hi,
attrs:[{n, v}]}]}` where `lo/hi` are the protobuf UUID halves and `v` is a
`dwd.Protobuf.Object`.

`uuid_to_guid_str()` mirrors `ProtobufExtensions.ToGuid` bit-for-bit. It has
to: `CollectionCount.archetypeID` is a GUID string and must match what the
client derives from the protobuf UUID.

Archetype IDs must be unique across sets — the client does
`dictionary.Add(archetypeID, ...)` and throws on duplicates, aborting the
whole load. `load_cards()` de-dupes defensively.

Useful attribute keys: `200580` set code, `200630` name, `200550` rarity,
`200490` HP, `200540` stage, `10140` localized-name key, `201420` league order
(scenarios).

## The installer is only a baseline

Card data through SM4, localization through **SM5**, four set icons. Everything
else came over the wire on first login and was cached under

    %USERPROFILE%\AppData\LocalLow\The Pokémon Company International\
        Pokemon Trading Card Game Online\
        archetypes\          one file per card, FULL definitions (attack
                             costs, damage, gameText, abilityID - all JSON)
        bundleCache\         card faces, foil masks, set symbols, products
        LocalizationDB-*.db  names and attack text
        AttributeDB.db       attribute definitions

Baseline here: 9,942 archetypes, 51 bundles. A live player has far more.

Do NOT conclude that later sets are unaddable because the client is old. The
assemblies are January 2023 - the final build - and carry RareHoloVMAX,
RareHoloVSTAR, VSTARDamageColor, RareRadiant, Foil_Radiant, FUSION_STRIKE and
Tag Team abilities. SWSH mechanics are implemented; only data and art are
missing, and card behaviour is data-driven JSON. I asserted the opposite from
the localization table alone, which only showed what the BASELINE knows, not
what the code supports. Check the binaries before ruling something out.

Scarlet & Violet genuinely never existed here: PTCGO's last content was Crown
Zenith, January 2023, matching the build date.

### The installer adds nothing — settled

`PokemonInstaller.msi` (v2.95.0, MSI stream dated 2023-01-12) **is** the client
already installed, not a newer one. All 589 unique payload files hash-match a
file already on disk; 0 are absent. Its card-data seed under
`StreamingAssets\tcgo-gateway.direwolfdigital.com\` holds exactly the same 62
sets - set-for-set identical, nothing extra - and it ships no `AttributeDB.db`
and no `archetypes\`. Do not re-investigate this hoping for SM5+; the answer is
no, and it is a hash comparison rather than an opinion.

Two things worth keeping from that dig:

- **Localization runs one set ahead of the archetypes.** The shipped
  `LocalizationDB-UTF16.db` carries **446 rules-text entries for `sm5`** -
  more than any other set (sm2 390, sm1 351, sm4 311): attack titles, gametext,
  abilities. So if SM5 archetypes are ever synthesized, the English card text
  is already local and needs no reconstruction. The rip also supplies all 173
  SM5 card faces. The missing piece is only the archetype records.
- **`keys.bin` is load-bearing.** It is a set->MD5 map the client validates the
  seed files against, so any fabricated set payload dropped in that directory
  would need a matching entry. The live patcher (`Refresher\`) shipped content
  as binary **vcdiff deltas** against these seed files, not as whole files.

## Card art

Only **5 of 62 sets** shipped art locally (XY12 + the four energy sets). The
rest, along with foil masks and menu backgrounds, was CDN-hosted, and the CDN is
dead. A donated cache later supplied 1,585 more bundles - see "Holo WAS
recovered" below - but the sourcing problem is the same shape.

**This is solved mechanically** by the loose-art patch in `patch/`: the client
now displays ordinary PNGs from `<game>_Data/LooseArt/`, named after the asset
request with `/` replaced by `_`. See `patch/README.md`. It is a sourcing
problem now, not a technical one.

LooseArt is an **override layer, checked before the bundles**. That is what
makes it work, and also why a stale placeholder there silently beats real
bundle art - the failure mode that hid every foil twice.

All 4,532 cards now have art, fetched by `tools/fetch_all_art.py` in 40 minutes
from api.pokemontcg.io. It is resumable through `tools/art_state.json` and safe
to interrupt.

### Texture geometry — the 803 box is card PLUS bleed

Settled by decoding the shipped DXT1 textures with `tools/bundle_textures.py`,
not by inference. Two numbers, and conflating them costs you every card:

    card    x=145..877, 733x1024  -> 733/1024 = 0.71582 vs 63:88 = 0.71591
    box     x=110..912, 803x1024  -> card, plus a 35px horizontal BLEED of the
                                     card's own edge column on each side

**The card is never stretched.** It sits at true paper aspect and the game
fills the outer band by smearing the edge pixel outward. On Free_Energy the
band is black instead of bled; everywhere else it tracks the edge row by row.
Proof that settles it: the round type symbol in the energy bundles measures
**50x50, a perfect circle**, and the same eye glyph is 1.3929 w/h in the BW and
XY bundles against 1.3913 on true-aspect paper art.

I got this wrong once, expensively. Measuring the *outer* extent of a rip
texture gives 803, which invites the conclusion that the card is 803 wide and
that art composited at the true 734 is "9% too narrow". It is not - it is
missing its bleed. Stretching ~5,000 cards to fill the box made every one of
them 9.4% too wide, and the tell was energies: a horizontal stretch turns their
round symbol into an obvious ellipse where card art hides it.

So: to author a card face, place it at its NATIVE aspect with the card box at
x=145, then bleed the edge column out to 110..912. Never resample to 803.
`tools/fetch_art.py` had this right in a comment all along and was overridden.

`tools/bundle_textures.py` parses these bundles generically off their embedded
type trees (`list` / `dump` / `span`). One trap: for serialized version < 16 an
object's `typeID` **is** the class ID, not an index into the type table.

Three things that bit during that run, all worth remembering:

- **The logger killed the run.** A card name containing a gender symbol went
  through `print()` on a cp1252 console, raised `UnicodeEncodeError` from
  inside the loop, and ended a multi-hour job 1,200 cards in. Per-card
  try/except did not help because the logging sat outside it. Wrapping the
  work is not enough - anything that reports on the work has to be
  unkillable too.
- **A whole set can be lost to one transient error.** Three sets were skipped
  to HTTP 500s that succeeded on the next attempt. The set-metadata call is
  the single point where one failure costs 100+ cards, so it gets its own
  larger retry budget and a deferred second pass.
- **Name matching is where correctness actually lives.** PTCGO writes
  `PokeBall`, `NidoranFemale`, `BattleCompressor`; upstream writes `Poké
  Ball`, `Nidoran ♀`, `Battle Compressor Team Flare Gear`. Fold accents,
  spell out gender symbols, allow prefix/suffix - but never let a rank
  marker (`ex`, `gx`, `break`) be the only difference, or Charizard gets
  Charizard-EX's art.

**Attribute 10020 is an asset-name override.** An archetype carrying it makes
the client request `XY4/065xy` instead of `XY4/065`. These are variant
printings: same illustration, different foil treatment (Charizard in
Evolutions has three - AngledPillars / Galaxy / Rainbow). Two consequences,
both of which bit:

- Keying art on the card number makes these archetypes look like duplicates,
  so they get silently dropped and the card renders blank.
- The real asset name is `attr 10020 or "%03d" % number`, never just the
  number. 335 of 9,135 asset requests use an override.

**And when 10020 contains a "/" it is ABSOLUTE, not relative to the set.**
`packs/BW1BlackWhite` names the shared `packs` bundle, so the file is
`packs_BW1BlackWhite.png` - not `BW1_packs_BW1BlackWhite.png`. Gluing the set
key on the front produced 60 pack images sitting under names nothing ever
requests, and "pack art is missing" was the symptom. Deck boxes are the same
story under `pcdBoxes`. `bundle_index.json` is the authority on which
namespaces exist, since the client gates every request on it.

Note `pcdBoxes` (angled product render) and `deckboxFlats` (the flat UV wrap
pasted onto the 3D box model) share leaf names but are different pictures - do
not write one where the other belongs.

The client's miss log is the fastest way to find this class of bug: it prints
the exact string it wanted, and `XY4/065xy` explains itself immediately where
staring at card data does not.

`tools/fix_missing_art.py` handles overrides and the Trainer Kits (TK5A-TK10B,
reprints that no public database carries as sets - resolved by name against art
already on disk, preferring the same era, since a reprint usually keeps its
original illustration).

**Never fill missing art by matching card names.** Tried, shipped, wrong:
TK10B Alolan Raichu got Crimson Invasion's Alolan Raichu, different attack,
caught in play. Same name in two sets = different cards, and carddata has no
attribute that separates them (`10190` looks like a card id but is per-set:
all 20 TK10B archetypes share it). Blank beats wrong - a substituted face
misstates the card during a game.

Also worth carrying: verifying that a copy succeeded is not verifying that
the source was right. Checking `TK10B_017 == SM4_031` byte for byte proved
the file copied, which was never in doubt, and said nothing about whether
SM4 was the correct card. Pick the check that can actually fail.

The one safe substitution is same set + same card number (variant printings).
That was settled empirically rather than assumed: the authentic XY12 bundles
ship `011` and `011xy` together, and extracting both textures (UnityWeb ->
LZMA-alone -> DXT1 via a synthetic DDS header, then flip vertically) shows the
same card with a set-logo stamp in the art box.

`tools/fetch_art.py --from-log` reads the client's own miss log and fetches
only the cards actually encountered. It name-checks every download against
`carddata/` and skips sets whose art already ships locally (LooseArt takes
priority over bundles, so fetching those would replace authentic art).

**Card texture geometry** - measured from the shipped XY12 DXT1 textures, not
assumed. The card does NOT fill the square: the quad crops to x=110..912
(~0.78) over full 1024 height. Correct authoring is a 1024x1024 canvas with the
card at its NATIVE ~0.719 aspect, full height, centred. Do not stretch to fill,
and do not stretch to 0.78 either - stretching a 0.719 source into the 0.78
column renders every card ~9% too wide.

The one thing this paragraph originally missed is what fills the rest of the
column: not padding but a **bleed** of the card's edge pixel. See "the 803 box
is card PLUS bleed" below - and note that the missing bleed is exactly what
made 803 look like the card width and prompted a wrong 5,000-file stretch.

**Foil masks.** `_wp_std` / `_wp_ph` / `_wp_pcd` are foil MASKS, not alternate
printings (`wp_ph` = reverse holo). Real ones are 512x512 DXT5, 12-26% alpha
coverage, hand-authored per card. With none bound the shader samples stale
reflection state and smears a sheen across the card, so `fetch_art.py` writes
neutral transparent masks alongside each card. XY12 has 108 authentic masks
locally.

Two request namespaces, easily confused: `<SET>_wp_<kind>/<num>` is the stamp
layer, `<SET>_wp_<kind>_Foil<N>/<num>` is the foil mask. Bundle assets are keyed
by bare collector number ("107").

**BW foils are in, from a second donated cache.** The note below was written
when BW1-BW11 had no masks at all and only "another donor from the right era"
could close it. That donor arrived: `tools/import_bundle_cache.py` took 869
bundles out of their `bundleCache` and BW went from 94.4% flat to 0.4%.
Foil coverage overall is 99.9% - 5,315 of 5,322 - with seven BW cards left.

**Holo WAS recovered, from a donated cache.** An earlier note here said it
could never extend beyond XY12. That was right about what was reproducible and
wrong about what was recoverable - a friend's `LocalLow` cache from an account
that actually played supplied 444 foil bundle payloads. 45 of the 62 sets now
have real masks; **BW1-BW11 still have none** (that donor played the SM/SWSH
era). Only another donor from the right era can close that.

What remains true: a mask traces the card's artwork silhouette, so it is
derived from the original layered art and **cannot be synthesised** from a flat
card scan. Do not generate them; a wrong mask is worse than none, which is what
the neutral masks exist to prevent. Note also that most cards never had a holo
printing at all - broad `wp_ph` coverage with sparse `wp_std` is correct, not a
gap.

**LooseArt shadows bundles, and that failure is silent.** LooseArt is checked
*before* the bundle system, so a neutral placeholder beats a real mask and the
card renders flat with nothing logged. `tools/unshadow_foils.py` moves aside
exactly the placeholders a bundle can serve, and leaves the rest - a set with
no donated mask still needs its blank.

Deriving the namespace is where this went wrong twice. Bundle names end in a
content-release token of **at least three shapes** - `_CR105`, `_CRR65p1`,
`_CRSM4`, and a bare set code `_SM3` - so matching the *release* is guesswork.
Match the namespace instead: everything up to and including
`_wp_<kind>[_Foil<n>]`. Two rounds of "fixed" foils were really one set being
freed and another silently left behind.

**Verify coverage against what the client actually requests**, i.e. the
`[LooseArt] <ns>/<num>` lines in `output_log.txt` - not against a namespace we
derived. A coverage table built on the broken rule reported 100% while nothing
rendered, and that number was used to explain away a real bug twice.

**Avatar items: the definitions are gone, but they are reconstructible.**
4,653 avatar art assets exist across 18 bundles, and only 2 of the 9,940
archetypes reference avatar art - both pack products. The item definitions were
server-side, so they are genuinely absent.

What was wrong here was the conclusion, not the observation. An empty wardrobe
is *not* "the correct rendering of the data": wardrobe items arrive exclusively
through `AllAvatarArchetypesFound`, and `on_GetProtobufAllAvatarArchetypesList`
was answering with an empty body, so the client built an empty list and then
never requested a single avatar asset. That is why nothing shows and why
`output_log.txt` contains no avatar requests at all.

The inputs to rebuild the catalog all survive client-side: `LocalizationDB`
holds ~1,395 item names, the bundles enumerate 1,719 `_thumb` sprites and 2,278
body stems, and the wardrobe slot is derivable from the asset suffix
(`_hair`, `_hat`, `_jacketll`, ...), gender from the leading `f`/`m`.

Attributes the client needs per avatar archetype - **`200215` is load-bearing**:
`CreateAvatarRenderers` does an unconditional `.get_Value().Value` on it, so
omitting it throws at startup.

    200215  int              male/female pairing key - MUST be present
    200930  LocalizableText  sprite base; request is avatar_thumbs/<v>_thumb
    200890  enum Group       Eyes=0 ... Hair=6, Hat=7, Jacket=8, Trousers=9
    10540   enum ProductType must be "AvatarItems" to take the avatar branch
    10220   enum Gender      Female=0, Male=1

Enums travel as **strings**, not ints. `AllAvatarArchetypesFound` has the same
field layout as `AllArchetypesFound`, so `pb_archetype` encodes it unchanged -
only the type name differs.

`asset_server.py` serves `.unity3d` over HTTP at
`/bundles/pc/{locale}/{locale}_{name}_{version}.unity3d`, so any recovered
bundles dropped into `StreamingAssets\en_US\` work with no code change - rerun
`bundle_index.py` and bump `MANIFEST_VERSION`.

## Gameplay

The client is a renderer and an input device. It holds **no rules at all** - it
cannot know that you may attach one Energy per turn, so the server must send a
menu of legal moves and the client only draws it. That is why a rules engine
was unavoidable.

### Match flow

```
RequestQueueMatch  -> MatchFound
   (client drives VersusScreen -> Playmat unaided; no server part)
PlayerReady        -> GoFirstChoiceRequired
GameCustomChoice   -> SerializedGameState, the deal, ActivePlayerSet
                   -> SelectionWithTargetsRequired   (choose Active + Bench)
SelectionWithTargets
                   -> SelectionWithTargetsAndActionsRequired
SelectionWithTargetsAndActions  (null selection = pass = end turn)
   ... an effect that asks something interleaves
                   -> SelectionWithTargetsRequired / CustomChoiceRequired
ResignGame         -> GameEnded + GameCompletedMessage
```

`SerializedGameState` carries the whole board as one recursive entity tree and
may only be sent **once** - a second throws. Send it with every card still in
its owner's deck, then animate the deal, or the game appears rather than
starts.

### Wrap game messages, do not send them bare

A bare `GameMessage` is processed **twice**: once as a command in
`SessionProvider.Update`, and again when the `Sequences` consumer replays it
off the queue. For most messages the replay is merely wrong (`ActivePlayerSet`
double-counted the turn). For `SerializedGameState` the replay **throws**, the
exception escapes `ConsumeQueuedMessages`, and Unity kills that coroutine
permanently - after which nothing drains the queue and every later message is
silently ignored. That one bug presented as "concede does nothing", "the deal
never animates" and "the turn counter starts at 2".

Wrap in a `SequenceMessage` with an **all-zero** `sequenceID`: legal outside a
sequence (the parser's mismatch check short-circuits on `Guid.Empty`), executes
exactly once, in order, no Start/Stop pair needed.

**`GameCompletedMessage` is the one exception and must stay bare** -
`MessageCommands` only unwraps `EffectPlayed`, and the `Sequences` loop tests
for it before `SequenceMessage`.

### Sequences

Only needed for the named animations (`DealInitialHands`,
`IntroduceInitialPokemon`, `DealInitialPrizeCards`, `GroupedMove`, ...). Named
sequences run their nested `GroupedMove`s **in parallel** with a stagger, which
is what makes a hand fan out instead of trickling one card at a time.

Mis-bracketing throws out of the same never-restarted coroutine, so always emit
through one helper that closes what it opens. Nested messages use the same
`{"name":..., "value":...}` envelope and **`name` must be the first key**.

Derive the deal from the **final state**, never by replaying the engine's
change log - that log contains every mulligan redraw, which renders as cards
flying out of the deck and back into it.

### The LooseArt cache must be ref-counted - do not remove Track()

`AssetBundleImageCache` is an LRU capped at **60**. `AddTexture()` evicts
before inserting, and eviction calls `AssetRefCounter.RemoveReference`, which
**throws** for anything it is not counting.

The loose-art helper writes into that dictionary by reflection. The first
version never registered what it inserted, so every LooseArt card face was an
untracked landmine: after roughly sixty cards had been viewed the cache stayed
full, and the next `AddTexture` - which is exactly how a real foil mask arrives
from a bundle - hit one during eviction and threw. The throw escapes the
loading coroutine and Unity kills it where it stood:

| coroutine | dies before | symptom |
| --- | --- | --- |
| `CardImageRenderer.updateCardImage` | `setFoilMask()` | card keeps its face, loses its foil |
| `AssetBundleTexture.loadAssetRoutine` | `set_mainTexture()` | deck box / sleeve / coin blank |
| `AssetBundleMaterial` | same | 3D deck box wrap blank |

One session's log carried 203. This is why foils "worked for the first few
cards and then stopped" and read as an era-specific gap: whether a card got its
foil depended only on how many cards had been looked at that session. It is
also why the deck box and sleeves were missing - their assets were in the
manifest and served correctly the whole time. See `patch/README.md`.

### The attributes that make a card render

| id | type | without it |
| --- | --- | --- |
| 10000 | ArchetypeID | no card identity |
| 10140 | LocalizableText | `HandSort.Compare` throws; the hand empties every frame |
| 200570 | `PokemonTypes[]` | **`getDefaultPerCardType` does `EnergyType.Value` with no `HasValue` check.** The throw unwinds `RefreshRequestData` before `getImageRequestString`, so the card never requests its texture and renders blank - for the whole match |
| 200340 | `SpecialConditions[]` | no status markers; send the WHOLE list, it is not a delta |
| 201040 | `{"options": [[type]]}` | wrong placeholder colour. Note carddata stores this as a JSON *string*; the wire wants the object |
| 10020 | string | variant printings ("017a") render the plain art. A value containing "/" is an absolute product path, not a card face |
| 200620 / 200610 | FoilMasks / FoilEffects | **the card renders FLAT.** `IsFoil` is computed from these two and nothing else, and only a card that says it is foil ever requests a mask. 5,331 of 9,940 archetypes are foil |

Collection and deck views build cards from the local archetype DB, which has
every attribute. Only entities the server synthesises can be missing one, which
is why art broke in matches alone.

### The opening is three steps, and skipping one breaks the next

```
CoinFlipChoiceRequired   call heads or tails   <- raises BOTH coins
InitialCoinFlip          the flip itself
GoFirstChoiceRequired    only to whoever WON the flip
```

The call is not a formality. Nothing else raises the coins at the start of a
game - the `CoinFlipChoice` command does it (`InitialUp`,
`YouPickHeadsOrTails`) - so a flip sent without it animates a coin that is
still lying down, which reads as "there was no coin flip". `DealInitialHands`
then lowers both coins again, so the flip has to come before the deal.

`MultipleCoinFlipWithContextEffect` needs a `source` the client already knows:
its command constructor does `All.get_Item(source)` with no guard. That forces
the board to be sent first, so the match is built with a provisional first
player and the real answer written back before setup begins.

`resultLst[0] == 0` is heads.

### Mulligans are shown, not hidden

A mulligan is public: the hand with no Basic is revealed to the opponent before
it is shuffled back. `opening_animation` used to suppress the whole thing
because the raw redraws looked like cards flying in and out of the deck - that
hid a rule to fix a rendering problem.

`MulliganRevealCardsEffect` inside the `Mulligan` sequence is the right tool.
It carries the hands INLINE as attributes and introduces those entities itself,
so cards that are back in the deck need no prior `EntityIntroduced`, and the
sequence blocks the rest of the opening until the dialog closes.

The carousel pages with its own left/right buttons; the header is the 1-based
page index and the sub-header is `string.Format(prompt, piles.Count)`, so
**`prompt` must not be null** or that throws. `player` does not gate rendering
- the cards come from the inline attributes either way - it gates which cards
are un-introduced when the dialog closes, so a wrong account can strip cards
out of the local hand view.

`MulliganChoiceRequired` is a dead end - `IMulliganChoice` has exactly one
implementation and no caller anywhere in pie-src.

Compensation ("for each of your opponent's mulligans you MAY draw a card") is
an offer, not a debt: `PlayerState.owed_draws`, answered by `DrawMulligans`,
ranked ahead of setup because the drawn cards may be what gets placed.

### Never send an action offer while the Active is empty

`CheckShouldEndTurn` does `player1Active.get_Entity().Children.get_Item(0)`
with no guard, and an empty Active is precisely the state a promotion is asked
in. Use `KnockoutPokemonTargetInformation` on a `SelectionWithTargetsRequired`
instead - the client has a command registered for that kind.

### An action offer is never `forced: true`

`forced` makes the root's `MayCancel` false, which is the only way to get the
button captioned "End Turn" rather than "Done" - but it makes `MayAdvance`
false at the same time, so the button is drawn and does nothing. That is a soft
lock, not a stricter prompt. Anything that genuinely cannot be declined gets
its own selection message.

For an offer with `forced: false` and an EMPTY `targetMap` the button does
appear, captioned "Done", and clicking it sends `selection: null`. A row-less
offer is therefore not a dead end.

### Suppressing a banner

`"prompt": ""` draws no banner: `CanShowPrompt` needs `Prompt != null` AND a
non-empty `DisplayText`, and `L.LT("")` returns `""`. Cleaner than borrowing a
key out of `PiePromptListener.suppressedKeys`.

Send `CoinFlipChoiceRequired` BEFORE `GoFirstChoiceRequired`. If the go-first
prompt arrives while the coin animator is still in its `Start` state the
factory hands it to a different command whose constructor hard-sets an override
banner - and leaks `OverrideShowPrompt`, killing the action button for the rest
of the game.

### What may and may not hang off the Active

Attacks and **retreat**, both `"AbilitySelection"`, and nothing else. Clicking
the Active is how a player asks for its menu, so any other row there hijacks
that click - an end-turn row on an Active with no Energy attached meant
clicking it silently ended the turn instead of showing anything. Ending a turn
is the client's own button.

Retreat has exactly one home and this is it. `SelectableActionUtil.IsRetreat`
is `Description == "BaseRetreat"`, and every caller asks the ACTIVE. Retreat
used to hang off the bench Pokemon being switched to, which kept one
selectionType per entity and was completely invisible - there was no way to
retreat at all. It does not need to be a printed ability: `CreateButtons`
pulls it out by description BEFORE it looks for a `PieAbilityDescription`, so
it wants no entry in attribute 200740 and is drawn from its own prefab
(`retreatButtonPrefab`) using the Pokemon's retreat cost and Energy type.

During SETUP the Active carries no attacks, so a "done benching" row there is
safe, and that is where it goes.

### An unhandled Change kind is dropped in SILENCE

`animation_for` does `getattr(self, "_change_" + kind, None)` and `continue`s
when there is none. Nothing is logged. The engine applies the change, the
server's board is correct, every test passes, and the client is simply never
told. Two shipped that way:

- **promote** had no handler at all, so a knocked-out Active was never
  replaced on screen - the bench Pokemon just sat there. Reported as "the
  opponent doesn't promote"; it was both sides.
- **retreat** emitted moves for the discarded Energy but the SWAP itself is
  `CHANGE_RETREAT`, which had none - so a retreat paid its cost and nothing
  moved.

`tests/test_match.py:ChangeCoverageTests` now fails on any Change kind that is
neither animated nor in an explicit `NOT_ANIMATED` allow-list, so a new kind
forces the decision instead of vanishing.

### The client is 32-bit, and LooseArt was the memory hog

It dies around 3.4 GB of address space regardless of installed RAM. A
collection scroll loaded **583** distinct LooseArt textures and crashed on an
access violation inside a memcpy at a 3348 MB working set (the dump is under
`%LOCALAPPDATA%\Temp\<Company>\<Product>\Crashes`, and it keeps the
`output_log.txt` from the crashed run, which the live one has overwritten).

`LoadImage()` decodes to **RGBA32** - 4 MB for a 1024x1024 card face, plus a
second CPU-side copy because the texture stays readable. `Compress(false)`
then `Apply(false, true)` takes that to DXT1/DXT5 with no readable copy, an
8-16x reduction, and DXT is the format the real bundles ship so it is the
authentic quality rather than a downgrade.

Still outstanding: the patch writes `map[request] = tex` **directly**, which
bypasses `AddTexture()`'s eviction entirely, so LooseArt entries are not
subject to the LRU cap of 60 at all. Compression buys a lot of headroom but
the collection is still unbounded.

### The confirm button only exists on the LAST node of a chain

`buttonIsActive` needs `haveZeroSelectionRelatedInterrupts`, and for a chained
selection that comes from `flag6 = MayAdvance && NodeToAdvanceTo() == null`.
Only the final TargetInformation in a row has nothing to advance to, so only
it can own a confirm button. Retreat's cost tray was sent FIRST, with the
destination chained after it, and could therefore never be confirmed - "it
lets me select Energy but there is no confirm button". Destination first, tray
last. The client's own label logic agrees: it flips the tray's button from
Cancel to Done exactly when the tray is satisfied, which only means anything
if the player is sitting in the tray with the button live.

### Sleeve, coin and deck box travel in gameOptions

Not as attributes, not on any entity, not in the board. `N.d` reads them out of
`MatchFound.gameOptions` keyed by **account**:

    gameExtrasSleeve_<accountID>     gameExtrasDeckImage_<accountID>
    gameExtrasCoin_<accountID>       gameExtrasSecondaryDeckImage_<accountID>
    gameExtrasDeckBox_<accountID>    gameExtrasImageURL_<accountID>
    gameExtrasDeckID_<accountID>     gameExtrasDeckColor<N>_<accountID>
    avatarProfile_<accountID>        avatarProfile_name_<accountID>

`getSettingArchetype` drops any value whose length is not **exactly 36** - a
bare hyphenated GUID - so a quoted or braced one is silently no sleeve at all.
With the keys absent `getSleevePaths` returns `_default_sleeve`, which is what
every match used, because the client's own `clientOptions` are only
`{"Timers": "false"}` and we forwarded them unchanged. The values are already
on the deck as attributes 200680 / 200670 / 200690.

### A sequence runs its children WHILE it animates

So a sequence is a container for one beat, never for "everything that happened
next". Setup made this vivid: the placements were animated as
`IntroduceInitialPokemon`, and because `SetupDone` was applied in the same
`engine.apply` loop, the prize deal, the turn banner and the first draw were
all folded into that one sequence. The prizes laid themselves out on top of the
Pokemon still being placed, the banner fired over the top, and the board only
settled afterwards. Apply the placements, emit them as the sequence, then
apply `SetupDone` and emit ITS changes at top level.

The same rule killed the coin flip, and the fix has a trap on BOTH sides. An
empty `ActivePlayerSet` sequence does four things, before running any children:

    initialCoinFlipAnimator   DonePicking = true, Hidden = true
    promptListener            OverrideShowPrompt = false, OverrideText = null
    player1Coin / player2Coin InitialUp = false, up = false

Sent directly behind the flip it cuts the flip's own animation short and the
hand deals over the top. But `DealInitialHands` lowers only the four COIN
bools - it never hides the dialog and never clears the prompt override - so
removing the sequence instead left a full-screen banner over the setup screen
that swallowed every click behind it, and **nothing else in the client ever
clears `OverrideShowPrompt`**. It belongs AFTER the deal, which is the one
position that satisfies both.

`OpponentChoosingToGoFirst` is the "your opponent will go first" notice, and it
fires only when the sequence has **at least one child** and none of them is a
`b.O` (`ObserverCustomChoiceOfferMessage`, spectator-only, which we never send).
An empty one is silently nothing. The two deck `Shuffled` messages are its
body - sent bare they animated both decks, top left and bottom right, over a
coin that was still flipping.

### Retreat: the button, the tray, the destination

Three nodes in the order the player performs them, all on the Active:

```
"BaseRetreat" / "AbilitySelection"      the retreat button
  RetreatCostEntityListTargetInformation  the pip tray - pay the cost
    RetreatNewActiveTargetInformation     who comes in
```

`valueToSelect` on the tray is the cost in Energy **symbols**, not a card
count - `Tally()` sums `get_EnergyProvidedCount()` over what is selected, so a
Double Colorless pays two by itself. It must be `forced: true`: `get_satisfied`
lets an unforced tray be satisfied by selecting *nothing*, which retreats
without paying. The tray only renders when every selectable Energy is a child
of a Pokemon (`isPipTrayRetreatSelection`); ours always are, because Energy
attachment is structural.

Honour what the tray returns. The offer already carries a legal Retreat per
destination with a server-chosen payment, so ignoring the reply is still legal
and discards Energy the player did not pick. `_do_retreat` validates properly -
attached to the Active, covers the cost, does not over-discard - so a bad
selection is refused and re-offered.

Both prompt keys are the original server's own:
`com.direwolfdigital.cake.rules.actions.cake.retreat.prompt1` and
`playmat.prompt.selectenergytodiscard.<N>`.

### Do not chain InitialBenchedTargetInformation

The real setup screen chains it after `ActivePokemonTargetInformation`, and
finishing it needs the client's own Done button. When that button does not
appear the player is frozen: the bench lights up and there is no way forward.
Reported twice. Send the Active node ALONE - a chain with nothing after it
advances straight to the reply, so the Active always resolves on the drag - and
ask for the bench separately as ordinary clickable rows.

More generally: **an offer must never be a dead end.** If nothing is legal the
server ends the turn itself rather than sending a row-less offer.

### positionInParent

`-1` appends, which is right for a hand or a discard pile and wrong for the
Active: `SingleCardArea` lays its one card out by index, so an appended Active
sits off centre. Send `0` for single-card areas.

### Selection replies

Two different shapes, and they are not interchangeable:

```
SelectionWithTargetsAndActions   [[entityID, abilityID], [TargetResponse, ...]]
SelectionWithTargets             {entityID, targetResponses: [TargetResponse]}
```

`TargetResponse` is `{"entityList": [...], "name": "..."}`. The action reply's
second element is the TARGET the player picked - ignoring it makes one row
standing for six Actions apply an arbitrary one, which is how attaching an
Energy used to land on a Pokemon nobody chose.

`SelectionWithTargetsRequired`'s `targetMap` is a **dict** with **exactly one
key** - with `ignoreFirst` the root does `if (AvailableSelections.Count() != 1)
throw`, and those selections are the targetMap keys. The **second
TargetInformation becomes a CHILD of the first**, which is how "Active, then
Bench" is one offer answered in one message.

### Animation

- **`animDuration` does not control anything you want.** It is milliseconds,
  and its only use is delaying the game-log line. Card flight time comes from a
  `CurveMotion` prefab chosen by the source and destination zone.
- The animation vocabulary is the **61 named sequences** (`GroupedMove` is the
  fan-out, `Attack`, `Knockout`, `Draw`, `Mulligan`, ...). A message sent
  outside one gets no choreography. `match.animation_for()` keeps that
  structure; `messages_for()` flattens it.
- **`ParallelSequence` always throws.** Its backing list is declared and never
  assigned, so it raises a NullReferenceException out of the message pump and
  ends the game. There is no generic "run these at once" sequence. To show
  several cards together use a message that takes a LIST -
  `RevealCardsToAllEffect` for a reveal - not a sequence.
- **~40 effect classes have no consumer at all** and are silently dropped -
  `AnimationDelayEffect`, `BlinkEffect`, `PromptMessage`, `GameLogMessage`,
  `SelectionFinished`. Check `docs/client-protocol-notes.md` before sending one.
- `CakeAttackEffect` must be sent **before** the HP update: the client decides
  the knockout itself as `damageAmount >= defender.currentHP`, so afterwards a
  60 damage hit on a 100 HP Pokemon compares 60 against 40 and animates a
  knockout that did not happen.
- The "YOUR TURN" banner is a baked prefab driven by `ActivePlayerSet`. The
  server never sends its text.
- **`ActivePlayerSet` as a SEQUENCE and as a MESSAGE do different things.** The
  message (`L.Q`) sets the active player, bumps the turn counter, plays the
  banner and clears `model.c`; it does **not** touch the coins. The sequence
  (`l.Z`) lowers both coins and hides the coin dialog, and does that work
  BEFORE running its children - so an `ActivePlayerSet` sequence with **zero
  children** is a clean "put the coin away" primitive and nothing else. Only
  that sequence and `DealInitialHands` ever lower `InitialUp`.
- `PostActionPhaseEffect` lowers `up` but not `InitialUp`, so it cannot clear
  the opening coin. `ForceSelectionFinished` does not clear it either - it ends
  the offer first, which makes its own coin branches unreachable - and it hangs
  if it is the local player's turn with no offer open.
- **`PauseOnPromptEffect` with `doPause:false` never clears
  `OverrideShowPrompt`**, and that flag permanently disables the action button.
  Every one needs a matching `ClosePauseOnPromptEffect`.

### Localization keys

The DB is the client's own `LocalizationDB-UTF16.db`: 27,550 rows, **entirely
lowercase**, looked up case-insensitively. A key that is not in it is not an
error - `L.LT` returns the key itself, so the UI displays
`playmat.prompt.yourturn` as text. That exact string was on screen, from a key
this server invented.

carddata wraps its keys as `"$$$com.direwolfdigital...$$$"`; the wrapper is not
part of the key.

The DB also still carries the ORIGINAL SERVER's namespace,
`com.direwolfdigital.cake.rules.*` - direct evidence of what was really sent.
`tests/test_match.py` reads every key literal out of `match.py` and `server.py`
and fails if it is not in the DB.

`SelectableAction.description` is a **semantic tag, not display text**:
`CheckHintStrength` tests `Description.Contains("BaseRetreat")`.

### Entities the client will not tolerate

| requirement | what happens otherwise |
| --- | --- |
| card attribute **10140** (name key) | `HandSort`'s comparator NREs, `List.Sort` throws, and `EntityChildRenderer` has already cleared the layout - the hand empties every frame and cards park unparented at the world origin |
| bench attribute **201920** = 5 | `BenchLayout` divides by it; 0 gives Infinity/NaN transforms and endless collider warnings |
| playmat + `outOfPlay`/`activeStadium`/`activeTrainer` have **no owner** | `IntroduceEntity` routes by `owningPlayerID`; owned by a player they are never bound to their layouts and one dereferences an unassigned field |
| `children` never null | `Entities.initialize` reads `.Length` |
| exact zone names in 10140 | `k.P.introduce` throws on anything unrecognised |
| both players present in the initial state | `configurePlayerEntities` runs only there; `ActivePlayerSet` NREs otherwise |

"Face down" is not a flag - it is `attributes: null`. Revealing a card is an
`EntityIntroduced`. Energy attachment is **structural**: the energy entity
becomes a child of the Pokemon. Damage needs no message: HP carries current in
`value` and max in `originalValue`, and the client subtracts.

### Offers

`selectionType` decides the UI and is the most load-bearing string.
`"Ability"` auto-advances to target selection and never looks the action id up
on the card - right for moves that are not a printed ability. `"AbilitySelection"`
draws a button per ability and only if the action id really appears in that
card's attribute-200740 list - so attacks must carry their true `abilityID`.
One entity must never mix the two.

`MulliganChoiceRequired` must **never** be sent: its node kind is the empty
string, which has no UI and no fallback, so the game stalls forever. PTCGO's
mulligans were animation plus a summary, never a prompt.

### Deck legality

Checked entirely client-side before `RequestQueueMatch` is even sent. Two
things bite: the collection grants **4 of each card**, and ownership is
enforced - so 40 copies of a printed Basic Energy is an illegal deck.
**`Free_Energy` is exempt from the ownership check**; build decks from it and
cap real cards at 4.

Validation results are keyed by **format GUID**, and the client hard-codes the
six (`F.L`): Modified `6402e830-...`, ThemeDeck `1414fd67-...`, TrainerChallenge
`6a1dec5a-...136`, Unlimited `6a1dec5a-...135`, Expanded `98c83df9-...`, Legacy
`6b33d420-...`. Decks must also be served with attribute **10860** (the format
name list) or `ValidateDecksData` bails and nothing is legal.

## Status / next

Working: login, main menu, deck builder and deck save/load, 62 sets, 9,940
cards with 4 of each in the collection, card art for every card, pack opening,
the avatar wardrobe (1,333 reconstructed items), and **a complete game** -
the player chooses their own Active and Bench, plays Basics, evolves, attaches
Energy, retreats, uses Abilities, plays Trainers, attacks with real hit
effects and damage numbers, promotes after a knockout, takes prizes, and wins
or loses.

Assets: **2,484 bundles / 79,003 indexed asset names** after importing from
two donated caches. Foils resolve for every era, 99.9% of cards.

Verify any change with `python match_client.py` - a headless client that plays
a whole match over the real socket and asserts on what it receives. `--games N
--seed S --quiet` soaks; `--deck NAME` picks a deck. It fails the run if it
never actually played, because an earlier version read field names the offer
does not contain, answered null to everything, and reported every game clean.

Known gaps, in rough order of value:

- **Triggered abilities do not exist.** Everything is activated or continuous;
  nothing fires on an event, so Rocky Helmet, Exp. Share and every "when this
  Pokemon is Knocked Out" card is correctly unimplemented rather than
  half-working. This is the largest remaining rules gap.
- **Auras are not modelled** - `static_effects` sees only the Pokemon's own
  Abilities and Tools plus the Stadium, so a benched Pokemon buffing the
  Active does nothing.
- **Card coverage.** 486 of 1,120 Trainer printings (80 distinct names), 4,626
  of 12,204 attack printings, 35 activated and 28 continuous Abilities. A card
  whose text does not match a known pattern stays inert rather than guessing.
- **Choice shapes are a flat list.** Reordering (Pokedex), face-up prizes
  (Town Map) and Devolution Spray need shapes the renderer does not have.
- **Seven BW cards** still render flat out of 5,322 foils. Everything else
  resolves to a real mask.
- **SM5-SM12 and SWSH art** is imported but unusable: no card definitions
  exist for those sets, and the donated `AttributeDB` is a search index (GUID,
  name, abilities) rather than full attributes.
- The **AI is beginner strength** and will discard a good hand to a draw
  Supporter.
- **The client's own end-turn button is load-bearing and unverified from
  here.** It is what ends a turn early and what confirms a chained selection;
  the server can only guarantee the cases where nothing is legal. If it stops
  appearing, look at `buttonIsActive` in the playmat action-button component
  (`pie_d.cs:137899`) before changing message shapes.
- **A Pokemon with no legal move cannot be clicked.** Rows are only sent for
  legal actions, so an Active with too little Energy to attack or retreat has
  no node and clicking it does nothing - it "just sits there". The real client
  showed the card and greyed the buttons. Attacks cannot be offered unusably
  (`AbilityButtonRenderer.Render` has no affordability path), so this needs a
  different mechanism than the action offer.

## How this has gone wrong before

Patterns worth internalising, each of which cost a test cycle or several:

1. **The client dereferences unguarded, constantly.** A missing dictionary key
   or attribute throws rather than degrading, usually inside a coroutine, and
   the symptom surfaces somewhere unrelated. When something "does nothing",
   look for a throw, not for a missing feature.
2. **Localization misses are silent.** `LocalizationLookup` returns the key
   verbatim, so an invented key renders as raw text with nothing logged. Check
   every key against `LocalizationDB-UTF16.db` before sending it.
3. **Measure against what the client requests**, not against a name you
   derived. Two separate bugs survived rounds of "fixes" because a coverage
   check used the same wrong assumption as the code it was checking.
4. **Verify the fix is actually running.** Compare the server's startup
   timestamp against the traffic it served.
5. **A green check can be worse than a red one.** The match harness read
   field names the offer does not contain, found no actions, answered null to
   every offer, and reported 65 consecutive games clean while exercising
   nothing. Its board check ran against the pre-deal state, where every card is
   legitimately face down, so it asserted on zero cards. Both now fail loudly
   when they have nothing to check. Ask what a passing test would have to see
   to pass, not just whether it passes.
6. **Read the assemblies rather than inferring.** Nearly every real answer here
   came from IL; nearly every wrong turn came from a plausible guess. The
   obfuscator collapses distinct fields onto identical decompiled names, so
   resolve attribute ids from IL (`scratchpad/pfmap.json`) and not from source.
