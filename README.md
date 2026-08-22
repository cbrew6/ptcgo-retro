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

**Working:** the client boots, logs in, loads fully, and reaches the main menu and deck
builder. UI text renders. The collection holds 4 of every card — 9,940 archetypes
across all 62 sets. The session stays connected with no disconnects.

**Not working:** card art for 57 of 62 sets, and menu backgrounds — both were streamed
from the CDN and never cached locally (see [Card art](#card-art)). Gameplay is not
implemented.

| Stage | State |
| --- | --- |
| Patcher / updater | Bypassed (`ShouldPatch=false`) |
| Asset preload | Working |
| Gateway handshake | Working |
| Session + login | Working (DeviceID and sha1/digest) |
| Post-login data load | Working — 37 message handlers |
| Main menu | Reached, stable |
| Localization (labels) | Working — 27,550 strings served |
| Card database | Working — 9,940 cards, all 62 sets |
| Collection | Working — 4 of every card |
| Asset bundles | Working — 233 bundles, 6,529 assets indexed |
| Cosmetics (boxes, sleeves, coins, avatars) | Working |
| Card art | Partial — energy sets + XY12 only; rest was CDN-hosted |
| Backgrounds | Absent — CDN-hosted, no bundle ships locally |
| Trainer Challenge | Stub scenarios only |
| Matches / gameplay | Not implemented |

---

## Running it

```
start-server.cmd
```

Then launch the game normally. The server must be running *before* the client tries to
log in. Requires Python 3 (tested on 3.12).

Logging is verbose by design: every frame in and out is printed, and any message
without a handler logs `no handler for '<Name>'`. That log is the primary tool for
finding what to implement next.

`test_client.py` replays the full gateway → session → login handshake without launching
the game — useful for checking the server in isolation after a change.

---

## Layout

| File | Purpose |
| --- | --- |
| `server.py` | Gateway (**39389**) and game server (**39390**), both TLS |
| `asset_server.py` | CDN stand-in on **8081** (plain HTTP) |
| `test_client.py` | Handshake replay for testing without the game |
| `certs/` | Self-signed cert + key |
| `accounts.json` | Account store; accounts are auto-created on first login |
| `start-server.cmd` | Launcher |

---

## The protocol

Dire Wolf Digital's "WARG" protocol. TLS sockets carrying JSON, with a few protobuf
messages mixed in.

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

`name` is the C# class name; fields use the class's `[JsonName]` values. The client
resolves the name against types marked `[DwdJsonMessage]`.

### Protobuf messages

Wrapped in `dwd.Protobuf.ProtoMessage`:

```
field 1 (string)  messageName  - full .NET type name in ProtobufMessages.dll
field 2 (varint)  messageTag   - field number the body is stored under
field <tag>       the serialized message
```

`ProtoMessage` declares only fields 1 and 2, so the body arrives as a protobuf-net
*extension* and is read back with `Extensible.TryGetValue` at that tag. The tag is
chosen by the writer; any non-declared field number works (this server uses 100).

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

Two auth types are supported:

- **DeviceID** — what the client actually uses. `RequestDeviceID` → `AuthenticateDeviceID {deviceID}`.
- **sha1** — the username/password form. `RequestedUsername` → `RequestSaltForUser {username}`
  → `DigestSalt {salt}` → `AuthenticateDigest {username, digest}` where
  `digest = sha1_hex(password + ":" + salt)`.

Unknown usernames are auto-created and the first password used is pinned.

### TLS

`CertificateValidator` is constructed with `allowSelfSigned: true, allowExpired: true`,
so a self-signed cert is accepted — but the hostname must still match, so
`certs/server.crt` carries SANs for `127.0.0.1`, `localhost`, and
`tcgo-gateway.direwolfdigital.com`. The cert is self-signed with `subject == issuer`,
which the validator's chain check requires.

---

## Client configuration

An override `cake.cfg` lives in the client's `persistentDataPath`:

```
%USERPROFILE%\AppData\LocalLow\The Pokémon Company International\Pokemon Trading Card Game Online\cake.cfg
```

The client reads that location *before* the shipped config, so the original install is
untouched. Contents differ from stock in three ways:

```
hostname=127.0.0.1                  # was tcgo-gateway.direwolfdigital.com
versionURL=http://127.0.0.1:8081/   # was https://pie-live-dist.s3.amazonaws.com/
assetURL=http://127.0.0.1:8081/     # was https://dfsqwbwcu8r1a.cloudfront.net/
ShouldPatch=false                   # added; skips the dead updater
```

`ShouldPatch=false` matters: without it the client tries to download a patch manifest
from the dead CDN and aborts before login.

The gateway **port (39389) is hardcoded in the client**, not read from config.

### To revert

Delete that `cake.cfg` and the client goes back to stock behaviour (and back to being
unable to connect).

### Other change on disk

`StreamingAssets\tcgo-gateway.direwolfdigital.com\` was **copied** (not moved) to
`StreamingAssets\127.0.0.1\` because some client paths are derived from the configured
hostname. The original directory is intact. If `hostname` ever changes, mirror it again
or the shipped card databases won't be found under the new name.

---

## Known issues

<a id="card-art"></a>
**Card art is mostly unrecoverable from this machine.** Only **5 of 62 sets** have art
on disk — XY12 and the four energy sets. The rest was served from the CDN on demand and
cached in `bundleCache/`, which on this install was never populated. The CDN is dead, so
those images are simply absent; this is missing data, not a wiring bug. What does ship in
`StreamingAssets\en_US\` is mostly cosmetics: avatars, sleeves, coins, deck boxes, packs,
logos, set icons.

Card art loads **only** through `AssetBundle.LoadFromFile`. There is no loose-image path
for cards, so PNGs from an external source can't be dropped in without either repackaging
them into Unity 5.2.4f1 `UnityWeb` bundles (version-locked) or patching the client. See
the To do section for the most promising route.

**`getLocales` throws `NullReferenceException`** whenever the client asks for a bundle
that doesn't exist (e.g. `BW1`). Same root cause as above; it stops once the bundle is
present.

**Set data is thin.** `SetDataList` is built from the archetype filenames on disk, so the
names are right but `count`, `legalFormats`, and `block` are all empty/zero.

**Set data is thin.** `SetDataList` is built from the archetype filenames on disk, so the
names are right but `count`, `legalFormats`, and `block` are all empty/zero.

**Trainer Challenge scenarios are fabricated.** Three root scenarios exist purely to
satisfy `determineLeagueAvailability()`, which indexes `League.Gold` / `Platinum` /
`CityChampionship` without a containment check and throws `KeyNotFoundException`
otherwise — killing the connection. They carry attribute key `201420` (the int the client
casts to `O.g.League`) set to 1, 2, 3. They are not real campaign content.

**Everything post-login returns empty.** Wallet, decks, collection, notifications,
tournaments, lots, banned cards — all valid but empty. Correct for a fresh account,
but it means nothing is populated.

---

## To do

### Next up — card art

The blocker is format, not identification: every card's set code and number are already
in `carddata/`, so no lookup table is needed. But the client only loads art from Unity
asset bundles.

In order of preference:

1. **Find the original `.unity3d` bundles** — an archived CDN mirror, a backup, or an
   install with a populated `bundleCache/`. No patching, no repackaging, restores every
   set. `asset_server.py` already serves bundles at
   `/bundles/pc/{locale}/{locale}_{name}_{version}.unity3d`, so dropping files into
   `StreamingAssets\en_US\` is all that's required. A web search found no public mirror.
2. **Patch the client to load loose images.** `pie-src` `N/V.cs` already contains
   `GetURLImage(string url, callback)` — a URL→Texture loader with its own cache, used
   today for landing-page banners. Routing the card art path through it removes the
   bundle dependency entirely and makes any image source work. This is the most
   promising route if no bundles turn up.
3. **Build Unity 5.2.4f1 bundles** from external images. Needs that exact Editor
   version; heavy and fragile for ~10,000 cards.

Extracting the `assets[]` lists (asset name → bundle) would need the LZMA'd `UnityWeb`
containers decompressed and their asset tables read. Only worth doing for route 1.

### Then — deck saving

`GetDeckList` / `OnlineDecksFound` return empty and nothing is persisted. Decks built in
the deck builder are lost on restart.

### Later — gameplay

Substantially bigger than everything above combined. Needs the match engine: turn
structure, the rules engine, and per-card effects. `sausage-core.dll` and the
`dwd.core.match` namespaces are the place to start.

### Housekeeping

- Persist accounts properly (decks, collection) instead of returning empty every session
- Handle the compressed frame flag (`0x01`) — not yet exercised, but the client can send it
- Handle `MessageBundleMessage` (batched messages) on the read path
- Move the hardcoded ports/paths in `server.py` into a small config

---

## Reverse-engineering notes

Useful techniques, recorded because they'll be needed again:

**Decompiling.** `ilspycmd` with `DOTNET_ROLL_FORWARD=LatestMajor` (it targets .NET 6,
which isn't installed here). Decompile to a *single flat file* — the obfuscated
namespaces `b` and `B` collide on a case-insensitive filesystem and silently overwrite
each other in per-file output.

**Obfuscated strings.** String constants are encrypted in a `<PrivateImplementationDetails>`
class with ~1200 static decryptor methods. Don't reverse the cipher — load the assemblies
via reflection and invoke the decryptors. That yielded 7,557 strings across
`core`/`pie-core`/`pie-src`/`sausage-core` and made the whole protocol readable.

**Obfuscated field names.** Many fields share a name (`A`, `a`, `B`) and differ only by
type, so decompiler names can't be mapped back by name. Aligning decompiler declaration
order to reflection order **does not work** — it mismatched on 38 of 191 fields and
produced a wrong answer. Instead, read the method's IL via
`MethodBase.GetMethodBody().GetILAsByteArray()`, pull the operand of each `ldsfld`
(opcode `0x7E`), and resolve it with `Module.ResolveField(token)`. That gives the exact
field and its runtime value.

**Finding what to implement next.** Run the client and read the server log for
`no handler for '<Name>'`. Then find the response type: search for classes marked
`[DwdJsonMessage]`, and check whether the client blocks on it (a
`while (x == null) yield return null;` loop means it gates the loading bar).

**Watch out:** a `dwd.Protobuf.*` type existing does *not* mean the message is protobuf.
`CollectionCountFound` has one, but nothing registers it as a `ProtobufCounterpart`, so
sending it as protobuf made `ProtobufProcessor.Convert()` return the raw protobuf object
and `WargSocket.Read()` threw `InvalidCastException` — killing the read thread and the
session. Confirm a `[ProtobufCounterpart(typeof(...))]` registration exists before
choosing protobuf.

**Client-side crash reports.** The client sends its own exceptions to the server as
`LogClientError`, with a full stack in `debugInfo.Stack`. This is the fastest way to
diagnose client-side failures. `%USERPROFILE%\AppData\LocalLow\The Pokémon Company
International\Pokemon Trading Card Game Online\output_log.txt` has the same information
plus anything thrown before the session exists.
