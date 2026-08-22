# PTCGO Local Server

A local reimplementation of the server side of **Pokémon Trading Card Game Online**
(client v2.95.0.5815), built for offline/private use after TPCI shut the service down
on 2023-06-05.

Everything here was recovered from the client binaries already on this machine. No
official server code, assets, or credentials are involved, and nothing talks to any
Pokémon/TPCI host.

**Client:** the official download page is gone, but an archived copy from 2023-03-26
(before the shutdown) is at
<https://web.archive.org/web/20230326080907/https://www.pokemon.com/us/pokemon-tcg/play-online/download/>.
This server targets client **v2.95.0.5815**; other versions may differ in protocol
details or hardcoded ports.

---

## Status

The client boots, logs in, loads to the main menu and deck builder, and renders UI text,
cosmetics and card art. The collection holds 4 of every card.

| Stage | State |
| --- | --- |
| Patcher / updater | Bypassed (`ShouldPatch=false`) |
| Asset preload | Working |
| Gateway handshake | Working |
| Session + login | Working (DeviceID and sha1/digest) |
| Post-login data load | Working — 38 message handlers |
| Main menu / deck builder | Reached, stable |
| Localization | Working — 27,550 strings served |
| Card database | Working — 9,940 cards, all 62 sets |
| Collection | Working — 4 of every card |
| Asset bundles | Working — 233 bundles, 18,857 asset names indexed |
| Cosmetics (boxes, sleeves, coins, packs, logos) | Working |
| Card art | Working. 4,957 assets; every card the client requests |
| Backgrounds | Working via loose-art patch (one file) |
| Foil / holo | Authentic for XY12 only; masks elsewhere absent |
| Avatar items | **Not recoverable** — definitions never shipped |
| Matches / gameplay | Not implemented |

### What is genuinely gone

Card art, foil masks and menu backgrounds were streamed from a CDN that no longer
exists, and `bundleCache/` was never populated on this install. Only **5 of 62 sets**
ship art locally (XY12 plus the four energy sets).

The [loose-art patch](patch/) works around this: the client will display ordinary PNGs,
so art can come from any source. It is a sourcing problem now, not a technical one.

**Avatar items are the exception — they cannot be restored.** The wardrobe *art*
survives (4,653 assets across 18 bundles), but only 2 of the 9,940 archetypes reference
avatar art and both are pack products. The item definitions lived server-side. An empty
avatar collection is the correct rendering of the data that exists.

---

## Running it

```
start-server.cmd
```

Then launch the game. The server must be running *before* the client logs in.
Requires Python 3 (tested on 3.12); `Pillow` is needed for the art tools.

Logging is verbose by design: every frame in and out, plus `no handler for '<Name>'`
for anything unimplemented. That log is the main tool for finding what to build next.

`test_client.py` replays the gateway → session → login handshake without the game.

---

## Layout

| File | Purpose |
| --- | --- |
| `server.py` | Gateway (**39389**) and game server (**39390**), both TLS |
| `asset_server.py` | CDN stand-in on **8081** (plain HTTP): manifest, config, bundles |
| `bundle_index.py` | Extracts asset names from the shipped `.unity3d` bundles |
| `patch/` | Loose-art patch — makes the client load PNGs ([details](patch/README.md)) |
| `tools/fetch_all_art.py` | Bulk art fetcher, resumable |
| `tools/fix_missing_art.py` | Fills variant printings and Trainer Kit reprints |
| `tools/fetch_art.py` | Single-card fetcher, kept for one-offs |
| `tools/find_cache.ps1` | Scans all drives for a donor `bundleCache` |
| `test_client.py` | Handshake replay for testing without the game |
| `build_cache.py` | **Dead code** — targets a class this build never constructs |

Generated, not committed: `carddata/`, `bundle_index.json`, `certs/`, `accounts.json`.

---

## The protocol

Dire Wolf Digital's "WARG" protocol: TLS sockets carrying JSON, with some protobuf.

### Framing

```
[int32 BE length][uint32 BE requestID][uint32 BE flags][payload]
```

`length` counts requestID + flags + payload, so `payload = length - 8`.

