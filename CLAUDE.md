# CLAUDE.md

Working notes for this repo. Read this before changing anything — most of it
is knowledge that cost real time to recover and is not obvious from the code.

## What this is

A local server for the **Pokémon Trading Card Game Online** client
(v2.95.0.5815), which lost its servers on 2023-06-05. Everything was
reverse-engineered from the client binaries on this machine. Goal: play
offline. Currently reaches the main menu and deck builder with a full card
collection; no gameplay yet.

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
- `build_cache.py` — writes an on-disk archetype cache. **Currently dead
  code**: it targets `WargArchetypesSource`, which nothing in this build
  constructs. Kept only as a reference for the file format.

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

Only **5 of 62 sets** ship art locally (XY12 + the four energy sets). The rest,
along with foil masks and menu backgrounds, was CDN-hosted; `bundleCache/` was
never populated and the CDN is dead.

**This is solved mechanically** by the loose-art patch in `patch/`: the client
now displays ordinary PNGs from `<game>_Data/LooseArt/`, named after the asset
request with `/` replaced by `_`. See `patch/README.md`. It is a sourcing
problem now, not a technical one.

All 4,532 cards now have art, fetched by `tools/fetch_all_art.py` in 40 minutes
from api.pokemontcg.io. It is resumable through `tools/art_state.json` and safe
to interrupt.

### Texture geometry — the card is stretched

A PTCGO card texture is 1024x1024 with the card occupying exactly **803x1024**:
centred, full height, ~110px of white padding each side. The card is therefore
*stretched horizontally* against its true 63:88 paper ratio, and the client
undoes that when it maps the texture onto the card quad. Verified across bw1,
xy1, xy12, sm8, hgss1, rsp and tk6a - it holds for every era.

The original fetch composited at 734x1024, the true paper ratio, on the
reasonable-looking assumption that matching the texture *height* was enough.
It is not: every fetched card rendered ~9% too narrow. Anything that composites
a card face must target 803x1024, not the real card aspect.

`tools/fix_card_geometry.py` repairs an existing file in place (crop the known
centred span, LANCZOS to 803x1024, repaste at x=110). It is **not idempotent**,
so it classifies by requiring the content to actually reach both expected edges
and refuses anything else. That guard earned itself immediately: the SM_Energy
composites are 724 wide (cols 150..873), not 734, and a looser check would have
baked a white sliver into all nine.

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
assumed. The card does NOT fill the square: the game's own art spans
x=110..912 (~0.78 aspect) over full 1024 height with white padding, and the
quad crops to that column. Getting this wrong looks like "stretched wide".
Correct authoring is: 1024x1024 canvas, card scaled to full height at its
NATIVE aspect, centred, white padding. Do not stretch to fill, and do not
stretch to 0.78 either - the source is ~0.719 and stretching it is ~9% too
wide.

**Foil masks.** `_wp_std` / `_wp_ph` / `_wp_pcd` are foil MASKS, not alternate
printings (`wp_ph` = reverse holo). Real ones are 512x512 DXT5, ~40% coverage,
hand-authored per card. With none bound the shader samples stale reflection
state and smears a sheen across the card, so `fetch_art.py` writes neutral
transparent masks alongside each card. XY12 has 108 authentic masks locally.

**Avatar items cannot be restored.** 4,653 avatar art assets exist across 18
bundles, but only 2 of the 9,940 archetypes reference avatar art and both are
pack products. The item definitions were server-side. An empty avatar
collection is the correct rendering of the data that exists.

`asset_server.py` serves `.unity3d` over HTTP at
`/bundles/pc/{locale}/{locale}_{name}_{version}.unity3d`, so any recovered
bundles dropped into `StreamingAssets\en_US\` work with no code change - rerun
`bundle_index.py` and bump `MANIFEST_VERSION`.

## Status / next

Working: login (DeviceID + sha1), full load to main menu and deck builder,
27,550 localized strings, 62 sets, 9,940 cards, 4 of each in the collection,
233 asset bundles with 18,857 indexed asset names, cosmetics, backgrounds and
card art (all 4,532 cards, 3.7 GB).

Absent, not broken: card art/foil masks for 57 sets (source per card), and
avatar items (definitions never shipped).

Next: more card art as encountered, then deck saving/loading, then gameplay
(the match engine - much larger; `dwd.core.match` namespaces).