Flags: `0x01` compressed (deflate, skip 2 bytes), `0x02` protobuf, `0x04` ping/pong,
`0x10` connection error, `0x20` ack request, `0x40` reconnect, `0x80` granted session.

### JSON messages

```json
{"name": "<ClassName>", "value": { ...fields... }}
```

`name` is the C# class name; fields use each class's `[JsonName]`. The client resolves
the name against types marked `[DwdJsonMessage]`.

### Protobuf messages

Wrapped in `dwd.Protobuf.ProtoMessage`: field 1 = full .NET type name, field 2 = a tag
number, field `<tag>` = the body. `ProtoMessage` declares only fields 1 and 2, so the
body arrives as a protobuf-net *extension* read back via `Extensible.TryGetValue`. The
tag is the writer's choice; this server uses 100.

### Login flow

```
gateway :39389   C-> RequestConnectionServiceWithVersion {clientVersion}
                 S-> ConnectionService {connectionEndPoint "host:port"}

game    :39390   C-> RequestSession {connectionInfo{...}}
                 S-> GrantedSession {version, serverTime, options, session}
                 C-> RequestLogin
                 S-> RequestedAuthType {validAuthTypes}
                 C-> StartAuthentication {authType}
                 ... auth exchange ...
                 S-> AuthenticationSuccessful {account, sessionID}
```

- **DeviceID** — what the client actually uses. `RequestDeviceID` → `AuthenticateDeviceID {deviceID}`.
- **sha1** — the username/password form. `RequestedUsername` → `RequestSaltForUser` →
  `DigestSalt {salt}` → `AuthenticateDigest {username, digest}`, where
  `digest = sha1_hex(password + ":" + salt)`.

Unknown usernames are auto-created; the first password used is pinned.

### Card data

Archetypes are fetched **per set**, and the keys drive everything:

```
GetArchetypeListKeys      -> ArchetypeKeys {keys: [62 set names]}
GetProtobufArchetypesList -> ArchetypesFound {archetypes, checksum, key}   (x62)
```

The client waits for exactly `keys.Length + 1` responses (the +1 is the avatar list).
An empty `keys` array means no cards at all.

### TLS

`CertificateValidator` is built with `allowSelfSigned: true, allowExpired: true`, so a
self-signed cert works — but hostname must still match, so `certs/server.crt` carries
SANs for `127.0.0.1`, `localhost` and `tcgo-gateway.direwolfdigital.com`, and is
self-signed with `subject == issuer` (the chain check requires it).

---

## Client configuration

An override `cake.cfg` in the client's `persistentDataPath`:

```
%USERPROFILE%\AppData\LocalLow\The Pokémon Company International\Pokemon Trading Card Game Online\cake.cfg
```

The client reads that before the shipped config, so the install stays clean. It differs
from stock in four ways:

```
hostname=127.0.0.1                  # was tcgo-gateway.direwolfdigital.com
versionURL=http://127.0.0.1:8081/   # was https://pie-live-dist.s3.amazonaws.com/
assetURL=http://127.0.0.1:8081/     # was https://dfsqwbwcu8r1a.cloudfront.net/
ShouldPatch=false                   # added; skips the dead updater
```

Without `ShouldPatch=false` the client tries the dead patch CDN and aborts before login.
The gateway **port 39389 is hardcoded in the client**, not read from config.

**To revert:** delete that `cake.cfg`. To remove the loose-art patch, restore
`pie-bundles.dll.orig` over `pie-bundles.dll` in `Managed/`.

`StreamingAssets\tcgo-gateway.direwolfdigital.com\` was **copied** (not moved) to
`StreamingAssets\127.0.0.1\`, because some client paths derive from the hostname. If
`hostname` changes again, mirror it again.

---

## Getting card art

The client only loads card art from Unity asset bundles, and the originals are gone. The
[loose-art patch](patch/) removes that constraint: drop a PNG in
`<game>_Data/LooseArt/` named after the asset request and it is used directly.

| Request | File |
| --- | --- |
| `Background/Background` | `Background_Background.png` |
| `BW10/008` | `BW10_008.png` |
| `BW10_wp_ph/008` | `BW10_wp_ph_008.png` (foil mask) |

**Every card already has art.** `tools/fetch_all_art.py` downloaded 4,532 of them
in 40 minutes. That is 4,532 files rather than 9,940 because art is keyed by set and
number, so reprints and reverse holos share one picture.

```
python tools/fetch_all_art.py            # everything outstanding
python tools/fetch_all_art.py BW1 XY6    # named sets only
python tools/fetch_all_art.py --retry    # another go at past failures
python tools/fetch_all_art.py --dry-run  # plan only
```

It is built to be left alone for hours: art already on disk is skipped, every outcome is
recorded in `tools/art_state.json`, and an interrupted run resumes exactly where it
stopped. Writes are atomic, so a kill cannot leave a half-written PNG behind.

Sources are `api.pokemontcg.io` for names and images — its hi-res files are **734×1024**,
exactly the height the client's textures want, so nothing is upscaled — with the
limitless CDN as fallback. Set metadata is cached in `tools/setcache/`, so a re-run costs
no API calls at all.

Unresolved assets are still logged as `[LooseArt] miss: <request>`, so if anything is
ever missing the client tells you exactly what it wants; `tools/fetch_art.py --from-log`
fetches just those.

### Asset names are not always the card number

One card number can have several printings, and an archetype may carry **attribute
10020**, an asset-name override. The client then asks for `XY4/065xy` rather than
`XY4/065`. These are the same illustration under a different foil treatment — Charizard
in Evolutions has three, differing only in foil pattern (AngledPillars / Galaxy /
Rainbow) — so the base card's art is the correct art for them.

This is easy to miss twice over: keying art on the card number makes those archetypes
look like duplicates, so they get skipped, and the resulting hole only shows up on the
handful of cards that have variants. `tools/fix_missing_art.py` resolves them, along with
the Trainer Kit sets (TK5A–TK10B), whose cards are reprints that no public database
carries — they are matched by name against art already on disk, preferring a printing
from the same era.

Images are laid out to match the client's own textures: **1024×1024, card scaled to full
height at its native aspect, centred, white padding**. Do not stretch to fill — see
Known issues.

Every download is verified by comparing the card's name in `carddata/` against the
source, so a wrong set mapping is skipped rather than silently saved as the wrong art.
Sets whose art already ships locally are skipped entirely, since LooseArt takes priority
over bundles and would otherwise replace authentic art with a third-party scan.

Name comparison is the fiddly part. PTCGO writes `PokeBall`, `NidoranFemale`,
`CharizardEX` and `BattleCompressor`; public data writes `Poké Ball`, `Nidoran ♀`,
`Charizard-EX` and `Battle Compressor Team Flare Gear`. The check folds accents, spells
out gender symbols, and accepts one name being a prefix or suffix of the other — but
**not** when the only difference is a rank marker (`ex`, `gx`, `break`, …), because
Charizard and Charizard-EX are genuinely different pictures. Two Blend Energies are
listed as explicit aliases rather than loosening the rule further.

---

## Known issues

**Foil masks are missing outside XY12.** The `_wp_std` / `_wp_ph` / `_wp_pcd` assets are
foil **masks**, not alternate printings. Real ones are 512×512 DXT5 with ~40% coverage —
hand-authored per card, and CDN-hosted. With none bound the shader samples stale
reflection state and smears a sheen across the card, so `fetch_art.py` writes neutral
(transparent) masks alongside each card to suppress that. XY12 has 108 authentic masks
locally and shows the real effect.

**Card texture geometry is easy to get wrong.** The card does *not* fill the square. The
client's own art spans x=110..912 (~0.78 aspect) over full height with white padding, and
the display quad crops to that column. Three wrong attempts before it was measured:
full-bleed (edges cropped), stretched into the column (~9% too wide, since the source is
0.719 and the column 0.78), then aspect-preserving and centred, which is correct.

**20 ambiguous asset names remain**, all avatar clothing items present in two avatar
bundles. Either resolution works; no tiebreak is applied.

**Set data is thin.** `SetDataList` is built from archetype filenames, so names are right
but `count`, `legalFormats` and `block` are empty.

**Trainer Challenge scenarios are fabricated** — three roots exist purely to satisfy
`determineLeagueAvailability()`, which indexes `League.Gold`/`Platinum`/`CityChampionship`
unguarded and throws otherwise, killing the connection.

**Downloaded art is a third-party scan**, not the client's own texture. It renders at
the right geometry and reads correctly, but it is not byte-identical to what shipped.
Original bundles would still be an upgrade — see To do.

**What is still without art** (244 of 9,135 asset requests):

| Kind | Count | Why |
| --- | --- | --- |
| Product art — booster packs, theme decks, elite trainer boxes, league bundles | 240 | Product photography, CDN-hosted, with no card behind it to look up |
| Trainer-Kit-exclusive cards (Pikachu Libre) | 2 | Printed only in a Trainer Kit; no public database carries those sets |
| UI logos (`Logos/globalNavLogo`, `Logos/logo_outline`) | 2 | Client artwork, not card data |

None are cards you can play with; they are storefront and chrome.

---

## To do

### Original bundles (still worth having)

Card art is done, but the originals would still be an upgrade: authentic scans at the
client's own resolution, plus the real per-card foil masks, with no patching involved.
Sources would be an archived CDN mirror, a backup, or an install with a populated
`bundleCache/`. `tools/find_cache.ps1` scans every drive and reports whether a cache
holds real set art or only the cosmetics every install has. Drop files into
`StreamingAssets\en_US\`, rerun `bundle_index.py`, bump `MANIFEST_VERSION`. A web
search found no public mirror.

### Foil masks

Only synthesisable, not recoverable — real masks are per-card artwork, so generated ones
would be plausible but invented. Compare against XY12 before deciding if that is worth it.

### Deck saving

`GetDeckList` / `OnlineDecksFound` return empty and nothing persists.

### Gameplay

Much larger than everything above combined: turn structure, rules engine, per-card
effects. `sausage-core.dll` and the `dwd.core.match` namespaces are the starting point.

---

## Reverse-engineering notes

**Decompiling.** `ilspycmd` with `DOTNET_ROLL_FORWARD=LatestMajor` (it targets .NET 6,
not installed). Decompile to a **single flat file** — obfuscated namespaces `b` and `B`
collide on a case-insensitive filesystem and silently overwrite each other.

**Obfuscated strings.** Encrypted in a `<PrivateImplementationDetails>` class with ~1200
static decryptor methods. Don't reverse the cipher — load the assemblies by reflection
and invoke the decryptors. Yields ~7,557 strings and makes the protocol readable.

**Obfuscated field names.** Many fields share a name and differ only by type. Aligning
decompiler declaration order to reflection order **does not work** — it mismatched on 38
of 191 fields. Read the IL instead: `GetILAsByteArray()`, take each `ldsfld` operand
(opcode `0x7E`), resolve with `Module.ResolveField(token)`.

**Reading the archetype blobs.** `StreamingAssets\<hostname>\<SET>` files are
BinaryFormatter-serialized `ArchetypesFound`. .NET Framework refuses them (obfuscated
fields differ only in case → CLS check). Use an `ISerializable` shim plus a
`SerializationBinder` mapping every `dwd.*` type to it — `ISerializable` bypasses the
member-binding check. Bind `List<dwd.Protobuf.X>` to `List<Shim>`; let enums resolve
normally.

**Reading .unity3d bundles.** `UnityWeb` container: header, then an LZMA-alone stream.
Inside, `m_Container` entries are little-endian length-prefixed ASCII padded to 4 bytes.
Textures are DXT1/DXT5 — wrap the mip-0 bytes in a synthetic DDS header and Pillow will
decode them, which is how the card geometry was measured.

**Localization.** The prebuilt `LocalizationDB-UTF16.db` is stamped `user_version=3` while
this build wants 4, so `PieDB.Init` wipes it every launch — permanently stale. Strings
must come from the server; read the prebuilt DB directly and serve its rows as one
release.
