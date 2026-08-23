# Client protocol notes

Reverse-engineering notes for the PTCGO client (v2.95.0.5815, Jan 2023), aimed
at someone implementing server-side gameplay against it. Complements
`CLAUDE.md`; where the two disagree, this file cites line numbers and CLAUDE.md
usually does not.

**Every claim is tagged.**

- **VERIFIED** — I read the code that consumes the field or message.
- **INFERRED** — consistent with the code, but the decompiler collapsed
  obfuscated field names (they are all called `A`/`a`/`B`) so the exact binding
  is a reading of declaration order, not proof.
- **UNKNOWN** — stated as unknown.

Citations are `pie_d.cs:NNNNN` / `core_d.cs:NNNNN` / `sausage.txt:NNNNN`, which
are **de-obfuscated** copies of the decompiled assemblies — see
"Reproducing the toolchain". Line numbers match the raw `pie.cs` / `core.cs`
exactly (the de-obfuscation is a within-line substitution), so
`pie.cs:141788 == pie_d.cs:141788`.

---

## 0. Reproducing the toolchain

The de-obfuscated sources are what make this readable. To rebuild them:

```bash
# 1. decompile, single flat file (namespaces `b` and `B` collide on NTFS)
cd "<install>/Pokemon Trading Card Game Online_Data/Managed"
DOTNET_ROLL_FORWARD=LatestMajor ~/.dotnet/tools/ilspycmd pie-src.dll     > pie.cs
DOTNET_ROLL_FORWARD=LatestMajor ~/.dotnet/tools/ilspycmd core.dll        > core.cs
DOTNET_ROLL_FORWARD=LatestMajor ~/.dotnet/tools/ilspycmd sausage-core.dll > sausage.cs

# 2. decrypt the string table  ->  "<decryptorName>\t<plaintext>" per line
powershell -File tools/dump_strings.ps1 "<Managed dir>" strings.tsv   # pie-src, 6211 rows
#   the same script pointed at core.dll gives corestrings.tsv, 1198 rows

# 3. substitute.  ILSpy renders the calls as a bare `.aH()` because of the
#    `using <PrivateImplementationDetails>{GUID};` alias at the top of the file,
#    so the decryptor method name survives verbatim and a regex is enough:
#      (?<![A-Za-z0-9_])\.([A-Za-z]{1,8})\(\)   ->   "<plaintext>"
#    ...only when the captured name is a key in the map. Preserve line numbers
#    (substitute within the line) so citations stay valid against the raw file.
```

That regex is safe in practice because the decryptor names come from a fixed
6211-entry set and real zero-argument methods with those exact names are rare.
Inspect anything that looks wrong.

Scratch copies used to write this document live under

    %LOCALAPPDATA%\Temp\claude\c--Users-cbrew-AppData-Roaming-Pok-mon-Trading-Card-Game-Online
        a55eba75-5ec4-43b5-a0ef-a7946c15ff7e\scratchpad\

and are session-local, so they will eventually be cleaned up - hence the recipe
above:

```
pie_d.cs         8.9 MB  de-obfuscated pie-src (283,937 lines)
core_d.cs        2.9 MB  de-obfuscated core    (109,407 lines)
strings.tsv      decryptor -> plaintext, pie-src  (6211 rows)
corestrings.tsv  decryptor -> plaintext, core     (1198 rows)
sausage.txt      decompiled sausage-core (still obfuscated strings)
pie-src.il       IL dump, for resolving collapsed field names
coredll.il       IL dump of core.dll
pfmap.json       "<il type>||<field>" -> attribute id, for P.F::* lookups
loc.db           copy of StreamingAssets/LocalizationDB-UTF8.db
```

**Obfuscated *field* names still cannot be trusted from source.** The
de-obfuscation only fixes string literals. Fields still all decompile to
`A`/`a`/`B`, so `msg.A` is ambiguous. Where it mattered I resolved it from IL
(`ldsfld` operand → `Module.ResolveField`); see the `P.F::A` example in §1.4.
`[JsonName(...)]` attributes are unaffected and are always trustworthy, and the
attribute order in the decompiled class **is** the real declaration order.

---

## 1. Protocol ground rules

These are the rules the rest of the document assumes. All VERIFIED.

### 1.1 Two different discriminator conventions — do not mix them

**(a) `[DwdJsonMessage]` / `[DwdJsonEffect]` classes use an ENVELOPE.**

`DwdModelAnalyzer.ConsumeValue` (`core_d.cs:31471`) peeks: if an object's
**first** property is `name` and its value matches a registered message class
name, it consumes `{"name": X, "value": {...}}` by popping exactly four tokens,
reading the body, then popping the object end (`ConsumeServerHintedValue`,
`core_d.cs:31507`).

- `name` **must be the first key**. This is why `server.py` builds nested dicts
  name-first.
- The value is the **simple class name** (`type.Name`), registered by
  `DwdModelAnalyzer.RegisterJSONMessage` (`core_d.cs:31400`).
- `DwdJsonEffectAttribute : DwdJsonMessageAttribute` (`core_d.cs:31319`), so
  effects live in the same name space and use the same envelope. An
  `EffectPlayed.effectMessage` value is therefore
  `{"name":"CakeAttackEffect","value":{...}}`.
- Duplicate simple names throw at startup (`RegisterJSONMessage`).

**(b) `[TypeHinting("name")]` classes use an INLINE field.**

`ConsumeObject` (`core_d.cs:31524`) scans the object for the hinting property
and switches to the named subclass. No envelope.

Types carrying `[TypeHinting("name")]` in this build:

| type | file:line |
|---|---|
| `TargetInformation` (and every `*TargetInformation`) | `core_d.cs:16618` |
| `TargetInfo` | `core_d.cs:16606` |
| `ClientAction` | `core_d.cs:36570` |
| `ClientEvent` | `core_d.cs:36408` |
| `RewardDefinition` | `core_d.cs:35798` |
| `DraftSubState`, `SerializableDraftSelectionInfo` | `core_d.cs:58915`, `58943` |

So a target info is written inline:

```json
{"name":"ActivePokemonTargetInformation","validTargets":["…"],"numberToSelect":1}
```

**Both failure modes throw**, inside the message pump, which is fatal (§1.5):

- hinting field missing → `DwdModelAnalysisException("Type indicates that it
  has a type hinting field, but we couldn't find it!")`
- unknown hint value → `DwdModelAnalysisException("Hinted Type {0} was not
  found!")`

`TargetResponse` is **not** type-hinted on the way in; the client writes a
`name` field on it on the way out purely as data (`core_d.cs:16712`).

### 1.2 Scalars

| type | JSON | source |
|---|---|---|
| `EntityID`, `AccountID`, `GameID`, `SequenceID`, `AbilityID`, `ArchetypeID`, `DeckID`, `PopupID`, `ArrowID` (all `TypedID`) | plain GUID string | `core_d.cs:90600`, tokenizer branch `TypedIDAnalyzer.IsTypedID` |
| any `enum` | **string** (the member name) | matches CLAUDE.md; e.g. `TargetPreference`, `SpecialConditions`, `AbilityType` |
| `Tuple<A,B>` | 2-element array `[a, b]` | `Tuple2Analyzer`, `core_d.cs:32073` |
| `LocalizableText` | plain string **or** `{"id": "…"}` | `LocalizableTextAnalyzer`, `core_d.cs:76469` |
| `LocalizableTextVariables` | plain string **or** `{"id": "…", "textVars": {…}}` | `LocalizableTextVariablesAnalyzer`, `core_d.cs:76075` |

**Trap:** `LocalizableTextVariables` **throws** on any property other than `id`
and `textVars` (`core_d.cs:76107`: *"Hit unknown property {0} while
deserializing LocalizableTextVariables"*), and throws again if `id` was absent
at object end. A bare string is always safest. `null` is safe (the analyzer
declines and the generic path coerces null).

`LocalizableText.ID` is `Trim('$')`-ed on construction (`core_d.cs:76410`) —
that is why the endgame parameter keys look like
`me_$playmat.endgame.stat.mvp.archetypeid$`.

`textVars` is `TextVariables` (`core_d.cs:76293`):
`{"numberMap": {tok:int}, "stringMap": {tok:locKey}, "varMap": {tok: string|string[]}}`.
Substitution is a plain `String.Replace` of the *token text* and `stringMap`
values are themselves localized. It is **not** `{0}`-style formatting — a
`{0}` in a loc string is filled by `L.LT(key, args)` from C#, never from
`textVars`.

### 1.3 Attributes

`BaseAttribute.Name` is an **int** (`core_d.cs:94604`). JSON shape
(`AttributeAnalyzer`, `core_d.cs:95288`):

```json
{"name": 200490, "value": 60, "originalValue": 60, "modValue": null}
```

`name` **must be first** — the analyzer resolves the `AttributeDefinition` from
it and uses that definition's `ValueType` to type `value` / `originalValue` /
`modValue`. Unknown attribute ids degrade gracefully (`MissingAttributes`); they
do not throw.

`ReadOnlyAttributes` accepts **either** a JSON object (keys ignored, values are
attribute objects) **or** a JSON array of attribute objects
(`ReadOnlyAttributesAnalyzer`, `core_d.cs:95980`).

`AttributeModified{entityID, attribute}` and
`AttributeRemoved{entityID, attributeName}` live in `sausage-core`
(`sausage.txt:3215`, `3229`); `EntityAdded{entityID, owningPlayerID,
parentEntityID}`, `EntityDestroyed{entityID}` and
`EntityIntroduced{attributeMap, entityID, entityName}` at `sausage.txt:1631`,
`1648`, `1659`.

### 1.4 Two inbound queues

`k.p.EnqueueMessage` (`pie_d.cs:133209`) splits game messages into an
**Instant** queue and a **Queued** queue. `messageIsInstantQueue`
(`pie_d.cs:133255`) routes to Instant **only** for `AttributeModified` whose
attribute name equals one specific id; everything else goes to Queued.

- That attribute is **201100**, whose value type is `Nullable<float>`.
  VERIFIED from IL - inside `k.p::messageIsInstantQueue`
  (`pie-src.il:518684`):

  ```
  IL_002b: ldsfld class AttributeDefinition`1<Nullable`1<float32>> P.F::A
  ```

  resolved through `pfmap.json`, whose key "Nullable`1<float32>||A" maps to
  201100. (`P.F` has many fields called `A`; only the IL operand disambiguates
  them - the decompiled source cannot.)
- `Sequences.ConsumeInstantMessages` (`pie_d.cs:147859`) assumes every Instant
  message is a `SequenceMessage` and casts unguarded
  (`((SequenceMessage)nextMessage).Msg`). So **`AttributeModified` for 201100
  must be wrapped in a `SequenceMessage` or it throws.**
- Everything else is drained by `ConsumeQueuedMessages` (`pie_d.cs:147808`) —
  the coroutine CLAUDE.md warns about.

### 1.5 Sequence dispatch, and what happens to unknown names

`SequenceCommands.GetCommand` (`core_d.cs:69749`) looks the sequence `name` up
in a table built from `[SequenceCommandConstructor("<name>")]` constructors. On
a miss it returns `null`, and `SequenceCommandFactory.GetCommand`
(`core_d.cs:32362`) falls back to `sequenceFactory.Generic(…)` → a plain
`SerialSequence`.

**So inventing a sequence name is safe**: it runs its children serially, in
order, with no animation grouping. (`WarnOnGenericSequence` logs them.)

`MessageCommands.getCommand` (`core_d.cs:69626`) is the same story for
messages: an unregistered message type produces `new Noop()`, logged only if
`loggingEnabled`. **An unknown message is silently ignored, not an error.**

`MessageCommands.GetCommand` (`core_d.cs:69613`) unwraps `EffectPlayed` before
dispatch and back-fills `effectMessage.gameID` from the wrapper — confirming
CLAUDE.md's "MessageCommands only unwraps EffectPlayed".

### 1.6 Sequence parameters are messages, not attributes

`StartSequence` carries `attributes` (`ReadOnlyAttributes`), but the named
sequence commands do **not** read them for their operands. `m.n`
(`pie_d.cs:144056`, the base class of every pie sequence command) builds two
dictionaries by scanning its own children:

```csharp
if (item is EntityIDDataEffectCommand c) dict [c.Key] = model[c.Value];
if (item is StringDataEffectCommand   s) dict2[s.Key] = s.Value;
```

Those commands come from `EntityIDDataEffect` / `StringDataEffect` /
`IntDataEffect` / `EntityIDListDataEffect` (`core_d.cs:70204`–`70219`),
`[DwdJsonEffect]` messages shaped `{"key": "…", "value": …}`
(`KeyValueDataEffect<T>`, `core_d.cs:70219`).

So to give a sequence a named operand, nest inside it:

```json
{"name":"EffectPlayed","value":{"gameID":"…","effectMessage":
  {"name":"EntityIDDataEffect","value":{"key":"From","value":"<guid>"}}}}
```

Keys actually read by this build (grep of `.get_Item("…")` in `pie_d.cs`):
`From`, `Into`, `Target`, `NewActive`, `Retreating`, `VUnion`,
`Subcard0`…`Subcard3`. `Evolve` reads `From`/`Into` (`pie_d.cs:141634`);
`Knockout` uses the literals `"From"`, `"To"`, `"ToKnockOut"` as *animation
curve* names, not as data keys (`pie_d.cs:147936`).

### 1.7 Zone (`entityName` / attribute 10140) vocabulary

`GetLocationNameFor` (`pie_d.cs:116432`) returns the enclosing zone's
unlocalized name, and the motion curve is looked up as `From<zone>` /
`To<zone>`. The recognised names (`pie_d.cs:179303`–`179410`):

```
playmat  hand  deck  discard  bench  activePokemonArea  activeStadium
activeTrainer  prizePile  outOfPlay  lostZone
```

plus the pseudo-locations `PipTray`, `AbilitySelect`, `TargetSelect`, each
optionally suffixed `Attachment` when the entity's parent is a Pokémon card.


---

## 2. Setup selection and mulligans

### 2.1 The messages, in full

`SelectionWithTargetsRequired` — `core_d.cs:15817`, `[DwdJsonMessage(false)]`,
extends `SelectionMessage` (`core_d.cs:73924`) extends `GameMessage`
(`core_d.cs:11623`). Complete field list (VERIFIED, all from `[JsonName]`):

| JSON field | C# type | notes |
|---|---|---|
| `gameID` | `GameID` | from `GameMessage` |
| `counter` | `int` | from `SelectionMessage`; echoed back verbatim |
| `prompt` | `LocalizableTextVariables` | the ROOT node's prompt; only shown while the root is Current |
| `offerLength` | `long` | seconds; 0 = no timer |
| `startingTimestamp` | `long` | ms epoch for the timer; 0 = none |
| `targetMap` | `Dictionary<EntityID, TargetInformation[]>` | **the payload** |
| `optimalPlayMap` | `Tuple<EntityID, TargetPreference>[]` | iterated unguarded in `HintStrength` (`core_d.cs:71877`) — **never null**; `[]` is fine |
| `forced` | `bool` | |
| `targetType` | `string` | becomes the ROOT node's `Kind` (`GetKind`, `core_d.cs:71921`) |
| `ignoreFirst` | `bool` | |
| `selectionParams` | `Dictionary<string,object>` | opaque; only exposed via `get_Params()` |
| `sourceID` | `EntityID` | `get_SourceID()`; used for prompt anchoring |

`CakeSelectionWithTargetsRequired` (`pie_d.cs:123049`) is a subclass that only
re-declares `prompt`. There is no reason to prefer it.

`TargetInformation` (abstract, `core_d.cs:16618`):

| JSON field | C# type | default | notes |
|---|---|---|---|
| `name` | `string` | — | **the type hint** — required, or deserialization throws |
| `selected` | `bool` | `true` | `false` ⇒ the node auto-advances without asking **and contributes no response** |
| `accountID` | `AccountID` | null | |
| `targetPrompt` | `LocalizableTextVariables` | null | this node's prompt. null is safe: `CanShowPrompt()` null-checks it (`pie_d.cs:192340`) |

`EntityListTargetInformation` (`core_d.cs:16633`) adds:

| JSON field | C# type | notes |
|---|---|---|
| `validTargets` | `EntityID[]` | **never null** — `.Length` is read unguarded (`core_d.cs:72875`) |
| `numberToSelect` | `int` | max; `-1` = unlimited (`get_AvailableSelections`, `core_d.cs:72900`) |
| `forced` | `bool` | gates `get_satisfied` (`core_d.cs:72880`) |
| `minimumToSelect` | `int` | `-1` means "use `min(validTargets.Length, numberToSelect)`" (`core_d.cs:72946`) |
| `hintTargetMap` | `Dictionary<TargetStrength, EntityID[]>` | iterated unguarded (`core_d.cs:72957`) — **never null**; `{}` is fine. Keys are enum names: `Strong Weak Negative Forced Enhanced Depleted Super CanAutoSelect Any ForcedAttacker` |

`ActivePokemonTargetInformation` (`pie_d.cs:200149`) and
`InitialBenchedTargetInformation` (`pie_d.cs:200155`) are **empty subclasses of
`EntityListTargetInformation`**. They add no fields. Their entire purpose is the
`name` string, which becomes the node's `Kind` and picks the UI command.

### 2.2 What `ignoreFirst` + `forced` actually do (VERIFIED)

`SelectionWithTargetsNode` (`core_d.cs:71724`):

- **ctor** (`:71738`) — for each `targetMap` entry it keeps only the
  `TargetInformation`s with `selected == true`, builds a node from the **first**
  and passes `Skip(1)` as `subsequent`. `mayCancel` for that first node is
  `!Forced || !IgnoreFirst`.
- `TargetInfoNode`'s ctor (`core_d.cs:73183`) chains `subsequent` the same way,
  each with `mayCancel: true`. **This is how "Active then Bench" is one offer:
  the second `TargetInformation` in the array becomes a child of the first.**
- **`enter(fromCancel)`** (`:71903`) — when `ignoreFirst`:
  ```csharp
  if (get_AvailableSelections().Count() != 1)
      throw new InvalidOperationException(
          "Ignore first is only valid if there was only one selection available!");
  selection = get_AvailableSelections().First();
  Advance();
  ```
  `AvailableSelections` for the root is exactly the `targetMap` keys
  (`:71792`). Hence **exactly one `targetMap` key**, as CLAUDE.md says. The
  throw escapes into the message pump — fatal.
- `MayAdvance` / `MayCancel` on the root require `selection != null` when
  `forced` (`:71774`, `:71833`).

### 2.3 The response (VERIFIED)

`RootSelection.Advance()` (`core_d.cs:71284`) writes
`getResponseMessage(false)`; for this node that is
`Outgoing.SelectionWithTargets(message, selection, getResponses())`
(`core_d.cs:71893`).

`getResponses()` (`core_d.cs:71360`) walks from the **current** node up to the
root, collecting `TargetInfoNode.GetResponse()` (null when
`targetInfo.Selected == false`), then **reverses** — so the order matches the
order of the `TargetInformation[]` array.

`SelectionWithTargets` (`core_d.cs:74086`) serializes as:

```json
{"name":"SelectionWithTargets","value":{
  "gameID":"<guid>",
  "counter": 7,
  "selection": {                       // null if the player cancelled out
    "entityID":"<the single targetMap key>",
    "targetResponses":[
      {"name":"EntityListTargetResponse","entityList":["<active guid>"]},
      {"name":"EntityListTargetResponse","entityList":["<bench1>","<bench2>"]}
    ]}}}
```

`EntityListTargetResponse` = `core_d.cs:16705`; `IntTargetResponse`
(`{"amount":N,"name":"IntTargetResponse"}`) = `core_d.cs:16739`.

**The whole setup comes back in ONE message.** The bench node has no children,
so `NodeToAdvanceTo()` returns null and `Advance()` sends.

### 2.4 Concrete shape to send for setup

Every consumer above is VERIFIED; this exact payload has not yet been run
against the live client.

```json
{"name":"SequenceMessage","value":{
 "sequenceID":"00000000-0000-0000-0000-000000000000",
 "msg":{"name":"SelectionWithTargetsRequired","value":{
  "gameID":"<game>",
  "counter": 1,
  "prompt": "com.direwolfdigital.cake.rules.states.startgame.selectstartingpokemon",
  "offerLength": 0,
  "startingTimestamp": 0,
  "forced": true,
  "ignoreFirst": true,
  "targetType": "",
  "optimalPlayMap": [],
  "selectionParams": {},
  "sourceID": null,
  "targetMap": {
    "<player entity guid>": [
      {"name":"ActivePokemonTargetInformation",
       "selected": true,
       "accountID": null,
       "targetPrompt": "com.direwolfdigital.cake.rules.states.startgame.selectstartingpokemon",
       "validTargets": ["<basic in hand>", "..."],
       "numberToSelect": 1,
       "minimumToSelect": 1,
       "forced": true,
       "hintTargetMap": {}},
      {"name":"InitialBenchedTargetInformation",
       "selected": true,
       "accountID": null,
       "targetPrompt": "playmat.gamestart.promptbenchpokemon.new",
       "validTargets": ["<remaining basics in hand>", "..."],
       "numberToSelect": 5,
       "minimumToSelect": 0,
       "forced": false,
       "hintTargetMap": {}}
    ]}}}}}
```

Notes on each choice:

- **`targetType: ""`** is deliberate. `PieSelectionNodeCommandFactory.Update`
  (`pie_d.cs:155948`) does `factories.ContainsKey(currentChoice.get_Kind())`;
  `""` is not a key, so the root would fall through to `getDefaultCommand`.
  That never happens here because `ignoreFirst` advances off the root
  synchronously inside `Enter()` (which `Selection.BeginOffer` calls) before
  `Update` ever polls. Do **not** put `"ActivePokemonTargetInformation"` in
  `targetType` — the per-node `name` is what drives the UI.
  *(VERIFIED that `enter()` advances inside `Enter()`; INFERRED that the root's
  command is therefore never constructed.)*
- **the single `targetMap` key** can be any entity the client already knows. It
  is echoed back as `selection.entityID` and is not used for layout on this
  path; the player entity is the natural choice.
  *(VERIFIED that it must be exactly one key and that it is echoed; INFERRED
  that its identity is otherwise unused here — I found no consumer.)*
- **how many bench Pokémon** is purely `numberToSelect` on the
  `InitialBenchedTargetInformation`. VERIFIED:
  `EntityListTargetNode.get_MaxToSelect()` returns it (`core_d.cs:72941`) and
  `get_AvailableSelections()` returns the empty list once
  `previousSelections.Count >= MaxToSelect` (`core_d.cs:72900`). Bench
  attribute **201920** is only the *layout* divisor (`BenchLayout`); it does
  not constrain selection. Send `min(5, free slots)`.
- **the Done button**: `setLabelTextFromSelectionContext` (`pie_d.cs:137988`)
  special-cases `Kind == "InitialBenchedTargetInformation"` → label
  `common.dialog.done`, `showCancel = false`, regardless of how many are
  selected. With `minimumToSelect: 0` / `forced: false` the player may finish
  with an empty bench at any time. VERIFIED.
- **drag and drop**: `l.x` (Active, `pie_d.cs:141031`) and `l.T` (Bench,
  `pie_d.cs:140585`) are the two `SelectionCommand`s the factory picks
  (`pie_d.cs:155744`–`155757`). Both return `AllowsClickAndDrag() == true` and a
  `GetHintOverride()` pointing at `PlaymatProvider.activeTransform` /
  `benchTransform` — i.e. they only light up the drop zone. **They do not move
  the card.** The server must still send the `EntityMoved`s afterwards, ideally
  inside `IntroduceInitialPokemon` (which plays a `FlipOver` per introduced
  card) or `PlayActive` / `PlayCard`.

### 2.5 The prompt keys the real server used

The localization DB carries the old server's own prompt namespace,
`com.direwolfdigital.cake.rules.*` — direct evidence of what was sent:

| key | English |
|---|---|
| `…rules.states.startgame.selectstartingpokemon` | Choose a Pokémon to be your Active Pokémon. |
| `…rules.states.startgame.selectstartingpokemon.title` | Starting Pokémon |
| `…rules.states.startgame.opponentselectstartingpokemon` | Please wait while your opponent chooses an Active Pokémon. |
| `…rules.states.startgame.opponentselectstartingbenchedpokemon` | Please wait while your opponent chooses Benched Pokémon. |
| `…rules.states.startgame.coinflip` / `.heads` / `.tails` | Coin Flip / Heads / Tails |
| `…rules.states.actionphase.selectaction` | Pick an Action |

Client-side bench prompts: `playmat.gamestart.promptbenchpokemon.new`
("Choose Pokémon to start on your Bench."),
`playmat.gamestart.prompt.novalidbenchselections`,
`playmat.gamestart.promptbenchpokemon.title`. Full tables in §6.

**Two of the original prompt ids are deliberately hidden by the client.**
`PiePromptListener.suppressedKeys` (`pie_d.cs:140758`) contains
`com.direwolfdigital.cake.rules.states.ActionPhase.SelectAction` and
`com.direwolfdigital.cake.rules.actions.cake.DrawPrize.Prompt1` (compared
case-insensitively via `LocalizableText.HasId`, `core_d.cs:76425`). Using those
ids means no banner, which is what the original did.

### 2.6 Mulligans — what to send instead of `MulliganChoiceRequired`

**Why `MulliganChoiceRequired` hangs (VERIFIED).**
`MulliganChoiceNode` (`core_d.cs:70914`) implements `IMulliganChoice`. Across
both assemblies **`IMulliganChoice` appears exactly twice: the interface
declaration (`core_d.cs:70687`) and that one implementation.** Nothing in
`pie-src` ever calls `MakeMulliganChoice`, so `mulliganChoice` stays null,
`MayAdvance` stays false, and no response is ever sent. The node also inherits
the default `GetKind()` → `""` (`core_d.cs:72271`), which is not a key in
`PieSelectionNodeCommandFactory.factories`, and it is neither `ICustomChoice`
nor `IEntityListSelection`, so `getDefaultCommand` (`pie_d.cs:155884`) returns
null and the client installs a `NullSelectionCommand`. Dead end, confirmed.

**What IS sent (VERIFIED consumers):**

1. **The mulligan reveal carousel** — `MulliganRevealCardsEffect`
   (`pie_d.cs:14402`, `[DwdJsonEffect]`), consumed by `b.N`
   (`pie_d.cs:14774`, `[MessageCommandConstructor(IsOverride = true)]`).

   | JSON field | type | use |
   |---|---|---|
   | `player` | `AccountID` | whose mulligans; the dialog only re-introduces hand cards when this is the local player (`pie_d.cs:7935`) |
   | `entityIDPiles` | `List<Dictionary<EntityID, ReadOnlyAttributes>>` | **one dict per mulliganed hand**; the carousel pages through them, `mulliganCount = Count - 1` |
   | `prompt` | `LocalizableText` | sub-header, used as `string.Format(L.LT(prompt), piles.Count)` *(INFERRED — the decompiler collapsed `message.A`; it is the only `LocalizableText` in that `string.Format`)* |
   | `revealTitle` | `LocalizableText` | |
   | `revealSource` | `EntityID` | |

   The dialog **introduces the listed entities itself** from the inline
   `ReadOnlyAttributes` (`pie_d.cs:8030`), so the server does not need prior
   `EntityIntroduced` messages for them, and it un-introduces them on close.
   `b.N.execute()` blocks until the dialog is dismissed **or** a timeout read
   from the `"match"` `DWDDataManager` model elapses (`pie_d.cs:14790`) — so it
   cannot hang forever. Header key `playmat.mulligan.dialog.carousel.header`,
   button `common.dialog.done`.

2. **Wrap it in the `Mulligan` sequence.** `v` (`pie_d.cs:142699`,
   `[SequenceCommandConstructor("Mulligan")]`) runs each child and then spins
   while `model.c` is true — and `model.c` is set true in the reveal dialog's
   `Awake` and false in its `OnDestroy` (`pie_d.cs:7916`, `pie_d.cs:8022`).
   That is what stops the rest of the game animating behind the dialog.
   `RevealMulligans` (`pie_d.cs:15287`) is a plain serial sequence.

3. **The "your opponent mulliganed, draw a card?" prompt** is an ordinary
   `CustomChoiceRequired` yes/no. Evidence: the loc keys
   `com.direwolfdigital.cake.rules.states.startgame.mulligancustomchoice`
   ("Your opponent had no Basic Pokémon and had to draw a new hand. Would you
   like to draw a card?"), `.drawmultiple` ("…for mulligan {0}?"),
   `.mulliganchoicechoice1` = "Yes", `.mulliganchoicechoice2` = "No".
   *(INFERRED from the key names plus the `CustomChoiceRequired.buttons` shape;
   the server that sent them is gone.)*

4. **Client-side mulligan banners** you can drive with `PauseOnPromptEffect`
   (§3.5): `playmat.gamestart.prompt.playernopokemon.playmat2`,
   `.playermulliganwait`, `.playermulligandone`, `.oppmulligan`,
   `.bothnopokemon`, `.opponentnopokemon`.


---

## 3. Animation hooks

**Direct answer to the question asked:** yes — there is a real scripting
surface, and it is larger than `EntityMoved`. There are three separate
mechanisms — **named sequences**, **effect messages**, and
**`RunClientAction`** — plus one important negative: a large fraction of the
effect-message classes that *exist* in this build have **no consumer at all**
and are silently dropped.

### 3.1 `EntityMoved` — and what `animDuration` really does

`EntityMoved` (`core_d.cs:11600`): `entityID`, `destinationID`,
`positionInParent` (int), `animDuration` (int).

Consumed by `m.I` (`pie_d.cs:143448`). **`animDuration` is in milliseconds**
(`float duration = (float)a / 1000f;`, `pie_d.cs:143494`) and its **only** use
is `StartCoroutine(makeLogDisplayable(duration))` (`pie_d.cs:143594`) — a
`WaitForSeconds` before the game-log line becomes visible. VERIFIED by reading
every use of `duration` inside `MoveEntity`.

The card's actual flight time comes from a `CurveMotion` prefab looked up by
the animation-curve stack `From<zone>` / `To<zone>`
(`CurveMotionProvider.GetPathFor`, `pie_d.cs:116465`). **You cannot speed up or
slow down a card move from the server.** What you *can* change is which curve
is chosen, by changing the source/destination zone.

Special cases inside `m.I`: a move whose old **or** new parent is the `playmat`
skips animation entirely (`pie_d.cs:143491`); `prizePile → hand` plays a
dedicated path and updates the prize-count audio (`pie_d.cs:143550`); a Stadium
moving to `activeStadium` swaps the previous stadium out (`pie_d.cs:143506`).

### 3.2 Named sequences — the real animation vocabulary

61 names, from `[SequenceCommandConstructor]` in `pie-src` (there are none in
`core` or `pie-core`). Send `StartSequence{sequenceID,name,attributes}`, the
children as `SequenceMessage`s with the same `sequenceID`, then `StopSequence`.

```
ActiveCardAndAttachmentsShuffled  ActivePlayerSet   AddSpecialCondition
AlwaysReveal    AttachToVUnion    Attack            BenchSizeModified
BurnDamage      ClosePrizePile    CreateLegend      CreateVUnion
DealInitialHands  DealInitialPrizeCards  Devolve    DiscardRetreatCost
DismissAbilitySelect  Draw        DrawPrizeCard     Evolve
FlipForBurn     FlipForConfusion  FlipToWakeUp      GroupedMove
HandShuffledAndMovedToDeck  HurtFromConfusion       InitialCoinFlip
InitialPick     IntroduceInitialPokemon  Knockout   MoveFromBottomOfDeck
MoveFromMiddleOfDeck  MoveFromTopOfDeck   Mulligan  OpponentChoosingToGoFirst
OpponentPickingHeadsOrTails  ParallelSequence       PlayActive
PlayCard        PlayEnergy        PlayTool          PoisonDamage
PokeAbility     RecursiveReturnToOwnersHand  RemoveSpecialCondition
ReplaceActive   Retreat           RevealAndSkipMove RevealMulligans
RoboSubstitute  SerialSequence    SimultaneousFlipThenAction
StadiumPresent  TrainerCard       TrainerPresent    TransformEntity
TransformSwap   UseStadiumAbility VUnionBreakSequence  WithOpenPrizeCards
WonderLock
```

The ones worth knowing (all VERIFIED by reading `executeSequence`):

| name | line | what it adds |
|---|---|---|
| `GroupedMove` | `pie_d.cs:141530` | runs children **in parallel with a 0.2 s stagger** (`k.z(…, 0.2f)`). This is the fan-out. |
| `DealInitialHands` | `pie_d.cs:32581` | the deal |
| `DealInitialPrizeCards` | `pie_d.cs:142986` | 0.1 s stagger, then turns on each `player*PrizeCount` badge if that pile is non-empty |
| `IntroduceInitialPokemon` | `pie_d.cs:141473` | plays a `FlipOver` on every `EntityIntroduced` child, 0.2 s apart, under a Reveal layer |
| `InitialCoinFlip` | `pie_d.cs:141441` | pushes the nested `MultipleCoinFlipWithContextEffect` result onto **both** coin animators |
| `ActivePlayerSet` | `pie_d.cs:141074` | hides the coin-flip dialog and resets both coins before running children |
| `Attack` | `pie_d.cs:141788` | the big one: moves the attacker to the target-select area, plays the ability pop-in (`popinFrame<Type>`), groups shield effects, then the hit |
| `Knockout` | `pie_d.cs:147946` | animates `FromActive`→`ToKnockOut` or `FromBench`→`ToBenchKnockOut` for whichever child `EntityMoved` takes a Pokémon out of play, then runs the moves |
| `DrawPrizeCard` | `pie_d.cs:142745` | prize-pile present/close choreography |
| `Draw` | `pie_d.cs:143090` | buckets children by card type (Pokémon / Trainer / Energy / other) for the draw fan |
| `Mulligan` | `pie_d.cs:142703` | blocks until the mulligan dialog closes |
| `Evolve` | `pie_d.cs:141630` | needs `From` and `Into` data effects (§1.6) |
| `ParallelSequence` / `SerialSequence` | `pie_d.cs:200677` / `200733` | generic run-all-at-once / run-in-order |

`SerialSequence` is a generic run-in-order primitive.

**`ParallelSequence` IS BROKEN — never send it.** Its
`private IList<Command> A` is declared and never assigned (the constructor
passes `sequence` to the base and nothing sets `A`), so `executeSequence`'s
`foreach (Command item in A)` throws a `NullReferenceException` every single
time. The throw escapes the sequence and kills the message pump, ending the
game. Verified against a live client: a reveal wrapped in one stopped play
dead, with `r.M+m.MoveNext` at the top of the stack. There is no generic
"run these at once" sequence in this build.

### 3.3 Effect messages — full inventory, and which ones are DEAD

An effect is sent as
`EffectPlayed{effectMessage:{"name":"<Class>","value":{…}}}`, itself normally
inside a `SequenceMessage`.

**LIVE** (has a `[MessageCommandConstructor]`, so it does something):

| effect | fields | consumer | what it does |
|---|---|---|---|
| `CakeAttackEffect` `pie_d.cs:122477` | `damageSource`, `entityID`, `weaknessTriggered`, `resistanceTrigger`, `damageType` (string[]), `attackName`, `damageAmount`, `damageModification`, `visualType` | `m.m` `pie_d.cs:143785` | hit FX + damage number + KO |
| `PlaceDamageEffect` `pie_d.cs:122557` | `destinationID`, `originID`, `amount`, `abilityName` | `m.p` `pie_d.cs:144209` | damage-counter placement |
| `MultipleCoinFlipWithContextEffect` `pie_d.cs:123001` | `resultLst` (int[]), `title`, `source`, `targets`, `gameText` | `L.x` `pie_d.cs:137096` | the coin |
| `AbilityPlayedEffect` `pie_d.cs:116769` | `eID`, `abilityID`, `abilityTitle`, `abilityType` | `L.o` `pie_d.cs:136575` | ability pop-in |
| `AbilityFinishedEffect` `pie_d.cs:122443` | `eID` | `k.U` `pie_d.cs:133972` | closes it |
| `CakeAbilitySelectedEffect` `pie_d.cs:122465` | `entityID`, `abilityName`, `abilityType` | `L.V` `pie_d.cs:137025` | **empty body** |
| `CleanupAttackEffect` `pie_d.cs:14387` | `entityID`, `cleanupCurvePrefix` | `b.j` `pie_d.cs:14603` | returns the attacker home |
| `GXAttackUsedEffect` `pie_d.cs:14396` | `user` | `b.M` `pie_d.cs:14750` | flips the GX token |
| `VSTARPowerUsedEffect` `pie_d.cs:203431` | `user` | `P.Y` `pie_d.cs:185902` | flips the VSTAR token |
| `MulliganRevealCardsEffect` `pie_d.cs:14402` | see §2.6 | `b.N` `pie_d.cs:14774` | mulligan carousel |
| `RevealCardsToAllEffect` `pie_d.cs:122608` | `entityID` (EntityID[]), `playerPrompt` (Dictionary<AccountID,LocalizableText>), `revealTitle`, `revealSource`, `prompt` | `m.s` `pie_d.cs:144480` | reveal dialog; **blocks until `RevealClosed`** |
| `RevealCardsToPlayerEffect` `pie_d.cs:122626` | `player`, `entityID`, `prompt`, `revealTitle`, `revealSource` | `m.t` `pie_d.cs:144534` | reveal dialog for one player |
| `RevealCardToAllEffect` `pie_d.cs:65656` | `entityID` (Guid), `Return` (bool), `alwaysReveal` | `m.u` `pie_d.cs:144600` | one card flies to the reveal area, waits, optionally flies back |
| `RockPaperScissorsEffect` `pie_d.cs:122644` | `choices` (Dictionary<AccountID,int?>) | `m.w` `pie_d.cs:144703` | |
| `EvolveWithContextEffect` `pie_d.cs:122535` | `source`, `targets` | `m.K` `pie_d.cs:143729` | **empty body** |
| `CreatureHealWithContextEvent` `pie_d.cs:122515` | `source`, `targets`, `amount` | `L.y` `pie_d.cs:137284` | heal number |
| `ShieldTargetsEffect` `pie_d.cs:14420` | `source`, `targets`, `wasDamage` | `b.U` `pie_d.cs:15305` | shield / no-damage FX |
| `NonDamagingTargetsEffect` `pie_d.cs:29928` | `targets` | `S.j` `pie_d.cs:202125` | marks targets for the log (empty body) |
| `PauseOnPromptEffect` `pie_d.cs:65644` | `buttonText`, `prompt`, `doPause` | `m.l` `pie_d.cs:143749` | **generic on-screen text** — see §3.5 |
| `ClosePauseOnPromptEffect` `pie_d.cs:21905` | none | `d.C` `pie_d.cs:30855` | clears it |
| `PostActionPhaseEffect` `pie_d.cs:29934` | none | `d.D` `pie_d.cs:30870` | returns the select-areas to rest |
| `EnergySwapEffect` `pie_d.cs:226247` | | `v.y` `pie_d.cs:226282` | |
| `PileReordered` `pie_d.cs:7349` | `entityID`, `children` (Guid[]) | `a.o` `pie_d.cs:7379` | re-sorts a pile |
| `DrawFromBottom` `pie_d.cs:203414` | `playerID`, `numOfTargets` | `S.e` `pie_d.cs:201881` | |
| `PlaceOnBottom` `pie_d.cs:203423` | `entityID`, `target` | `S.I` `pie_d.cs:202037` | |
| `EntityIDDataEffect` / `EntityIDListDataEffect` / `IntDataEffect` / `StringDataEffect` / `SourceTargetDataEffect` | `key`, `value` | `core_d.cs:70227`–`70285` | sequence operands (§1.6) |

**DEAD — declared but with no consumer anywhere. Sending one produces `Noop()`:**

`AnimationDelayEffect` (`core_d.cs:11428`, `duration` int),
`AnimationDelayEffectV2` (`core_d.cs:351`, `duration` float),
`BlinkEffect`, `DefendEffect`, `ClearDefendEffect`, `SwapDefenderEffect`,
`PhaseChangeEffect`, `PromptMessage`, `GameLogMessage`, `GameEffectMessage`,
`ModifyStackDisplay`, `PushStackDisplay`, `RemoveStackDisplay`,
`WaitForTargetOnEffect`, `WaitForTargetOffEffect`,
`MultipleCoinFlipEffect` (the non-`WithContext` one),
`WaitingForOpponentEffect`, `DoneWaitingForOpponentEffect`,
`AddSpecialConditionEffect`, `RemoveSpecialConditionEffect`, `EvolveEffect`,
`HealWithContextEvent`, `CreatureHealEvent`, `BurnEffect`, `PoisonEffect`,
`SleepEffect`, `ParalyzeEffect`, `ConfuseEffect`, `RemoveBurnEffect`,
`RemovePoisonEffect`, `RemoveSleepEffect`, `RemoveParalyzeEffect`,
`RemoveConfuseEffect`, `MaterializeEntity`, `DematerializeEntity`,
`EntityMovedWithoutID`, `MapEntityID`, `SelectionFinished`, `GameEnded`,
`RevealOpened` (use `CakeRevealOpened`).

Method: for each class name, count occurrences across `pie_d.cs` and
`core_d.cs`. A count of 1 in `pie_d.cs` (or 1 in `core_d.cs` and 0 in
`pie_d.cs`) means only the declaration exists. Cross-checked against a
`[MessageCommandConstructor]` index built from both files, plus `sausage.txt`
(which registers exactly five: `AttributeModified`, `AttributeRemoved`,
`EntityAdded`, `EntityDestroyed`, `EntityIntroduced`) and `pie-core.dll` (which
registers none — 0 hits for `MessageCommandConstructorAttribute` in
`piecore.il`).

**Consequence for special conditions:** there is **no message that draws a
Poisoned / Burned / Asleep marker**. Those come from `AttributeModified` on the
Pokémon plus the `AddSpecialCondition` / `RemoveSpecialCondition` named
sequences (`pie_d.cs:142613` / `142536`). `MecanimController.SetData`
(`pie_d.cs:116490`) drives `burnOn` / `poisonOn` from the entity's own model.
The `SpecialConditions` enum (`pie_d.cs:177960`) is
`Asleep=0, Burned=1, Confused=2, Paralyzed=3, Poisoned=4, UNSET=-1`.

`SelectionFinished` deserves calling out: `SelectionEndOffer`
(`core_d.cs:72155`) takes it in a constructor, but that constructor carries
**no `[MessageCommandConstructor]` attribute** — VERIFIED in IL at
`coredll.il:267118` (`.method public hidebysig specialname rtspecialname
instance void .ctor (class SelectionFinished message)` with no `.custom` line).
It is also not needed: `Selection` subscribes `offerSent → EndOffer`
(`core_d.cs:72124`, `:72151`), so an offer closes itself the moment the client
sends its reply. To *cancel* an outstanding offer, send
**`ForceSelectionFinished`** (`pie_d.cs:29938`) — a bare `NetworkMessageEvent`
with **no fields and no gameID** — consumed by `m.d` (`pie_d.cs:143181`), which
calls `SelectionUtils.ForceEndSelection()` and tidies up the coin-flip and
go-first dialogs.

### 3.4 `RunClientAction` — the generic "do this on the client" hook

This is the closest thing to a scripting API, and the current server does not
use it at all.

`RunClientAction` (`core_d.cs:35993`) is a `[DwdJsonMessage]` `GameMessage`
with one field, `clientAction`, of the `[TypeHinting("name")]` type
`ClientAction`. `RunClientActionCommand` (`core_d.cs:36096`) hands it to
`ClientEventResponder.RunClientAction` (`core_d.cs:35890`) and **waits for the
resulting command to complete** — so it can be used as a blocking beat inside a
sequence.

`getClientActionCommand` in pie (`pie_d.cs:195082`) dispatches through a
`Dictionary<Type, …>` (`pie_d.cs:195019`); anything absent returns `Noop()`.
**Implemented** (VERIFIED, that dictionary):

| `name` | fields | declared at |
|---|---|---|
| `ShowPopup` | `text`, `title`, `instructionString` (all `LocalizableText`), `handle` (`PopupID`), `blocking` (bool), `modal` (bool) | `core_d.cs:36700` |
| `HidePopup` | (pie type) | `pie_d.cs:33578` |
| `ShowScreenOverlay` / `HideScreenOverlay` | (pie types) | `pie_d.cs:33709` / `33588` |
| `ShowArrow` | `entityIDs` (EntityID[]), `handle` (`ArrowID`), `additionalParams` (string), `label` (`LocalizableText`), `waitMilliseconds` (long), `kwargs` (Dictionary<string,string>) | `core_d.cs:36667` |
| `HideArrow` | `handle` | `core_d.cs:36626` |
| `ShowAbilityMenuArrow` | | `pie_d.cs:33689` |
| `RequirePileZoom` | | `pie_d.cs:33699` |
| `FailFeedback` | `text` (`LocalizableText`) | `core_d.cs:36574` |
| `LogToAnalytics` | `kontagentName`, `s1`, `s2`, `s3` | `core_d.cs:36604` |
| `DisableReminders` | | `pie_d.cs:33686` |
| `ShowBanter` | | `pie_d.cs:16194` |
| `NopBlock` | none — **throws** unless it arrives with a trigger (`core_d.cs:36090`) | `core_d.cs:36646` |
| `InstallEventResponse` | `eventResponse` (`ClientEventResponse`) | `core_d.cs:36636` |
| `UninstallEventResponse` | `handle` | `core_d.cs:36730` |

**Declared but NOT in the dispatch table → `Noop`:** `ForceZoomEntity`,
`ForceUnzoomEntity`, `SetIndicator`, `Voiceover`, `WaitForMouse`.

`InstallEventResponse` is the most powerful: it registers a
`ClientEventResponse{event: ClientEvent, action: ClientAction[], handle}`
(`core_d.cs:35937`) so that a *client-side* event — `AbilityMenuDisplayed`,
`AttackSelected`, `AttackSelectedOtherThan`, `ClickToContinue`,
`RetreatSelected`, `ZoomEntity`, `CardInHandSelected`, `ActiveSelected`,
`ClosedPileZoom`, … (`pie_d.cs:195093`–`195200`) — fires actions later with no
round trip. This is the tutorial engine. It is fully functional and completely
unused by our server.

### 3.5 A generic "show this text" hook

`PauseOnPromptEffect` (`pie_d.cs:65644`) → `m.l` (`pie_d.cs:143749`):

```csharp
promptListener.OverrideShowPrompt = true;
promptListener.OverrideText = msg.prompt.get_DisplayText();
if (!msg.doPause) yield break;              // text stays up, queue continues
playmatProvider.PausedOnPrompt = true;      // ... otherwise the queue blocks
```

With `doPause: false` it sets a banner and returns immediately; with
`doPause: true` it blocks the message queue until the player clicks or a
timeout from the `"match"` data model expires. `ClosePauseOnPromptEffect`
clears it. `PiePromptListener.LateUpdate` (`pie_d.cs:140853`) prefers
`OverrideText` over the selection prompt whenever it is non-empty.

This is the intended way to say "your opponent is choosing", "you have no Basic
Pokémon", and so on. *(VERIFIED what it does; INFERRED that this is what the
original used for those particular messages.)*

### 3.6 The coin flip (VERIFIED end to end)

Two separate things.

**(a) The opening call.** `GoFirstChoiceRequired` (`pie_d.cs:116784`) —
`SelectionMessage` plus `sortType`, `buttons` (`LocalizableTextVariables[]`),
`sourceEntity`. Node `l.r` (`pie_d.cs:140401`), `GetKind()` →
`"GoFirstChoice"`; reply is `Outgoing.CustomChoice` →
**`GameCustomChoice{gameID, selection:int, counter}`**, with `-1` for cancel.
`CoinFlipChoiceRequired` (`pie_d.cs:122431`) is the identical shape with
`GetKind()` → `"CoinFlipChoice"` (node `l.S`, `pie_d.cs:140488`) and is the
heads/tails call. Button keys `…startgame.heads` / `…startgame.tails`; prompt
`…startgame.coinflip` or `playmat.gamestart.prompt.coinflipchoice`.
Note `PieSelectionNodeCommandFactory.Update` special-cases `"GoFirstChoice"`
when the coin animator is still in its `Start` state (`pie_d.cs:155962`).

**(b) In-game flips.** `MultipleCoinFlipWithContextEffect` (`pie_d.cs:123001`),
consumed by `L.x` (`pie_d.cs:137096`):

| field | type | meaning |
|---|---|---|
| `resultLst` | `int[]` | one entry per flip; **`0` = heads, anything else = tails** (`get_Result()`, `pie_d.cs:137158`) |
| `title` | `LocalizableText` | shown in the results panel |
| `source` | `EntityID` | picks whose coin animates — `All[source].OwningPlayerID == myAccount ? player1Coin : player2Coin` (`pie_d.cs:137180`) |
| `targets` | `EntityID[]` | |
| `gameText` | `LocalizableText` | |

`execute()` (`pie_d.cs:137196`) plays each flip with a shrinking delay
(0.5 s × 0.9ⁿ, floored at 0.3 s), sets `multiHeads` / `multiTails` triggers when
there is more than one flip, and finally calls
`gamelog.FlipCoin(player, heads, tails, displayText)`. Bracket it in an
`InitialCoinFlip` sequence for the opening flip (`pie_d.cs:141441` reads the
child's result and pushes it onto **both** coins), or in `FlipForBurn` /
`FlipForConfusion` / `FlipToWakeUp` / `SimultaneousFlipThenAction` for the
rules flips.

Prompt keys: `playmat.prompt.coinflip.attack`, `.pokeability`, `.trainer`,
`.burned`, `.opponentattack`, `.opponentconfused`, `.opponentasleep`,
`.title` ("Coin Flip Results"), `playmat.controls.coinflipresults`
("Heads {0}, Tails {1}"), `playmat.prompt.flipacoin`, `.flipcoins`.

### 3.7 Attack, damage numbers and knockout (VERIFIED)

`m.m` (`pie_d.cs:143785`), the `CakeAttackEffect` command; `Play()` at
`pie_d.cs:143885`:

- The FX path is
  `"Basic" + damageType[last] + "/HitFX_" + damageType[last].ToLower()` with a
  suffix chosen by `damageAmount`: `_Light` (<50), `_Medium` (50–99), `_Hard`
  (≥100). An empty `damageType` gives
  `BasicColorless/HitFX_colorless_Light`. The strings are energy-type names
  (`Fire`, `Water`, `Colorless`, …).
- **The knockout is decided client-side**:
  `C = damageAmount >= defender.TryGetOne<s.B>().get_Current()` — the effect's
  damage against the defender's *current HP attribute*. When true it fires the
  `KnockOut` mecanim trigger and pushes the card into `knockoutPile`;
  otherwise `Light` / `Medium` / `Hard`.
- If the defender is asleep / paralyzed / confused the KO branch fires that
  trigger first (`pie_d.cs:143928`).
- The **damage number** is a `NumericFlyText` set to `damageAmount`
  (`PlayDamageFlyText`, `pie_d.cs:144004`) with a rider from `SetDamageMod`
  (`pie_d.cs:144017`): `playmat.cardattribute.weakness` when
  `weaknessTriggered`, `…resistance` when `resistanceTrigger`,
  `…damageincrease` / `…damagedecrease` from the `damageModification` enum
  `{None=0, Weakness, Resistance, DamageIncrease, DamageDecrease}`
  (`pie_d.cs:144052`). *(INFERRED which collapsed field is which; the enum and
  the four loc keys are VERIFIED.)*
- `visualType` is `InteractionVisualizations {DamagingAction=0,
  NonDamagingAction=1}` (`pie_d.cs:152820`). `NonDamagingAction` suppresses the
  hit FX entirely.
- The knockout pile is **flushed on the next `ActivePlayerSet`**
  (`pie_d.cs:136710`), which animates the KO'd cards away. The move to the
  discard itself is the server's `EntityMoved` inside a `Knockout` sequence.

HP needs no message: as CLAUDE.md says, the HP attribute carries current in
`value` and max in `originalValue`, and the client subtracts.

### 3.8 Shuffle, deal, and the turn banner

- **Shuffle**: `Shuffled{gameID, entityID}` (`core_d.cs:11651`) → `c.m`
  (`pie_d.cs:21191`). The animator is chosen from the target pile's identity:
  deck → `player*DeckAnimator`, trigger `deckShuffle`; prize pile →
  `player*PrizeDeckAnimator`; anything else → `player*HandShuffleAnimator` with
  `handShuffle`, or `ignoreShuffle` when the pile holds a single childless
  card. Deck shuffles are skipped when the deck has ≤ 2 children.
- **Deal**: `DealInitialHands` wrapping per-player `GroupedMove`s, then
  `IntroduceInitialPokemon`, then `DealInitialPrizeCards` — exactly what
  `Match.opening_animation` already does.
- **"YOUR TURN" banner**: driven entirely by `ActivePlayerSet`
  (`core_d.cs:11413`, one field `accountID`) → `L.Q` (`pie_d.cs:136658`). It
  flips the active/inactive player flags, **increments the turn counter**
  (`this.A.A++` — the double-count CLAUDE.md warns about), plays
  `PlayerTurnIndicator` or `OpponentTurnIndicator` and waits for the clip
  length, then clears the knockout pile. The banner **text is a baked prefab**
  using `playmat.turnindicator.yourturn` / `.opponentturn`; the server never
  sends it. **`playmat.prompt.yourturn` does not exist in the localization DB**
  — the only `yourturn` key in 27,550 rows is `playmat.turnindicator.yourturn`.
- **Game log** is entirely client-generated from the effect commands
  (`gamelog.FlipCoin`, `gameLog.StartTurn`, `gamelog.PendingNonDamage`,
  `gamelog.AddMatchResult`). The `gamelog.message.*` keys are formatted
  client-side; the server does not push log lines. `GameLogMessage` exists and
  is dead (§3.3).


---

## 4. Trainers, abilities and arbitrary in-match choices

### 4.1 The selection roots the client can build

`SelectionNodeFactory.SelectionMap` (`core_d.cs:72276`) plus pie's override
`n.t` (`pie_d.cs:156022`). Anything not in this table throws
`ArgumentException("Target Information was of an unsupported type!")`.

| message | node | node `Kind` | reply |
|---|---|---|---|
| `SelectionWithTargetsRequired` | `SelectionWithTargetsNode` | `targetType` | `SelectionWithTargets` |
| `SelectionWithTargetsAndActionsRequired` | `SelectionWithTargetsAndActionsNode` | `targetType` (`core_d.cs:71636`) | `SelectionWithTargetsAndActions` |
| `MultipleSelectionWithTargetsRequired` | `MultipleSelectionWithTargetsNode` | `""` | `MultipleSelectionWithTargets` |
| `CustomChoiceRequired` | `CustomChoiceNode` | its own `kind` field | `GameCustomChoice` |
| `CustomChoiceWithTargetsRequired` | `CustomChoiceWithTargetsNode` | `choiceKind` | `CustomChoiceWithTargets` |
| `ArchetypeCustomChoiceRequired` | `ArchetypeCustomChoiceNode` | | |
| `MulliganChoiceRequired` | `MulliganChoiceNode` | `""` | **never — do not send** |
| `GoFirstChoiceRequired` *(pie)* | `l.r` | `"GoFirstChoice"` | `GameCustomChoice` |
| `CoinFlipChoiceRequired` *(pie)* | `l.S` | `"CoinFlipChoice"` | `GameCustomChoice` |
| `ParameterizedLocCustomChoiceRequired` *(pie)* | `b.R` | `"ParameterizedLocCustomChoiceRequired"` | |
| `GetXCostChoiceRequired` | — **not in the map; sending it throws** | | |

### 4.2 Yes/no and multiple choice — `CustomChoiceRequired`

`core_d.cs:15778`:

| field | type | notes |
|---|---|---|
| `gameID`, `counter`, `prompt`, `offerLength`, `startingTimestamp` | inherited from `SelectionMessage` | |
| `sortType` | `string` | |
| `buttons` | `LocalizableTextVariables[]` | one button per entry; **`selection` is the index** |
| `sourceEntity` | `EntityID` | the card the prompt hangs off |
| `kind` | `string` | becomes the node's `Kind`; leave `""` unless you want a specialised UI |

`CustomChoiceNode` (`core_d.cs:70833`): `Select(int)` then `Advance()` sends

```json
{"name":"GameCustomChoice","value":{"gameID":"<guid>","selection":0,"counter":7}}
```

`-1` means cancelled (`get_SelectedNumber() ?? -1`, `core_d.cs:70848`).
`server.py:on_GameCustomChoice` already handles this exact shape — the go-first
prompt uses it.

`PiePromptListener.SetCustomChoiceButtonFrameScale` lays the buttons out in
`buttonColumns` (3) columns. Ready-made button keys:
`playmat.prompt.generic.yes` / `.no` / `.cancel`, or `common.dialog.yes` /
`.no` / `.ok` / `.done`.

`CustomChoiceWithTargetsRequired` (`core_d.cs:73725`) is the richer form:
`forced`, `ignoreFirst`, `choiceKind`, `choices` (`List<SerializedEntity>` —
i.e. arbitrary cards to show as buttons), `choiceTargets`
(`List<TargetInformation[]>`, one entry per choice), `optimalChoices`
(`List<TargetPreference>`), `sourceEntity`, `selectionParams`. Reply is
`CustomChoiceWithTargets` (`core_d.cs:74046`):
`{gameID, counter, selection: [int, TargetResponse[]]}`.

`CustomChoiceOfferMessage` (`pie_d.cs:65605`) is a **different, non-selection**
message (`selectingPlayer`, `prompt`, `buttons` (`LocalizableText[]`),
`offerLength`, `sourceEntity`, `selection` (int?), `correctChoice` (int?)),
consumed by `D.x` (`pie_d.cs:30354`) — it *shows* a choice the opponent made, it
does not ask for one.

### 4.3 Choosing cards from a zone (deck search, discard retrieval, …)

Use `SelectionWithTargetsRequired` with `forced: true, ignoreFirst: true`, one
`targetMap` key, and a `TargetInformation` whose `name` picks the presentation:

| `name` | class | presentation |
|---|---|---|
| `EntityListTargetInformation` | `core_d.cs:16633` | plain click-to-pick on the board |
| `RevealEntityListTargetInformation` | `core_d.cs:10939` | adds `revealEntities` (`Dictionary<EntityID, ReadOnlyAttributes>`) — **this is the deck-search dialog**: it hands the client the face-up contents of a hidden zone so it can render cards it was never told about |
| `RevealDetailedEntityListTargetInformation` | `core_d.cs:26405` | same, richer renderer |
| `CompositeRevealEntityListTargetInformation` / `Or…` / `And…` / `Any…` / `ExclusiveMulti…` | `pie_d.cs:195690`–`195722` | multi-group reveal pickers ("choose 1 Pokémon **and** 1 Item") |
| `MultiSelectEntityListTargetInformation` | `pie_d.cs:193737` | multi-select; its own `MultiSelectEntityListTargetResponse` (`pie_d.cs:193888`) |
| `AlignedEntityListTargetInformation` | `pie_d.cs:197325` | ordered / aligned layout |
| `SlotAssociatedEntityListTargetInformation` | `pie_d.cs:195971` | pick into slots; `SlotAssociatedEntityListTargetResponse` (`pie_d.cs:197217`) |
| `RevealAssociatedEntityListTargetInformation` | `pie_d.cs:15113` | `RevealAssociatedEntityListTargetResponse` (`pie_d.cs:197190`) |
| `CompositeRevealAssociatedEntityListTargetInformation` | `pie_d.cs:195646` | |
| `PrizeCardTargetInformation` | `pie_d.cs:200158` | prize pile; adds `presentPrizesAllowed` (bool) and `horizontalLayout` (bool) |
| `RetreatCostEntityListTargetInformation` | `pie_d.cs:147746` | energy pip tray |
| `EnergyCostEntityListTargetInformation` | `pie_d.cs:198855` | energy pip tray |
| `KnockoutPokemonTargetInformation` | `pie_d.cs:200152` | "choose your new Active after a KO" |
| `RetreatNewActiveTargetInformation` | `pie_d.cs:200146` | "choose your new Active on retreat" |
| `OrEntityListTargetInformation` | `pie_d.cs:199471` | |
| `CompositeEntityListTargetInformation` | `pie_d.cs:15121` | |
| `CustomChoiceTargetInformation` | `core_d.cs:16661` | `sortType`, `choices` (`LocalizableTextVariables[]`), `titles` (`LocalizableText[]`) — a button list nested inside a target chain |
| `OrientationCustomChoiceTargetInformation` | `pie_d.cs:200166` | Stadium orientation |
| `CakeAttackCustomChoiceTargetInformation` | `pie_d.cs:121230` | attack-effect choice |
| `CustomChoiceAsAbilitySelectTargetInformation` (+ `…WithTAGBonus`) | `pie_d.cs:200169` / `200172` | |
| `XTargetInformation` | `core_d.cs:16650` | `forced`, `min`, `max`; replies `IntTargetResponse` — but see the `GetXCostChoiceRequired` caveat above; as a *nested* target info it is fine |

**How is the client told which zone, and how many?** It is not told a zone at
all — `validTargets` is a flat list of entity ids and the client derives the
zone from each entity's parent. "How many" is `numberToSelect` /
`minimumToSelect` (§2.1). For cards in a zone the player cannot see, either
introduce them first (`EntityIntroduced`) or use
`RevealEntityListTargetInformation.revealEntities`, which carries the
attributes inline.

The reply is always
`EntityListTargetResponse{entityList: […], name:"EntityListTargetResponse"}`
unless the row above names a different response type.

Selection-count UI keys: `playmat.selection.prompt.selectionsvalue` ("Number of
cards you may select:"), `.selectionsvalueforced` ("…you must select:"),
`.selectionsmade`.

### 4.4 Targeting a benched Pokémon (yours or the opponent's)

Nothing special is required — a plain `EntityListTargetInformation` whose
`validTargets` are the bench entity ids. The client works out which hint area
to light from the available selections' parent zone (`pie_d.cs:139051`,
`139061` test `NameData == "bench"` / `"active"`).

Prompts already in the DB: `playmat.prompt.selectanenemybenchedpokemon`,
`.choosebenchedpokemontodamage`, `.chooseselfbenchedpokemontodamage`,
`.selectabenchedpokemon`, `.select2enemybenchedpokemon`, `.opponentpull`,
`.opponentpush`, `.megacatcher`, plus `.opponentbenchfull` / `.benchfull` /
`.fullbench` for the failure cases.

### 4.5 Revealing cards to both players

Three mechanisms, in increasing weight:

1. **`RevealCardToAllEffect`** (`pie_d.cs:65656`) — one card: `entityID`
   (a bare `Guid`), `Return` (fly back afterwards), `alwaysReveal`. The card
   flies to `multiPresentArea`, plays the `playmat.card.reveal` sound, waits
   ~0.5 s (0.25 s for energy) or until a click, then optionally returns.
   Consumer `m.u` (`pie_d.cs:144600`). It **skips** the animation for your own
   cards already visible in play unless `alwaysReveal` is set.
2. **`RevealCardsToAllEffect`** / **`RevealCardsToPlayerEffect`** — a modal
   reveal dialog. `RevealCardsToAllEffect.playerPrompt` is a
   `Dictionary<AccountID, LocalizableText>`, so each player can be told
   something different (and a player absent from the map sees nothing).
   `m.s.execute()` **blocks the queue** while `model.c` is set, and `model.c` is
   cleared by **`RevealClosed{revealID}`** (`core_d.cs:11644` → `m.v`,
   `pie_d.cs:144688`). *(VERIFIED that `RevealClosed` clears it; UNKNOWN whether
   the dialog's own Done button also clears it — I did not read
   `simpleRevealAreaPrefab`'s script. Send `RevealClosed` to be safe.)*
3. **`CakeRevealOpened`** (`pie_d.cs:123025`) — the full reveal/selection
   dialog: `revealID` (Guid), `entityIDs` (`GuidDictionary<ReadOnlyAttributes>`
   — a JSON object keyed by guid), `revealSource`, `revealTypes` (string, one of
   the constants `"Highlight"` / `"Selection"` / `"Ordered"`,
   `pie_d.cs:123043`), `revealTitle`. Consumer `L.W` (`pie_d.cs:137062`) also
   sets `suppressedEntities`, so the revealed cards vanish from their normal
   zone while the dialog is up. The plain `RevealOpened` (`core_d.cs:70550`)
   has **no consumer** — always use the Cake one.

### 4.6 Action offers (`SelectionWithTargetsAndActionsRequired`) — the contract

`core_d.cs:73965`. `SelectionMessage` header plus:

| field | type |
|---|---|
| `targetMap` | `TargetsAndActions[]`, where `TargetsAndActions = {entityID, selectableAction, targetInfoLst}` (`core_d.cs:73969`) |
| `optimalPlayMap` | `Tuple<EntityID, TargetPreference>[]` |
| `forced` | `bool` |
| `targetType` | `string` (root Kind) |
| `selectionParams` | `Dictionary<string,object>` |

`SelectableAction` (`core_d.cs:11055`): `gameID`, `actionID` (`AbilityID`),
`description` (string), `selectionType` (string), `actionHint`
(`TargetPreference`). **`actionHint` must never be `Unselectable`** —
`PreferenceToStrength` throws `NotImplementedException` on it
(`core_d.cs:73914`). Valid values: `Optimal`, `Suboptimal`, `ForcedAttacker`,
`Depleted`, `Super`, `Negative`.

`ActionsNode` (`core_d.cs:72521`) groups rows by entity and **logs an error if
one entity's rows carry more than one distinct `selectionType`** — confirming
CLAUDE.md. The `selectionType` becomes the node's `Kind` and picks the UI
command from `pie_d.cs:155735`: `"Ability"`, `"AbilitySelection"`,
`"OutOfPlay"`. Rows whose `TargetInformation` has `selected: false` are not
turned into nodes at all but their `validTargets` are still copied into
`predictedEntityTargetMap` for hinting (`core_d.cs:72549`).

Reply (`Outgoing.SelectionWithTargetsAndActions`, `core_d.cs:73769`):

```json
{"name":"SelectionWithTargetsAndActions","value":{
  "gameID":"…", "counter":7,
  "selection": [ ["<entityID>","<abilityID>"], [ <targetResponses…> ] ]}}
```

`selection` is `null` when the player passes. `match.decode_reply` already
reads `selection[0][0]` / `selection[0][1]`, which matches exactly.

### 4.7 Other messages the client can send unprompted

- `PlayerInteractedWithEntity` (`core_d.cs:74141`) — `gameID`, `playerID`,
  `entityID`, `flavor`. Emitted when the player fiddles with a card; the server
  is expected to mirror it to the other side as
  `PlayerInteractedWithEntityEffect` / `…V2` / `PlayerStoppedInteractingEffect`
  (`core_d.cs:74172`–`74213`). None of those three has a
  `[MessageCommandConstructor]` in this build, so ignoring the whole feature is
  safe.
- `LogClientError` (`core_d.cs:67725`) — the client reporting its own crash;
  already the project's best debugging tool.

---

## 5. Win / loss / end of game

### 5.1 What actually ends the game

`GameEnded` (`core_d.cs:70446`: `winnerList` (`AccountID[]`), `loserMap`
(`Dictionary<AccountID,string>`), `draw` (bool)) has **no
`[MessageCommandConstructor]`**. Its only listener is
`sessionProvider.AddListener<GameEnded>(handleGameEnded)` at `core_d.cs:74545`
in a matchmaking class. CLAUDE.md is right that it is inert here; harmless to
send.

`GameCompletedMessage` (`pie_d.cs:122686`) is the whole mechanism:

| field | type | consumed? |
|---|---|---|
| `coins` | int | no load site found |
| `exp` | int | no load site found |
| `share` | bool | no load site found |
| `endOfGameText` | `LocalizableText` | no load site found |
| `rewardList` | `A.N[]` | **iterated unguarded** at `pie_d.cs:14152`, `64383`, `64478` — never null; `[]` is fine |
| `winner` | string (accountID) | |
| `loser` | string (accountID) | |
| `additionalParameters` | `Dictionary<string, LocalizableText>` | **everything the dialog shows** |

`Sequences.ConsumeQueuedMessages` (`pie_d.cs:147818`) tests
`nextMessage is GameCompletedMessage` **before** `is SequenceMessage` and sets
`gameEnded = true`, terminating both consumer coroutines. So it must arrive
**bare** — exactly as CLAUDE.md says.

### 5.2 `additionalParameters` keys the client reads

The one **unguarded** read is `EOGAnimationController_SummaryDialog.Init`
(`pie_d.cs:14018`):

```csharp
animator.SetInteger("PlayerVictory", (message.additionalParameters["GameResult"] == "Win") ? 1 : 2);
```

A `KeyNotFoundException` here throws inside the animation coroutine, which is
why an empty `additionalParameters` looked like "concede does nothing". Two
more unguarded `["GameResult"]` reads at `pie_d.cs:114250` and `115241`
(reward-tab art), and one at `pie_d.cs:64369` (spin wheel).

Everything else is guarded. Complete list of keys read (VERIFIED — grep of
`additionalParameters` / `AdditionalParameters` in `pie_d.cs`):

| key | read at | meaning |
|---|---|---|
| `GameResult` | `14018`, `64306`, `64369`, `114250`, `115241` | **compared only against `"Win"`**; anything else reads as a loss |
| `playmat.endgame.stat.gameresult` | `64303` | the result line — feed it one of the `playmat.endgame.wincondition.*` keys |
| `GameDuration` | `14079` | **`double.Parse`, culture-sensitive — send an integer string of milliseconds** |
| `aiName` | `14108` | opponent name in single-player |
| `me_$playmat.endgame.stat.mvp.archetypeid$`, `opp_$…$` | `14113`, `14121` | MVP card art (the `$`s are stripped by `LocalizableText`) |
| `Headsflipped`, `Damagedealt` | `14137`, `14142` | |
| `OldTCStars`, `NewTCStars` | `14146`, `14150` | **`int.Parse`** — Trainer Challenge only |
| `gameExtrasDeck_<accountID>` | `14167` | a JSON-serialized deck, `JSON.Deserialize<A.n>` |
| `me_playmat.endgame.stat.<X>`, `opp_playmat.endgame.stat.<X>` | `13446`–`13463`, `114966`–`114978` | the Summary and Stats tabs |
| `TC<Name>Bonus`, `TCDifficultyMultiplier` | `114860`–`114875` | Trainer Challenge score tab |
| `TimeTaken` | `64411` | analytics only |

The stats tabs read exactly these `<X>`: `timetaken`, `damagedealt`,
`damagehealed`, `biggestattack`, `energyplayed`, `trainersplayed`,
`prizecardstaken`, `cardsdrawn`, `mvp`, `headsflipped`, `tailsflipped`. Each
row's label is the key with the `me_` / `opp_` prefix stripped and then
localized (`playerKey.Split('_')[1].LocalizeIfNeeded()`, `pie_d.cs:114985`) —
so the `playmat.endgame.stat.*` keys double as both parameter name and label.

**Result-line values.** `playmat.endgame.stat.gameresult` should be one of the
22 `playmat.endgame.wincondition.*` keys (§6). They come in `player.` /
`opponent.` pairs — `player.` is worded as "you won", `opponent.` as "you lost",
so pick by perspective, not by who won:

```
.player.resigned      Your opponent conceded the game.
.opponent.resigned    You conceded the game.
.player.outofpokemon  You Knocked Out your opponent's last Pokémon in play to win the game!
.player.opponentscore You took all of your Prize cards to win the game!
.player.outofcards    Your opponent was unable to draw a card at the beginning of the turn.
.player.bounceactive  Your opponent lost the game for having no more Pokémon in play.
```

plus `.idletimeout`, `.outoftime`, `.timedout`, `.specialcard`,
`.walkoffhomer`, and the unpaired `playerkoprize` / `opponentkoprize`.
`queueGameEndResultAnimation` (`pie_d.cs:64453`) also switches to a special
"Unown" victory animation when a `victoryReasonKey` parameter matches a
specific Unown key — not worth reproducing.

### 5.3 Prizes and knockouts

- **Taking a prize** is an ordinary selection whose target info is named
  `PrizeCardTargetInformation` (`pie_d.cs:200158`; adds
  `presentPrizesAllowed` and `horizontalLayout`) → command `r.B`
  (`pie_d.cs:199630`), which hides the prize-count badge and presents the pile.
  The card then moves on the server's `EntityMoved` inside a `DrawPrizeCard`
  sequence (`pie_d.cs:142745`). `SelectionUtils.SelectingOpponentCards()`
  decides which pile is presented. Prompt:
  `com.direwolfdigital.cake.rules.actions.cake.DrawPrize.Prompt1`
  ("Choose your Prize card.") — which is in `suppressedKeys`, so no banner.
  The prize-count badges are turned on by `DealInitialPrizeCards` and toggled
  by `r.B`.
- **Knockout**: `CakeAttackEffect` already animates the KO (§3.7); the card is
  actually moved by `EntityMoved` inside a `Knockout` sequence
  (`pie_d.cs:147946`), which picks `FromActive`/`ToKnockOut` versus
  `FromBench`/`ToBenchKnockOut` for the first child whose entity is a Pokémon
  leaving play. The pile is flushed on the next `ActivePlayerSet`.
- **Promoting after a KO**: send a `SelectionWithTargetsRequired` whose target
  info is named `KnockoutPokemonTargetInformation`. `IfKnockOut`
  (`pie_d.cs:138569`) and the drag handler (`pie_d.cs:65802`) both key off that
  exact string. `RetreatNewActiveTargetInformation` is the retreat equivalent.

---

## 6. Localization

`LocalizationLookup.Localize` (`core_d.cs:76565`) lowercases the key
(`formatKey`, `core_d.cs:76547`) and **returns the key verbatim on a miss** —
silent, as CLAUDE.md says. `LocalizableText.HasId` compares
`OrdinalIgnoreCase` after `Trim('$')`. So case does not matter when sending,
but the stored keys are all lowercase, and `SetPairs` (`core_d.cs:76605`)
inserts keys **verbatim** — so a release served to the client must use
lowercase keys or nothing will ever match.

Source: `StreamingAssets/LocalizationDB-UTF8.db`, table `Lookup(key, value)`,
27,550 rows.

Namespace sizes:

```
20654  com.direwolfdigital.*      (14418 of them rules.abilities.* = per-card text)
  572  playmat.prompt.*
  174  specialvisualizations.*
   82  playmat.endgame.*
   51  pregame.ui.*
   29  gamelog.message.*
```

The per-card ability text lives at
`com.direwolfdigital.cake.rules.abilities.attacks.<set>.<attackname>.title` /
`.name` / `.gametext` — useful when you need an attack's printed name for
`CakeAttackEffect.attackName`.

**`playmat.prompt.yourturn` does not exist.** Use
`playmat.turnindicator.yourturn`, and note the banner is client-driven anyway
(§3.8).

Tables follow. Card-specific `playmat.prompt.<set>_<num>.*` keys are omitted
(there are ~180 of them); query `loc.db` directly if you need one.


#### `com.direwolfdigital.cake.rules.*` - the SERVER's own prompt vocabulary  (12 keys)

| key | English |
|---|---|
| `com.direwolfdigital.cake.rules.states.actionphase.selectaction` | Pick an Action |
| `com.direwolfdigital.cake.rules.states.startgame.coinflip` | Coin Flip |
| `com.direwolfdigital.cake.rules.states.startgame.heads` | Heads |
| `com.direwolfdigital.cake.rules.states.startgame.mulliganchoicechoice1` | Yes |
| `com.direwolfdigital.cake.rules.states.startgame.mulliganchoicechoice2` | No |
| `com.direwolfdigital.cake.rules.states.startgame.mulligancustomchoice` | Your opponent had no Basic Pokémon and had to draw a new hand. Would you like to draw a card? |
| `com.direwolfdigital.cake.rules.states.startgame.mulligancustomchoice.drawmultiple` | Your opponent had no Basic Pokémon and had to draw a new hand. Would you like to draw a card for mulligan {0}? |
| `com.direwolfdigital.cake.rules.states.startgame.opponentselectstartingbenchedpokemon` | Please wait while your opponent chooses Benched Pokémon. |
| `com.direwolfdigital.cake.rules.states.startgame.opponentselectstartingpokemon` | Please wait while your opponent chooses an Active Pokémon. |
| `com.direwolfdigital.cake.rules.states.startgame.selectstartingpokemon` | Choose a Pokémon to be your Active Pokémon. |
| `com.direwolfdigital.cake.rules.states.startgame.selectstartingpokemon.title` | Starting Pokémon |
| `com.direwolfdigital.cake.rules.states.startgame.tails` | Tails |

#### `com.direwolfdigital.cake.rules.actions.*`  (11 keys)

| key | English |
|---|---|
| `com.direwolfdigital.cake.rules.actions.cake.conversionpowder.prompt1` | Asleep |
| `com.direwolfdigital.cake.rules.actions.cake.conversionpowder.prompt2` | Poisoned |
| `com.direwolfdigital.cake.rules.actions.cake.drawprize.prompt1` | Choose your Prize card. |
| `com.direwolfdigital.cake.rules.actions.cake.retreat.prompt1` | Select a Pokémon to become the new Active Pokémon. |
| `com.direwolfdigital.cake.rules.actions.cake.selectattack.prompt1` | Select an attack. |
| `com.direwolfdigital.cake.rules.actions.generic.damage.prompt1` | Choose a Pokémon to attack. |
| `com.direwolfdigital.cake.rules.actions.generic.discard.prompt1` | Choose a card to discard. |
| `com.direwolfdigital.cake.rules.actions.generic.discard.prompt2` | Choose two cards to discard. |
| `com.direwolfdigital.cake.rules.actions.generic.discard.prompt3` | Choose three cards to discard. |
| `com.direwolfdigital.cake.rules.actions.generic.returntoownersdeck.prompt1` | Choose 2 cards to shuffle into your deck. |
| `com.direwolfdigital.cake.rules.actions.generic.returntoownershand.prompt1` | Return to owner's hand. |

#### `com.direwolfdigital.cake.rules.entities.*`  (3 keys)

| key | English |
|---|---|
| `com.direwolfdigital.cake.rules.entities.pokemon.opponentvalidretreats` | Choose a Pokémon to make Active. |
| `com.direwolfdigital.cake.rules.entities.pokemon.optionalvalidretreats` | You may choose a Pokémon to be your Active Pokémon or click OK to continue. |
| `com.direwolfdigital.cake.rules.entities.pokemon.validretreats` | Choose a Pokémon to be your Active Pokémon. |

#### `playmat.gamestart.*`  (18 keys)

| key | English |
|---|---|
| `playmat.gamestart.opponentgoesfirst` | {0} goes first! |
| `playmat.gamestart.opponentpromptbenchpokemon` | Your opponent is selecting their starting Pokémon. |
| `playmat.gamestart.playergoesfirst` | {0} goes first! |
| `playmat.gamestart.prompt.bothnopokemon` | You and your opponent have no Basic Pokémon and must each draw a new hand. Look at the cards and click OK. |
| `playmat.gamestart.prompt.coinflipchoice` | Choose heads or tails to see who goes first. |
| `playmat.gamestart.prompt.coinflipchoicetitle` | Beginning of Game |
| `playmat.gamestart.prompt.novalidbenchselections` | You have no additional Basic Pokémon to start on your Bench. Select Done to continue. |
| `playmat.gamestart.prompt.oppmulligan` | Your opponent had no Basic Pokémon and will take a mulligan after you set up to play. |
| `playmat.gamestart.prompt.opponentnopokemon` | Your opponent has no Basic Pokémon and must draw a new hand. Look at their hand and select Done. |
| `playmat.gamestart.prompt.opponentnopokemon.title` | Opponent's Hand |
| `playmat.gamestart.prompt.playermulligandone` | Your opponent has finished setting up to play. Select Done to take a mulligan. |
| `playmat.gamestart.prompt.playermulliganwait` | You have no Basic Pokémon. You'll take a mulligan once your opponent finishes setting up to play. |
| `playmat.gamestart.prompt.playernopokemon` | You have no Basic Pokémon. Click OK to draw a new hand. |
| `playmat.gamestart.prompt.playernopokemon.playmat2` | Your opening hand has no Basic Pokémon. Select Done to take a mulligan. |
| `playmat.gamestart.prompt.selectgofirst` | Would you like to go first? If you choose No, your opponent will go first. |
| `playmat.gamestart.promptbenchpokemon` | Choose the Basic Pokémon you wish to start on the Bench and click OK. |
| `playmat.gamestart.promptbenchpokemon.new` | Choose Pokémon to start on your Bench. |
| `playmat.gamestart.promptbenchpokemon.title` | Starting Benched Pokémon |

#### `playmat.turnindicator.*`  (2 keys)

| key | English |
|---|---|
| `playmat.turnindicator.opponentturn` | OPPONENT'S TURN |
| `playmat.turnindicator.yourturn` | YOUR TURN |

#### `playmat.controls.*`  (10 keys)

| key | English |
|---|---|
| `playmat.controls.cancelbutton` | Cancel |
| `playmat.controls.coinflipresults` | Heads {0}, Tails {1} |
| `playmat.controls.coinresults` | Results |
| `playmat.controls.continuebutton` | Continue |
| `playmat.controls.endturnbutton` | End Turn |
| `playmat.controls.exitbutton` | Quit |
| `playmat.controls.showcardsbutton` | Show Cards |
| `playmat.controls.showplaymatbutton` | Show Playmat |
| `playmat.controls.showselectionsbutton` | Show Selections |
| `playmat.controls.verifyquitmessage` | Are you sure you want to concede the game? |

#### `playmat.actionpanel.*` / `playmat.actions.*`  (42 keys)

| key | English |
|---|---|
| `playmat.action.defaultenergyplayability.actionprompt` | Select an Energy card from your hand to attach to a Pokémon in play. |
| `playmat.action.defaultpokemonplayability.actionprompt` | Select a Pokémon from your hand to put onto your Bench. |
| `playmat.action.defaultstadiumplayability.actionprompt` | Select a Stadium card from your hand to play it. |
| `playmat.action.evolvepokemonplayability.actionprompt` | Select an Evolution card from your hand to evolve your Active Pokémon. |
| `playmat.action.usepokemonattack.actionprompt` | Choose an attack to use from your Active Pokémon. |
| `playmat.action.usestadiumcard.actionprompt` | Choose the effect to use from the active Stadium card. |
| `playmat.action.usetrainercard.actionprompt` | Select a Trainer card to play from your hand. |
| `playmat.actionpanel.buttons.attachenergy.text` | Attach Energy |
| `playmat.actionpanel.buttons.attack.text` | Attack |
| `playmat.actionpanel.buttons.cancel.text` | Cancel |
| `playmat.actionpanel.buttons.continue.text` | Continue |
| `playmat.actionpanel.buttons.evolve.text` | Evolve Pokémon |
| `playmat.actionpanel.buttons.evolve.tooltip` | Evolve Pokémon (as many as you want) |
| `playmat.actionpanel.buttons.playstadium.text` | Play a Stadium Card |
| `playmat.actionpanel.buttons.playtobench.text` | Play Basic Pokémon |
| `playmat.actionpanel.buttons.playtobench.tooltip` | Put Basic Pokémon onto your Bench (up to 5 total) |
| `playmat.actionpanel.buttons.playtrainer.text` | Play Trainer Cards |
| `playmat.actionpanel.buttons.pokeability.text` | Use Abilities |
| `playmat.actionpanel.buttons.retreat.text` | Retreat |
| `playmat.actionpanel.buttons.showplaymat.text` | Show Playmat |
| `playmat.actionpanel.buttons.undo.text` | Undo Last Action |
| `playmat.actionpanel.buttons.undo.tooltip` | Undo the last action (if able) |
| `playmat.actionpanel.buttons.usestadium.text` | Activate Stadium |
| `playmat.actionpanels.buttons.lookatplaymat.text1` | Look at Playmat |
| `playmat.actionpanels.buttons.lookatplaymat.text2` | Look at Cards |
| `playmat.actions.button.activate` | Activate |
| `playmat.actions.button.activatetrainer` | Activate Trainer |
| `playmat.actions.button.moveenergy` | Move Energy |
| `playmat.actions.button.movetobench` | Move to Bench |
| `playmat.actions.button.passiveability` | Use Passive Ability |
| `playmat.actions.button.pokepower` | Use Poké-Power |
| `playmat.actions.tooltip.attachenergy` | Attach 1 Energy card to 1 of your Pokémon (only once per turn) |
| `playmat.actions.tooltip.attack` | Attack with your Active Pokémon (Attacking will end your turn) |
| `playmat.actions.tooltip.currentactions` | Actions that can currently be taken |
| `playmat.actions.tooltip.endturn` | End your turn without attacking |
| `playmat.actions.tooltip.openclosechat` | Opens/Closes the chat window and event log |
| `playmat.actions.tooltip.pokepower` | Use Poké-Power (as many as you want) |
| `playmat.actions.tooltip.retreat` | Retreat your Active Pokémon (once per turn). The Retreat Cost will send Energy cards to the discard pile. |
| `playmat.actions.tooltip.showallactions` | Toggle showing all actions or only currently available actions |
| `playmat.actions.tooltip.showhideactions` | Click to show/hide turn actions |
| `playmat.actions.tooltip.stadium` | Activate the effect of the Stadium card in play |
| `playmat.actions.tooltip.trainer` | You may play Item cards (as many as you want) and play Supporter and Stadium cards (only one of each) |

#### `playmat.selection*`  (7 keys)

| key | English |
|---|---|
| `playmat.selection.prompt.selectionsmade` | Number of selections made |
| `playmat.selection.prompt.selectionsvalue` | Number of cards you may select: |
| `playmat.selection.prompt.selectionsvalueforced` | Number of cards you must select: |
| `playmat.selectiondialog.droptargetrenderer.toplabel` | (Top) |
| `playmat.selectiondialog.hidedialogbutton.tooltip` | Show Playmat |
| `playmat.selectiondialog.showallcardscheckbox.label` | Show All Cards |
| `playmat.selectionreveal.slot.label` | Pokémon |

#### `common.dialog.*`  (19 keys)

| key | English |
|---|---|
| `common.dialog.accept` | Accept |
| `common.dialog.back` | Back |
| `common.dialog.button.label.updatenow` | Update Now |
| `common.dialog.cancel` | Cancel |
| `common.dialog.decline` | Decline |
| `common.dialog.default` | Reset Defaults |
| `common.dialog.done` | Done |
| `common.dialog.error` | Error! |
| `common.dialog.exit` | Exit |
| `common.dialog.no` | No |
| `common.dialog.ok` | OK |
| `common.dialog.retry` | Retry |
| `common.dialog.save` | Save |
| `common.dialog.savechanges` | Save Changes |
| `common.dialog.success` | Success! |
| `common.dialog.upgrade` | Upgrade |
| `common.dialog.wait` | Please Wait |
| `common.dialog.warning` | Warning! |
| `common.dialog.yes` | Yes |

#### `playmat.endgame.wincondition.*`  (22 keys)

| key | English |
|---|---|
| `playmat.endgame.wincondition.opponent.bounceactive` | You lost the game for having no more Pokémon in play. |
| `playmat.endgame.wincondition.opponent.idletimeout` | You were inactive for too long. |
| `playmat.endgame.wincondition.opponent.opponentscore` | Your opponent took all of their Prize cards to win the game! |
| `playmat.endgame.wincondition.opponent.outofcards` | You were unable to draw a card at the beginning of the turn. |
| `playmat.endgame.wincondition.opponent.outofpokemon` | Your opponent Knocked Out your last Pokémon in play to win the game! |
| `playmat.endgame.wincondition.opponent.outoftime` | You ran out of time. |
| `playmat.endgame.wincondition.opponent.resigned` | You conceded the game. |
| `playmat.endgame.wincondition.opponent.specialcard` | Your opponent used a special card to win the game. |
| `playmat.endgame.wincondition.opponent.timedout` | You ran out of time. |
| `playmat.endgame.wincondition.opponent.walkoffhomer` | Your opponent used Walk-Off Homer to win the game. |
| `playmat.endgame.wincondition.opponentkoprize` | Your opponent Knocked Out all of your Pokémon in play and took all of their Prize card to win the game! |
| `playmat.endgame.wincondition.player.bounceactive` | Your opponent lost the game for having no more Pokémon in play. |
| `playmat.endgame.wincondition.player.idletimeout` | Your opponent was inactive for too long. |
| `playmat.endgame.wincondition.player.opponentscore` | You took all of your Prize cards to win the game! |
| `playmat.endgame.wincondition.player.outofcards` | Your opponent was unable to draw a card at the beginning of the turn. |
| `playmat.endgame.wincondition.player.outofpokemon` | You Knocked Out your opponent's last Pokémon in play to win the game! |
| `playmat.endgame.wincondition.player.outoftime` | Your opponent ran out of time. |
| `playmat.endgame.wincondition.player.resigned` | Your opponent conceded the game. |
| `playmat.endgame.wincondition.player.specialcard` | You used a special card to win the game. |
| `playmat.endgame.wincondition.player.timedout` | Your opponent has run out of time. |
| `playmat.endgame.wincondition.player.walkoffhomer` | You used Walk-Off Homer to win the game. |
| `playmat.endgame.wincondition.playerkoprize` | You Knocked Out all of your opponent's Pokémon in play and took all your Prize cards to win the game! |

#### `playmat.endgame.stat.*` / `playmat.endgame.score.*`  (38 keys)

| key | English |
|---|---|
| `playmat.endgame.stat.biggestattack` | Biggest Attack |
| `playmat.endgame.stat.biggestattackvalue` | {0} |
| `playmat.endgame.stat.cardsdrawn` | Cards Drawn |
| `playmat.endgame.stat.cardsdrawnvalue` | {0} |
| `playmat.endgame.stat.cardstats` | Card Stats |
| `playmat.endgame.stat.coinflips` | Coin Flips |
| `playmat.endgame.stat.damagedealt` | Damage Dealt |
| `playmat.endgame.stat.damagedealtvalue` | {0} |
| `playmat.endgame.stat.damagehealed` | Damage Healed |
| `playmat.endgame.stat.damagehealedvalue` | {0} |
| `playmat.endgame.stat.damagestats` | Damage Stats |
| `playmat.endgame.stat.energyplayed` | Energy Attached |
| `playmat.endgame.stat.energyplayedstat` | {0} |
| `playmat.endgame.stat.gameresult` | Game Result |
| `playmat.endgame.stat.headsflipped` | Heads Flipped |
| `playmat.endgame.stat.headsflippedvalue` | {0} |
| `playmat.endgame.stat.lose.text` | You Lost the Match |
| `playmat.endgame.stat.minute` | minute |
| `playmat.endgame.stat.minutes` | minutes |
| `playmat.endgame.stat.mvp` | MVP |
| `playmat.endgame.stat.mvp.description` | Player's MVP |
| `playmat.endgame.stat.mvp.pokemonname` | {0} |
| `playmat.endgame.stat.nomvp` | None |
| `playmat.endgame.stat.opponentstats` | Opponent's Stats |
| `playmat.endgame.stat.prizecardsremaining` | Prize Cards Remaining |
| `playmat.endgame.stat.prizecardsremainingvalue` | {0} |
| `playmat.endgame.stat.prizecardstaken` | Prize Cards Taken |
| `playmat.endgame.stat.prizecardstakenvalue` | {0} |
| `playmat.endgame.stat.tailsflipped` | Tails Flipped |
| `playmat.endgame.stat.tailsflippedvalue` | {0} |
| `playmat.endgame.stat.timetaken` | Total Game Time |
| `playmat.endgame.stat.timetakennumber` | {0} {1} |
| `playmat.endgame.stat.title` | Game Completed |
| `playmat.endgame.stat.totalturnstaken` | Total Turns Taken |
| `playmat.endgame.stat.trainersplayed` | Trainers Played |
| `playmat.endgame.stat.trainersplayedvalue` | {0} |
| `playmat.endgame.stat.win.text` | You Won the Match! |
| `playmat.endgame.stat.yourstats` | Your Stats |

#### `playmat.endgamedialog.*` / `playmat.gamecompleted.*`  (15 keys)

| key | English |
|---|---|
| `playmat.endgamedialog.rewards.collectionitemearned.label` | Collection Item Earned! |
| `playmat.endgamedialog.rewards.deckupdated.label` | Deck Updated! |
| `playmat.endgamedialog.rewards.prizewheelrewardsearned.label` | Rewards Earned! |
| `playmat.endgamedialog.rewards.tokensearned.label` | Tokens Earned! |
| `playmat.endgamedialog.rewards.totaltokensearned.label` | Total Tokens Earned: |
| `playmat.endgamedialog.score.descriptionheader.label` | Description |
| `playmat.endgamedialog.score.scoreheader.label` | Score |
| `playmat.endgamedialog.statistics.opponentheader.label` | Opponent |
| `playmat.endgamedialog.statistics.playerheader.label` | You |
| `playmat.endgamedialog.statistics.statheader.label` | Stat |
| `playmat.endgamedialog.summary.deckupdatedformat` | {0} updated |
| `playmat.endgamedialog.summary.defaultupdatedeckname` | deck |
| `playmat.endgamedialog.summary.descriptionheader.label` | Description |
| `playmat.endgamedialog.summary.statsheader.label` | Stats |
| `playmat.endgamedialog.title.label` | Match Results |

#### `playmat.gamecompleted.*`  (6 keys)

| key | English |
|---|---|
| `playmat.gamecompleted.donebutton.label` | Done |
| `playmat.gamecompleted.tabs.gamelog` | Game Log |
| `playmat.gamecompleted.tabs.rewards` | Rewards |
| `playmat.gamecompleted.tabs.stats` | Stats |
| `playmat.gamecompleted.tabs.summary` | Summary |
| `playmat.gamecompleted.tabs.tcscore` | Score |

#### `playmat.mulligan.*`  (8 keys)

| key | English |
|---|---|
| `playmat.mulligan.dialog.body.opponent` | Your opponent had to take {0} mulligan(s) before they drew a Pokémon they could play in their opening hand. |
| `playmat.mulligan.dialog.body.player` | You had to take {0} mulligan(s) before you drew a Pokémon you could play in your opening hand. |
| `playmat.mulligan.dialog.carousel.header` | Mulligan {0} |
| `playmat.mulligan.dialog.header` | Opening Hand Mulligans |
| `playmat.mulligan.drawcards.drawallbutton` | Yes to rest ({0}) |
| `playmat.mulligan.drawcards.drawnonebutton` | No to rest ({0}) |
| `playmat.mulligan.galewings.body` | Your opening hand has a valid starting Pokémon, so you may keep it or take a mulligan. Select Done to continue. |
| `playmat.mulligan.option.body` | Your opening hand has no Basic Pokémon. Select Done to take a mulligan. |

#### `playmat.cardattribute.*`  (4 keys)

| key | English |
|---|---|
| `playmat.cardattribute.damagedecrease` | Damage Decreased |
| `playmat.cardattribute.damageincrease` | Damage Increased |
| `playmat.cardattribute.resistance` | Resistance |
| `playmat.cardattribute.weakness` | Weakness |

#### `playmat.gametip.*`  (26 keys)

| key | English |
|---|---|
| `playmat.gametip.asleep.body` | While Asleep, a Pokémon cannot attack or retreat. Between turns, its owner flips a coin. If heads, the Pokémon wakes up. |
| `playmat.gametip.asleep.header` | Asleep |
| `playmat.gametip.burned.body` | If your Pokémon is Burned, put a Burn marker on it. Between turns, flip a coin. If tails, put 2 damage counters on the Burned Pokémon. |
| `playmat.gametip.burned.header` | Burned |
| `playmat.gametip.burnedsm.body` | If your Pokémon is Burned, put a Burn marker on it. Between turns, put 2 damage counters on the Burned Pokémon. Then, that Pokémon's owner flips a coin. If heads, the Pokémon returns to normal. |
| `playmat.gametip.confused.body` | If you attack with a Confused Pokémon, flip a coin. If tails, the attack does nothing, and put 3 damage counters on the Confused Pokémon. |
| `playmat.gametip.confused.header` | Confused |
| `playmat.gametip.discardpile.body` | You may search your discard pile or your opponent's discard pile at any time. |
| `playmat.gametip.discardpile.header` | Discard Pile |
| `playmat.gametip.evolution.body` | When a Pokémon evolves, it keeps all its Energy and damage counters. Any Special Conditions are removed. |
| `playmat.gametip.evolution.header` | Evolution |
| `playmat.gametip.mustfliptoattack.header` | Flipping to Attack |
| `playmat.gametip.mustfliptoplaytrainer` | You must flip heads to play a Trainer card. |
| `playmat.gametip.mustfliptoplaytrainer.header` | Must Flip to Play Trainer Card |
| `playmat.gametip.paralyzed.body` | While Paralyzed, a Pokémon cannot attack or retreat. At the end of your turn, your Pokémon returns to normal. |
| `playmat.gametip.paralyzed.header` | Paralyzed |
| `playmat.gametip.poisoned.body` | When a Pokémon is Poisoned, put 1 damage counter on it between turns. |
| `playmat.gametip.poisoned.header` | Poisoned |
| `playmat.gametip.pokebody.body` | A Poké-Body is an ability of a Pokémon that is active as long as that Pokémon is in play. |
| `playmat.gametip.pokebody.header` | Poké-Body |
| `playmat.gametip.prizecardawarded.body` | Every time you Knock Out an opponent's Pokémon, you take a Prize card. |
| `playmat.gametip.prizecardawarded.header` | Prize card awarded! |
| `playmat.gametip.retreat.body` | Retreating switches your Active Pokémon with one of your Benched Pokémon. To retreat, you must discard Energy from the retreating Pokémon equal to its Retreat Cost. |
| `playmat.gametip.retreat.header` | Retreat |
| `playmat.gametip.stadium.body` | Only one Stadium card can be in play at a time. If a new one comes into play, the old one is discarded. You can only play one Stadium card each turn. |
| `playmat.gametip.stadium.header` | Stadium |

#### `gamelog.message.*`  (29 keys)

| key | English |
|---|---|
| `gamelog.message.attachenergy` | [{0}]#player#[-] attached a #energyname# to #source#. |
| `gamelog.message.attachpokemontool` | [{0}]#player#[-] attached a #toolname# to #source#. |
| `gamelog.message.coinflip` | [{0}]#player#[-] flipped #coins# coin(s), resulting in #heads# heads and #tails# tails, for #effect#. |
| `gamelog.message.damage` | [{0}]#player#'s[-] #source# used #attack# and did #damage# damage to [{1}]#opponent#'s[-] #target#. |
| `gamelog.message.damageremoved` | [{0}]#player#'s[-] #source# healed for #heal#. |
| `gamelog.message.damagesimple` | [{0}]#player#'s[-] #source# did #damage# damage to [{1}]#opponent#'s[-] #target#. |
| `gamelog.message.damagetransfer` | [{0}]#player#'s[-] #source# had a damage counter moved to it. |
| `gamelog.message.devolved` | [{0}]#player#'s[-] #source# devolved into #target#. |
| `gamelog.message.effectadded` | [{0}]#player#'s[-] #source# is now #effect#. |
| `gamelog.message.effectremoved` | [{0}]#player#'s[-] #source# is no longer #effect#. |
| `gamelog.message.evolved` | [{0}]#player#'s[-] #source# evolved into #target#. |
| `gamelog.message.format` | {0} game has started. |
| `gamelog.message.heal` | [{0}]#player#'s[-] #source# healed for #heal#. |
| `gamelog.message.knockout` | [{0}]#opponent#'s[-] #target# was Knocked Out. |
| `gamelog.message.movetolostzone` | [{0}]#player#'s[-] #source# was moved to the Lost Zone. |
| `gamelog.message.newactivepokemon` | #source# became [{0}]#player#'s[-] new Active Pokémon. |
| `gamelog.message.nondamage` | [{0}]#player#'s[-] #source# used its #effect# attack. |
| `gamelog.message.opponentdrawcard` | [{0}]#player#[-] drew a card. |
| `gamelog.message.playerdrawcard` | [{0}]#player#[-] drew #source#. |
| `gamelog.message.playtobench` | [{0}]#player#[-] put #source# onto the Bench. |
| `gamelog.message.playtrainerstadium` | [{0}]#player#[-] played #source#. |
| `gamelog.message.pokeability` | [{0}]#player#'s[-] #source# used its #effect# Ability. |
| `gamelog.message.preventdamage` | [{0}]#player#'s[-] #source# prevented #effect#. |
| `gamelog.message.prizecard` | [{0}]#player#[-] took a Prize card. |
| `gamelog.message.reflectdamage` | [{0}]#player#'s[-] #source# had #damage# damage counters put on it from #effect#. |
| `gamelog.message.retreat` | [{0}]#player#'s[-] #source# retreated. |
| `gamelog.message.simplereflect` | [{0}]#player#'s[-] #source# had #damage# damage counter(s) put on it from #target#. |
| `gamelog.message.specialconditiondamage` | [{0}]#player#'s[-] #source# took #damage# damage because it was #effect#. |
| `gamelog.message.turnupdate` | It is now [{0}]#player#'s[-] turn (Turn ##num#). |

#### `specialvisualizations.*`  (174 keys)

| key | English |
|---|---|
| `specialvisualizations.additionalattackoption` | This Pokémon has gained additional attack effects. |
| `specialvisualizations.additionaltype` | This Pokémon is more than one type. |
| `specialvisualizations.attackcostdecreased` | This Pokémon's attacks cost less. |
| `specialvisualizations.attackcostincreased` | This Pokémon's attacks cost more. |
| `specialvisualizations.attackforfree` | This Pokémon can use its attacks with no attached Energy. |
| `specialvisualizations.benchdamagedealtaffected` | Damage from this Pokémon's attacks is affected by Weakness and Resistance for the opponent's Benched Pokémon. |
| `specialvisualizations.bsp_sm68.flashinghead` | This Pokémon takes no damage from attacks done by any opposing Pokémon that has Special Energy attached. |
| `specialvisualizations.bw8_90.plasmasteel` | This Pokémon takes no damage from attacks by the opponent's Pokémon-<i>EX</i>. |
| `specialvisualizations.bw9_100.frozencity` | Whenever you attach an Energy from your hand to 1 of your Pokémon, it will get 2 damage counters if it isn't a Team Plasma Pokémon. |
| `specialvisualizations.bw9_85.brightdown` | This Pokémon is immune to effects of attacks, including damage, done by any opposing Pokémon that has an Ability. |
| `specialvisualizations.cannotactivatepokeabilities` | This Pokémon's Ability can't be used. |
| `specialvisualizations.cannotattachspecialenergy` | You cannot attach any additional Special Energy cards to your Pokémon. |
| `specialvisualizations.cannotattack` | This Pokémon cannot attack this turn. |
| `specialvisualizations.cannotbehealed` | This Pokémon cannot be healed. |
| `specialvisualizations.cannotevolvefromhand` | This Pokémon's owner can't play any Pokémon from their hand to evolve it. |
| `specialvisualizations.cannotplayacespec` | This player can't play <i>ACE SPEC</i> cards from their hand. |
| `specialvisualizations.cannotplayenergyfromhand` | Energy cannot be attached to this Pokémon from its owner's hand. |
| `specialvisualizations.cannotplayevolutions` | This player can't play any Pokémon from their hand to evolve their Pokémon. |
| `specialvisualizations.cannotplayitem` | This player can't play Item cards. |
| `specialvisualizations.cannotplaypokemontoolcardsself` | Pokémon Tools cannot be attached to this Pokémon. |
| `specialvisualizations.cannotplayspecialenergy` | This player can't play Special Energy cards. |
| `specialvisualizations.cannotplaystadiumcards` | This player can't play Stadium cards. |
| `specialvisualizations.cannotplaysupporter` | This player can't play Supporter cards. |
| `specialvisualizations.cannotplaytoolcards` | This player can't play Pokémon Tool cards. |
| `specialvisualizations.cannotplaytrainer` | This player can't play Trainer cards. |
| `specialvisualizations.cannotremovedamagecounters` | Damage counters can't be removed from any Pokémon. (Damage counters can still be moved.) |
| `specialvisualizations.cannotretreat` | This Pokémon can't retreat. |
| `specialvisualizations.cannotuseability` | This Pokémon can't use an Ability. |
| `specialvisualizations.cannotusepokepower` | This Pokémon can't use a Poké-Power. |
| `specialvisualizations.canonlybedamage` | This Pokémon is protected from all effects of attacks, except damage. |
| `specialvisualizations.cantuseattackwithname` | This Pokémon can't use the following attack: {0}. |
| `specialvisualizations.damagedealtignoresresistance` | Attacks from this Pokémon ignore Resistance. |
| `specialvisualizations.damagedealtignoresweakness` | Attacks from this Pokémon ignore Weakness. |
| `specialvisualizations.damagedealtincreased` | Damage from this Pokémon is increased. |
| `specialvisualizations.damagedealtincreasedif` | This Pokémon may do additional damage. |
| `specialvisualizations.damagedealtreduced` | This Pokémon deals reduced damage. |
| `specialvisualizations.damagefromko` | When this Pokémon is Knocked Out by damage from an attack, it will do damage to the Attacking Pokémon. |
| `specialvisualizations.damagetakenbetweenturns` | This Pokémon will take damage between turns. |
| `specialvisualizations.damagetakenincreased` | This Pokémon takes increased damage. |
| `specialvisualizations.damagetakenpreventedif` | Damage done to this Pokémon may be prevented. |
| `specialvisualizations.damagetakenreduced` | This Pokémon takes less damage from attacks. |
| `specialvisualizations.damagetakenreducedif` | This Pokémon may take less damage from attacks. |
| `specialvisualizations.damagetakenreducednonfire` | This Pokémon takes less damage from the attacks of opposing non-{R} Pokémon. |
| `specialvisualizations.damagetakenreturnsdamage` | When this Pokémon is attacked, it will do damage to its attacker. |
| `specialvisualizations.damagetakeprevendedif` | This Pokémon may take no damage from attacks. |
| `specialvisualizations.damageunaffectedbyeffects` | Damage from this Pokémon's attacks is not affected by any effects on the opposing Active Pokémon. |
| `specialvisualizations.dealdamagewhendamaged` | If this Pokémon is damaged by an attack, it will put damage counters on the Attacking Pokémon. |
| `specialvisualizations.diesatendofturn` | This Pokémon will be Knocked Out at the end of this turn. |
| `specialvisualizations.disabledattack` | One of this Pokémon's attacks can't be used this turn. |
| `specialvisualizations.discardafteropponentsturn` | This Pokémon and all cards attached to it will be discarded at the end of this turn. |
| `specialvisualizations.discardsbacktohand` | This Pokémon will be returned to its owner's hand when it is Knocked Out. |
| `specialvisualizations.drawadditionalprizecardonkill` | When this Pokémon Knocks Out an opponent's Pokémon, its owner takes an additional Prize card. |
| `specialvisualizations.drawenergywhenkilled` | When this Pokémon is Knocked Out, its owner may search his or her deck for a card. |
| `specialvisualizations.drawwhendamaged` | When this Pokémon is damaged, its owner will draw cards. |
| `specialvisualizations.energycannotbediscarded` | Energy cards cannot be discarded from this Pokémon. |
| `specialvisualizations.energytypechanged` | This Pokémon's type has been changed. |
| `specialvisualizations.energyvalueincreased` | Your basic {P} Energy attached to your Pokémon provides {P}{P} Energy. |
| `specialvisualizations.flipmorecoinsnextattack` | This Pokémon's owner will flip more coins for its next attack. |
| `specialvisualizations.flipmorecoinstoawaken` | This Pokémon's owner must flip extra coins to awaken it. |
| `specialvisualizations.floralcrown` | This Pokémon heals at the end of its opponent's turn. |
| `specialvisualizations.forcedcoinfliptails` | Coins flipped by this player will always count as tails. |
| `specialvisualizations.healingscarf` | This Pokémon is healed when an Energy card is attached to it from its owner's hand. |
| `specialvisualizations.healsbetweenturns` | Damage is healed from this Pokémon between turns. |
| `specialvisualizations.hexmaniac` | This player's Pokémon in play, in their hand, and in their discard pile have no Abilities. |
| `specialvisualizations.hpmaxup` | This Pokémon has extra HP. |
| `specialvisualizations.immunetoallattackdamage` | This Pokémon is immune to damage from attacks. |
| `specialvisualizations.immunetoasleep` | This Pokémon is immune to being Asleep. |
| `specialvisualizations.immunetoattackdamage` | This Pokémon takes no damage from the opponent's attacks. |
| `specialvisualizations.immunetoattackeffects` | This Pokémon is immune to effects from attacks. |
| `specialvisualizations.immunetoattackeffectsanddamage` | This Pokémon is immune to effects from attacks, including damage. |
| `specialvisualizations.immunetoconfuse` | This Pokémon is immune to being Confused. |
| `specialvisualizations.immunetoopponentabilities` | This Pokémon is immune to all effects of the opponent's Pokémon's Abilities. |
| `specialvisualizations.immunetoparalysis` | This Pokémon cannot be Paralyzed. |
| `specialvisualizations.immunetoparalyze` | This Pokémon is immune to being Paralyzed. |
| `specialvisualizations.immunetopoison` | This Pokémon is immune to being Poisoned. |
| `specialvisualizations.immunetospecialconditions` | This Pokémon is immune to Special Conditions. |
| `specialvisualizations.increasedweaknessdamage` | This Pokémon's Weakness has been increased. |
| `specialvisualizations.ironfistofjustice` | This Pokémon's owner has a Team Plasma Pokémon in play. Iron Fist of Justice does nothing. |
| `specialvisualizations.joinedteamplasma` | This Pokémon has joined Team Plasma. |
| `specialvisualizations.killedondamage` | If this Pokémon is damaged by an attack, it is Knocked Out. |
| `specialvisualizations.maynotbeknockedout` | This Pokémon may remain at 10 HP when it would be Knocked Out. |
| `specialvisualizations.metaldealsincreaseddamage` | This player's {M} Pokémon's attacks do more damage to the opponent's Active Pokémon. |
| `specialvisualizations.moveenergywhenallykilled` | When an Active Pokémon is Knocked Out, its owner moves 1 basic Energy card from that Pokémon to this Pokémon. |
| `specialvisualizations.mustfliptoattack` | This Pokémon's owner must flip a coin successfully before it can use an attack. |
| `specialvisualizations.mustfliptoplaytrainer` | A coin must be flipped to play Trainer cards. If tails, the Trainer card has no effect. |
| `specialvisualizations.noabilitiesthisturn` | This Pokémon has no Abilities this turn. |
| `specialvisualizations.noprizecards` | If this Pokémon is Knocked Out, the opponent can't take a Prize card. |
| `specialvisualizations.noretreatcost` | This Pokémon has no Retreat Cost. |
| `specialvisualizations.player.bw10_23.cursedglare` | This player can't attach any Special Energy cards from their hand to their Pokémon. |
| `specialvisualizations.player.bw8_46.dualbrains` | This player may play 2 Supporter cards during their turn. |
| `specialvisualizations.player.cannotdrawstartofturn` | This player can't draw a card at the beginning of their turn. |
| `specialvisualizations.player.cannothealpokemon` | Pokémon cannot be healed. (Damage counters can still be moved.) |
| `specialvisualizations.player.cannotplayevolutions` | This player can't play any Pokémon from their hand to evolve their Pokémon. |
| `specialvisualizations.player.cannotplaytrainers` | This player can't play Trainer cards. |
| `specialvisualizations.player.evolvedpokemonuseanyattack` | Your evolved Pokémon can use any attack from their previous Evolutions. |
| `specialvisualizations.player.gen_52.benchbarrier` | Damage done to this player's Benched Pokémon by attacks is prevented. |
| `specialvisualizations.player.hgss3_23.defensesign` | Damage done to this player's Benched {G} Pokémon by attacks is prevented. |
| `specialvisualizations.player.nopokepower` | This player can't use any of their Pokémon's Poké-Powers. |
| `specialvisualizations.player.pokemondealdecreaseddamage` | This player's attacks do less damage. |
| `specialvisualizations.player.pokemondealincreaseddamage` | This player's attacks do more damage. |
| `specialvisualizations.player.silentlab` | Basic Pokémon in play, in each player's hand, and in each player's discard pile have no Abilities. |
| `specialvisualizations.player.sm3_6.disgustingpollen` | This player's Basic Pokémon can't attack. |
| `specialvisualizations.player.sm4_34.heavyrockgx` | This player can't play any cards from their hand. |
| `specialvisualizations.player.sm4_38.gnawingcurse` | Whenever this player attaches an Energy card from their hand to 1 of their Pokémon, that Pokémon will get 2 damage counters. |
| `specialvisualizations.player.sm4_43.bellofsilence` | This player can't play any Pokémon that has an Ability from their hand. |
| `specialvisualizations.player.sm4_90.gyrounit` | This player's Basic Pokémon in play have no Retreat Cost. |
| `specialvisualizations.player.sm4_93.devouredfield` | The attacks of {D} Pokémon and {N} Pokémon do 10 more damage to the opponent's Active Pokémon. |
| `specialvisualizations.player.xy7_57.chaoswheel` | This player can't play Pokémon Tool, Special Energy, or Stadium cards from their hand. |
| `specialvisualizations.player.xy7_74.forestofgiantplants` | Each {G} Pokémon can evolve on the first turn or during the turn it is played. |
| `specialvisualizations.player.xy8_145.parallelcitya` | The attacks of this player's {G}, {R}, and {W} Pokémon do 20 less damage <i>(before applying Weakness and Resistance)</i>. |
| `specialvisualizations.player.xy8_145.parallelcityb` | This player can't have more than 3 Benched Pokémon. |
| `specialvisualizations.player.xy9_57.garbotoxin` | Pokémon in play, in each player's hand, and in each player's discard pile have no Abilities (except for Garbotoxin). |
| `specialvisualizations.pokemonhasnoability` | This Pokémon has no Abilities. |
| `specialvisualizations.pokemontoolhasnoeffect` | This Pokémon Tool has no effect. |
| `specialvisualizations.poketoolattached` | This Pokémon has a Pokémon Tool attached. |
| `specialvisualizations.preventdamageeffectsbygxex` | This Pokémon is immune to all effects of attacks, including damage, done by the opponent's Pokémon-<i>GX</i> and Pokémon-<i>EX</i>. |
| `specialvisualizations.preventeffectsplayershand` | All effects of attacks done to this player or their hand by the opponent's Pokémon are prevented. |
| `specialvisualizations.preventeffectstobench` | All effects of the opponent's attacks, including damage, done to this player's Benched Pokémon are prevented. |
| `specialvisualizations.prizecardvaluereduced` | The player who Knocks Out this Pokémon takes one fewer Prize card. |
| `specialvisualizations.reflipforattacks` | This Pokémon's owner may choose to reflip coins from this Pokémon's attacks. |
| `specialvisualizations.resistanceincreased` | This Pokémon's Resistance has been increased. |
| `specialvisualizations.resistanceremoved` | This Pokémon has no Resistance. |
| `specialvisualizations.retreatcostincreased` | This Pokémon's Retreat Cost has been increased. |
| `specialvisualizations.retreatcostreduced` | This Pokémon's Retreat Cost has been reduced. |
| `specialvisualizations.sl_3.jungletotem` | Each basic {G} Energy attached to this player's Pokémon provides {G}{G} Energy. |
| `specialvisualizations.sm1_108.echoedvoice` | This Pokémon's Echoed Voice attack does more damage. |
| `specialvisualizations.sm1_128.professorkukui` | Your Pokémon's attacks do 20 more damage to your opponent's Active Pokémon. |
| `specialvisualizations.sm1_46.waterbubble` | Damage done to this Pokémon by attacks from the opponent's {R} Pokémon is prevented. |
| `specialvisualizations.sm1_58.powerofalchemy` | Basic Pokémon in play, in each player's hand, and in each player's discard pile have no Abilities. |
| `specialvisualizations.sm1_95.dragonswish` | You may attach any number of Energy cards from your hand to your Pokémon. |
| `specialvisualizations.sm2_116.aetherparadiseconservationarea` | Basic {G} and {L} Pokémon take 30 less damage from the opponent's attacks <i>(after applying Weakness and Resistance)</i>. |
| `specialvisualizations.sm2_117.altarofthemoone` | The Retreat Cost of each Pokémon that has any {P} or {D} Energy attached to it is {C}{C} less. |
| `specialvisualizations.sm2_118.altarofthesunne` | {R} and {M} Pokémon have no Weakness. |
| `specialvisualizations.sm2_64.dauntingpose` | The opponent's attacks and Abilities can't put damage counters on this player's Benched Pokémon. |
| `specialvisualizations.sm2_66.roadblock` | This player can't have more than 4 Benched Pokémon. |
| `specialvisualizations.sm2_91.thewagesoffluff` | If this Pokémon is Knocked Out during the opponent's next turn, that player takes 2 more Prize cards. |
| `specialvisualizations.sm2_93.flowershield` | This player's Pokémon that have {Y} Energy attached can't be affected by Special Conditions. |
| `specialvisualizations.sm3_121.potown` | Whenever this player plays a Pokémon from their hand to evolve 1 of their Pokémon, they must put 3 damage counters on that Pokémon. |
| `specialvisualizations.sm3_28.luminousbarrier` | This Pokémon is immune to all effects of attacks, including damage, done by opposing Pokémon-<i>GX</i> and Pokémon-<i>EX</i>. |
| `specialvisualizations.sm3_35.thickfat` | This Pokémon takes 30 less damage from the attacks of opposing {R} and {W} Pokémon. |
| `specialvisualizations.sm3_37.intimidatingpattern` | This player's Active Pokémon's attacks do 30 less damage. |
| `specialvisualizations.sm3_63.lightsend` | Damage done to this Pokémon by attacks from {C} Pokémon is prevented. |
| `specialvisualizations.sm3_68.healblock` | This player's Pokémon can't be healed. |
| `specialvisualizations.sm4_17.submerge` | This Pokémon is immune to damage done by attacks. |
| `specialvisualizations.sm4_28.icebergshield` | This Pokémon is immune to all effects of attacks, including damage, done by opposing Stage 2 Pokémon. |
| `specialvisualizations.sm4_77.submerge` | This Pokémon takes no damage from attacks. |
| `specialvisualizations.sm4_94.fightingmemory` | This Pokémon is a {F} Pokémon. |
| `specialvisualizations.sm4_98.psychicmemory` | This Pokémon is a {P} Pokémon. |
| `specialvisualizations.sm5_11.weatherguard` | This player's {G} Pokémon have no Weakness. |
| `specialvisualizations.sm5_19.incandescentbody` | If this Pokémon is damaged by an opponent's attack, the Attacking Pokémon is Burned. |
| `specialvisualizations.sm5_39.freezinggaze` | This player's Pokémon-<i>GX</i> and Pokémon-<i>EX</i> in play, in their hand, and in their discard pile have no Abilities, except for Freezing Gaze. |
| `specialvisualizations.sm5_81.solidunit` | This Pokémon takes no damage from attacks. |
| `specialvisualizations.sm5_85.earthenshield` | This player's {M} Pokémon take no damage from attacks by the opponent's Pokémon that have Special Energy attached. |
| `specialvisualizations.specialburnamount` | This Pokémon takes more damage from being Burned. |
| `specialvisualizations.specialconfusedamount` | This Pokémon takes more damage from being Confused. |
| `specialvisualizations.specialparalyzeduration` | This Pokémon has been Paralyzed for a longer duration. |
| `specialvisualizations.specialpoisonamount` | This Pokémon takes more damage from being Poisoned. |
| `specialvisualizations.teamplasmadrawcard` | If this Pokémon is a Team Plasma Pokémon and gets Knocked Out, its owner may search their deck for a card. |
| `specialvisualizations.turncontinuesonmegaevolution` | The turn will not end when this Pokémon evolves. |
| `specialvisualizations.weaknessadded` | This Pokémon has an additional Weakness. |
| `specialvisualizations.weaknesschanged` | This Pokémon's Weakness has been changed. |
| `specialvisualizations.weaknessremoved` | This Pokémon's Weakness has been removed. |
| `specialvisualizations.xy10_49.abilityenergykeeper` | Basic Energy cards cannot be discarded from this Pokémon by an opponent's effect. |
| `specialvisualizations.xy10_7.slashingstrike` | This Pokémon can't use Slashing Strike this turn. |
| `specialvisualizations.xy11_59.poisonenzyme` | This Pokémon takes no damage from attacks by the opponent's Poisoned Pokémon. |
| `specialvisualizations.xy11_74.quickguard` | This Pokémon takes no damage from attacks by Basic Pokémon. |
| `specialvisualizations.xy11_80.wonderlock` | This Pokémon takes no damage from attacks by the opponent's Mega Evolution Pokémon. |
| `specialvisualizations.xy12_47.littlegrudge` | If this Pokémon is Knocked Out by damage from an attack, an Energy attached to the Attacking Pokémon will be discarded. |
| `specialvisualizations.xy12_51.barrier` | Your Pokémon can't use the Barrier attack this turn. |
| `specialvisualizations.xy12_53.neutralshield` | This Pokémon is immune to attacks from the opponent's Evolution Pokémon. |
| `specialvisualizations.xy4_36.bidebarricade` | Pokémon in play, in each player's hand, and in each player's discard pile have no Abilities (except for {P} Pokémon). |
| `specialvisualizations.xy9_67.watchandlearn` | This Pokémon's attack is replaced by the attack used during the opponent's last turn. |
| `specialvisualizations.xy9_8.boohoo` | If an Energy card is attached to this Pokémon, it will be Asleep. |
| `specialvisualizations.xy9_82.moonbarrier` | This Pokémon is immune to all effects of attacks from {N} Pokémon, including damage. |

#### `pregame.ui.*`  (51 keys)

| key | English |
|---|---|
| `pregame.ui.buttons.change` | Change |
| `pregame.ui.buttons.inactive.comingsoon` | Coming Soon |
| `pregame.ui.buttons.next` | Next |
| `pregame.ui.buttons.playbtn.text.disabled` | <i><color=555555>Play  Now!</color></i> |
| `pregame.ui.buttons.playbtn.text.enabled` | <i><color=F36812>Play  Now!</color></i> |
| `pregame.ui.buttons.viewfilters` | View Filters |
| `pregame.ui.choosedifficulty` | Choose Difficulty |
| `pregame.ui.choosefriend` | Choose a Friend to Battle |
| `pregame.ui.choosemode` | Choose Mode |
| `pregame.ui.chooseplayformat` | Choose Type |
| `pregame.ui.chooseyouropponenttype` | Choose Your Opponent Type |
| `pregame.ui.deckstats.deckunlocks` | Deck Unlocks: |
| `pregame.ui.deckstats.overallplayed` | Overall Played |
| `pregame.ui.deckstats.overallwins` | Overall Wins |
| `pregame.ui.deckstats.playededit` | Played Since Last Edit |
| `pregame.ui.deckstats.winsedit` | Wins Since Last Edit |
| `pregame.ui.deckstats.winsuntilbooster` | Wins Until Booster Pack: |
| `pregame.ui.deckstats.winsuntilbooster.tooltip` | If you beat 12 different Trainers, you'll unlock a booster pack for your Collection. |
| `pregame.ui.playformat.challegetext.expanded` | Expanded |
| `pregame.ui.playformat.challegetext.modified` | Standard deck |
| `pregame.ui.playformat.challegetext.theme` | theme deck |
| `pregame.ui.playformat.challegetext.unlimited` | Unlimited deck |
| `pregame.ui.playformat.ineligibledeck` | Deck Ineligible for Format |
| `pregame.ui.playformat.modified` | Standard |
| `pregame.ui.playformat.modifiedlegal` | Standard |
| `pregame.ui.playformat.modifiednotlegal` | Standard |
| `pregame.ui.playformat.theme` | Theme |
| `pregame.ui.playformat.themedecklegal` | Theme Deck |
| `pregame.ui.playformat.themedecknotlegal` | Theme Deck |
| `pregame.ui.playformat.unlimited` | Unlimited |
| `pregame.ui.playformat.unlimitedlegal` | Unlimited |
| `pregame.ui.playformat.unlimitednotlegal` | Unlimited |
| `pregame.ui.playformat.unselected` | No Play Style Selected |
| `pregame.ui.playtitle` | Play |
| `pregame.ui.searchbox.search` | Search |
| `pregame.ui.selectyourdeck` | Select Your Deck |
| `pregame.ui.selectyourmodeofplay` | Select Your Mode of Play |
| `pregame.ui.versus.difficulty.beginner` | Beginner |
| `pregame.ui.versus.difficulty.expert` | Expert |
| `pregame.ui.versus.difficulty.intermediate` | Intermediate |
| `pregame.ui.versus.difficulty.novice` | Novice |
| `pregame.ui.versus.expertgame` | Expert games allow you to progress and play against better and better Trainers. They offer bigger rewards but can be more challenging. |
| `pregame.ui.versus.mydeck` | My Deck |
| `pregame.ui.versus.novicegame` | Novice games let you play just for fun. Don't worry about losing anything, but don't expect big rewards. |
| `pregame.ui.versus.opponenttype` | Opponent Type |
| `pregame.ui.versus.playbtndisabled` | <i><color=555555>Play  Now!</color></i> |
| `pregame.ui.versus.playbtnenabled` | <i><color=F36812>Play  Now!</color></i> |
| `pregame.ui.versus.playtype` | Play Type |
| `pregame.ui.versus.rankedbattle` | Ranked Battle |
| `pregame.ui.versus.unrankedbattle` | Unranked Battle |
| `pregame.ui.youveselected` | You've Selected |

#### `playmat.prompt.*` - generic only (card-specific `<set>_<num>` keys omitted)  (389 keys)

| key | English |
|---|---|
| `playmat.prompt.asleep` | Asleep |
| `playmat.prompt.attachanyenergythispokemon` | Choose an Energy to attach to this Pokémon. |
| `playmat.prompt.attachbasicenergythispokemon` | Choose a Basic Energy to attach to this Pokémon. |
| `playmat.prompt.attachenergy` | Choose a Pokémon to attach the Energy to. |
| `playmat.prompt.attack` | Attack |
| `playmat.prompt.beforeattackenergy.body` | Once you attack, you can't attach Energy or play any other cards. Do you want to skip attaching Energy? |
| `playmat.prompt.beforeattackenergy.header` | Energy |
| `playmat.prompt.benchfull` | You can't select any Pokémon because your Bench is full. |
| `playmat.prompt.bill.body` | You have a Bill Supporter card in your hand that you can play. Are you sure you want to continue without playing it this turn? |
| `playmat.prompt.bill.header` | Bill |
| `playmat.prompt.bsp_sm68.lightinggx` | Choose a card to add to your opponent's Prize cards face down. |
| `playmat.prompt.bsp_sm70.goldenwing` | Choose up to 2 basic Energy cards to move to your Benched Pokémon. |
| `playmat.prompt.bsp_xy_85.hyperspacering` | Choose up to 2 Item cards to put into your hand. |
| `playmat.prompt.burned` | Burned |
| `playmat.prompt.burning` | Burning |
| `playmat.prompt.caitlin.handcontrol` | These are the cards your opponent put on the bottom of your deck, starting with the card on the left. |
| `playmat.prompt.choose2cardtoputintohand` | Choose two cards to put into your hand. |
| `playmat.prompt.chooseaction` | Choose an Action |
| `playmat.prompt.chooseanenergytoattach` | Choose an Energy card to attach to 1 of your Pokémon. |
| `playmat.prompt.chooseattacktouse` | Choose an attack to use. |
| `playmat.prompt.choosebasicenergyforhand` | Choose basic Energy cards to put into your hand. |
| `playmat.prompt.choosebasicpokemon` | Choose a Basic Pokémon. |
| `playmat.prompt.choosebenchedpokemontodamage` | Choose an opposing Benched Pokémon to do damage to. |
| `playmat.prompt.choosecards` | Choose from these cards. Click OK when done if choosing more than one card. |
| `playmat.prompt.choosecardtoputintohand` | Choose a card to put into your hand. |
| `playmat.prompt.choosecardtoputondeck` | Choose a card to put on top of your deck. |
| `playmat.prompt.chooseenergy.nobenchpokemon` | You have no Pokémon on your Bench, so you can't choose any Energy to attach to them. |
| `playmat.prompt.chooseenergydiscard` | Choose an Energy card to discard. |
| `playmat.prompt.chooseenergymove` | Choose an Energy card to move. |
| `playmat.prompt.chooseevolutionforplay` | Choose a Pokémon that evolves from this Pokémon to put onto this Pokémon. |
| `playmat.prompt.chooseitemforhand` | Choose an Item card to put into your hand. |
| `playmat.prompt.choosemenergytoattach` | Choose a {M} Energy to attach to 1 of your Pokémon. |
| `playmat.prompt.chooseoption` | Choose one of the options provided. |
| `playmat.prompt.choosepokemondamage` | Select a Pokémon to remove a damage counter from. |
| `playmat.prompt.choosepokemonforbench` | Choose Basic Pokémon to put onto your Bench. |
| `playmat.prompt.choosepokemonforhand` | Choose Pokémon to put into your hand. |
| `playmat.prompt.choosepokemonforhand.single` | Choose a Pokémon to put into your hand. |
| `playmat.prompt.chooseselfbenchedpokemontodamage` | Choose 1 of your Benched Pokémon to do damage to. |
| `playmat.prompt.choosesupporterforhand` | Choose a Supporter card to put into your hand. |
| `playmat.prompt.choosetoolsforhand` | Choose Pokémon Tool cards to put into your hand. |
| `playmat.prompt.choosetrainerforhand` | Choose a Trainer card to put into your hand. |
| `playmat.prompt.choosetwopokemontodamage` | Choose 2 of your opponent's Pokémon to do damage to. |
| `playmat.prompt.chooseupto2forhand` | Choose up to 2 cards to put into your hand. |
| `playmat.prompt.chooseyourdeck` | Your deck? |
| `playmat.prompt.coinflip.attack` | Flipping for your Active Pokémon's attack. |
| `playmat.prompt.coinflip.burned` | Flipping to see if the Active Pokémon takes damage from being Burned... |
| `playmat.prompt.coinflip.opponentasleep` | Opponent is flipping to see if the Active Pokémon wakes up. |
| `playmat.prompt.coinflip.opponentattack` | Flipping for your opponent's Active Pokémon's attack. |
| `playmat.prompt.coinflip.opponentburned` | Opponent is flipping to see if the Active Pokémon takes damage from being Burned. |
| `playmat.prompt.coinflip.opponentconfused` | Opponent is flipping to see if the Active Pokémon hurts itself. |
| `playmat.prompt.coinflip.opponentpokeability` | Flipping for your opponent's Pokémon's Ability. |
| `playmat.prompt.coinflip.pokeability` | Flipping for your Pokémon's Ability. |
| `playmat.prompt.coinflip.title` | Coin Flip Results |
| `playmat.prompt.coinflip.trainer` | Flipping for the Trainer card. |
| `playmat.prompt.coinflippick.title` | Pick One |
| `playmat.prompt.combinelegend` | Select the other half of this Pokémon LEGEND to put it onto your Bench. |
| `playmat.prompt.concede.header` | Concede |
| `playmat.prompt.confirmselection.text` | Select OK to confirm your choice. |
| `playmat.prompt.confused` | Confused |
| `playmat.prompt.discard2energy` | Choose 2 Energy to discard. |
| `playmat.prompt.discardabenchedpokemon` | Discard a Benched Pokémon. |
| `playmat.prompt.discardanynumber` | Choose any number of cards to discard. |
| `playmat.prompt.discardstadiuminplay` | Choose the Stadium card to discard it, or select Done to leave the Stadium in play. |
| `playmat.prompt.discardtool` | Choose a Pokémon Tool to discard. |
| `playmat.prompt.discarduntil4` | Select the cards you want to discard until only 4 cards remain. |
| `playmat.prompt.discarduntil5` | Select the cards you want to discard until only 5 cards remain. |
| `playmat.prompt.domefossilkabuto` | Select a Kabuto to put onto your Bench. |
| `playmat.prompt.dragbenchtoactive` | Drag a Pokémon from your Bench to become the Active Pokémon. |
| `playmat.prompt.draw.header` | Would you like to draw a card? |
| `playmat.prompt.drawto6.header` | Would you like to draw until you have 6 cards in your hand? |
| `playmat.prompt.emptybench.body` | If you have no Pokémon in play, you lose the game. If your Active Pokémon is Knocked Out, you have no Benched Pokémon to take its place. Are you sure you want to continue without putting Pokémon on your Bench? |
| `playmat.prompt.emptybench.header` | Empty Bench |
| `playmat.prompt.endturn.body` | Are you sure you want to end your turn without attacking? |
| `playmat.prompt.endturn.header` | End Turn |
| `playmat.prompt.endturnability.body` | Are you sure you want to end your turn without using an Ability, Poké-Power, or Poké-Body? |
| `playmat.prompt.energy.body` | Pokémon need Energy to attack, and you have an Energy card in your hand. Are you sure you want to end your turn without attaching Energy to a Pokémon? |
| `playmat.prompt.energy.header` | Energy |
| `playmat.prompt.evolution.body` | At least 1 of your Pokémon is ready to evolve. Are you sure you want to end your turn without evolving your Pokémon? |
| `playmat.prompt.evolution.header` | Evolution |
| `playmat.prompt.expshare` | Select an Energy to move using Exp. Share. |
| `playmat.prompt.expshare.selectenergy` | Select the Energy you want to move. |
| `playmat.prompt.extraenergy.body` | Are you sure you want to attach extra Energy to that Pokémon? You already have enough for its most powerful attack. |
| `playmat.prompt.fewercards` | Are you sure you want to do this? Playing this card will result in having fewer cards in your hand. |
| `playmat.prompt.fewercardsattack` | Are you sure you want to do this? Using this attack will result in having fewer cards in your hand. |
| `playmat.prompt.flipacoin` | Flip a coin. |
| `playmat.prompt.flipcoins` | Flip coins. |
| `playmat.prompt.fossilexcavationkit` | Choose 2 cards from the following to put into your hand: Helix Fossil Omanyte, Dome Fossil Kabuto, or Old Amber Aerodactyl. |
| `playmat.prompt.fullbench` | Your Bench is full. |
| `playmat.prompt.generic.cancel` | Cancel |
| `playmat.prompt.generic.no` | No |
| `playmat.prompt.generic.yes` | Yes |
| `playmat.prompt.hasnoeffect` | Are you sure you want to do this? Playing this card will have no effect. |
| `playmat.prompt.helixfossilomanyte` | Select an Omanyte to put onto your Bench. |
| `playmat.prompt.inspect` | Inspect the playmat and click OK to return to the current selection. |
| `playmat.prompt.kiawe` | Choose up to 4 {R} Energy cards to attach to 1 of your Pokémon. |
| `playmat.prompt.korrina.combined` | Choose a {F} Pokémon and an Item card to put into your hand. |
| `playmat.prompt.kyogreex.giantwhirlpool` | Choose 2 {W} Energy to return to your hand. |
| `playmat.prompt.lookatopponenthand` | Look at your opponent's hand. |
| `playmat.prompt.maychooseenergydiscard` | You may choose an Energy card to discard. |
| `playmat.prompt.mayplacedamageonopponentpokemon` | Put 1 damage counter on each of your opponent's Pokémon? |
| `playmat.prompt.mayselectenergytodiscard` | When you play this Pokémon from your hand to evolve 1 of your Pokémon, you may discard an Energy attached to your opponent's Active Pokémon. |
| `playmat.prompt.mayselecttodiscard` | You may select a card to discard. |
| `playmat.prompt.megacatcher` | Choose one of your opponent's Mega Evolution Pokémon to make Active. |
| `playmat.prompt.megaevolution.body` | When 1 of your Pokémon becomes a Mega Evolution Pokémon, your turn ends. Are you sure you want to do this? |
| `playmat.prompt.megaevolution.body.b` | You just played a Mega Evolution Pokémon! Your turn will now end unless the matching Spirit Link card is attached to that Pokémon. |
| `playmat.prompt.megaevolution.header` | Mega Evolution |
| `playmat.prompt.move1damagecounter` | Select a Pokémon to move a damage counter to. |
| `playmat.prompt.moveblendgrpdenergy` | Select a Pokémon to move the Blend Energy {G}{R}{P}{D} to. |
| `playmat.prompt.moveblendwlfmenergy` | Select a Pokémon to move the Blend Energy {W}{L}{F}{M} to. |
| `playmat.prompt.moveburningenergy` | Select a Pokémon to move the Burning Energy to. |
| `playmat.prompt.movecounterenergy` | Select a Pokémon to move the Counter Energy to. |
| `playmat.prompt.movedangerousenergy` | Select a Pokémon to move the Dangerous Energy to. |
| `playmat.prompt.movedarknessenergy` | Select a Pokémon to move the {D} Energy to. |
| `playmat.prompt.movedoubleaquaenergy` | Select a Pokémon to move the Double Aqua Energy to. |
| `playmat.prompt.movedoublecolorlessenergy` | Select a Pokémon to move the Double Colorless Energy to. |
| `playmat.prompt.movedoubledragonenergy` | Select a Pokémon to move the Double Dragon Energy to. |
| `playmat.prompt.movedoublemagmaenergy` | Select a Pokémon to move the Double Magma Energy to. |
| `playmat.prompt.movefairyenergy` | Select a Pokémon to move the {Y} Energy to. |
| `playmat.prompt.movefightingenergy` | Select a Pokémon to move the {F} Energy to. |
| `playmat.prompt.movefireenergy` | Select a Pokémon to move the {R} Energy to. |
| `playmat.prompt.moveflashenergy` | Select a Pokémon to move the Flash Energy to. |
| `playmat.prompt.movegrassenergy` | Select a Pokémon to move the {G} Energy to. |
| `playmat.prompt.moveherbalenergy` | Select a Pokémon to move the Herbal Energy to. |
| `playmat.prompt.movelightningenergy` | Select a Pokémon to move the {L} Energy to. |
| `playmat.prompt.movemetalenergy` | Select a Pokémon to move the {M} Energy to. |
| `playmat.prompt.movemulitpledamagecountersfrom` | Choose a Pokémon to move damage counters from. |
| `playmat.prompt.movemysteryenergy` | Select a Pokémon to move the Mystery Energy to. |
| `playmat.prompt.moveplasmaenergy` | Select a Pokémon to move the Plasma Energy to. |
| `playmat.prompt.moveprismenergy` | Select a Pokémon to move the Prism Energy to. |
| `playmat.prompt.movepsychicenergy` | Select a Pokémon to move the {P} Energy to. |
| `playmat.prompt.moverainbowenergy` | Select a Pokémon to move the Rainbow Energy to. |
| `playmat.prompt.moverescueenergy` | Select a Pokémon to move the Rescue Energy to. |
| `playmat.prompt.moveshieldenergy` | Select a Pokémon to move the Shield Energy to. |
| `playmat.prompt.movespecialdarknessenergy` | Select a Pokémon to move the Special Darkness Energy to. |
| `playmat.prompt.movespecialmetalenergy` | Select a Pokémon to move the Special Metal Energy to. |
| `playmat.prompt.movesplashenergy` | Select a Pokémon to move the Splash Energy to. |
| `playmat.prompt.movestrongenergy` | Select a Pokémon to move the Strong Energy to. |
| `playmat.prompt.movesuperboostenergyprismstar` | Choose a Pokémon to attach the Super Boost Energy {*} to. |
| `playmat.prompt.moveunitenergygrw` | Choose a Pokémon to attach the Unit Energy {G}{R}{W} to. |
| `playmat.prompt.moveunitenergylpm` | Choose a Pokémon to attach the Unit Energy {L}{P}{M} to. |
| `playmat.prompt.movewarpenergy` | Select a Pokémon to move the Warp Energy to. |
| `playmat.prompt.movewaterenergy` | Select a Pokémon to move the {W} Energy to. |
| `playmat.prompt.movewonderenergy` | Select a Pokémon to move the Wonder Energy to. |
| `playmat.prompt.noavailableattacks` | You do not have the necessary Energy to use any of your opponent's Active Pokémon's attacks at this time, so this attack will do nothing. Do you still want to do this? |
| `playmat.prompt.noselection` | Sorry, there are no valid cards to select. Click Done to continue. |
| `playmat.prompt.olivia` | Choose up to 2 Pokémon-<i>GX</i> to put into your hand. |
| `playmat.prompt.opponentbenchfull` | You can't select any Pokémon because your opponent's Bench is full. |
| `playmat.prompt.opponentcoinchoice` | {0} chose {0}. |
| `playmat.prompt.opponentdiscard` | Your opponent is discarding a card. Click anywhere to continue. |
| `playmat.prompt.opponentflipcoins` | Your opponent flips a coin. |
| `playmat.prompt.opponentgoesfirst` | {0} goes first. |
| `playmat.prompt.opponentplaycard` | Your opponent is playing a card. Click the playmat to continue. |
| `playmat.prompt.opponentpull` | Choose an opponent's Benched Pokémon to make it their Active Pokémon. |
| `playmat.prompt.opponentpush` | Choose a Benched Pokémon to become your Active Pokémon. |
| `playmat.prompt.optionalswitch` | You may choose a Benched Pokémon to become your Active Pokémon. |
| `playmat.prompt.palpad` | Choose 2 Supporter cards to shuffle into your deck. |
| `playmat.prompt.paralyzed` | Paralyzed |
| `playmat.prompt.playergoesfirst` | {0} goes first. |
| `playmat.prompt.playlegend` | Play your Pokémon LEGEND onto the Bench. |
| `playmat.prompt.poisoned` | Poisoned |
| `playmat.prompt.promosm_56.queenscommandgx` | Choose 4 cards to discard. |
| `playmat.prompt.putdamagecounters.1` | Select a Pokémon to put 1 damage counter on. |
| `playmat.prompt.putdamagecounters.2` | Select a Pokémon to put 2 damage counters on. |
| `playmat.prompt.putdamagecounters.3` | Select a Pokémon to put 3 damage counters on. |
| `playmat.prompt.putdamagecounters.4` | Select a Pokémon to put 4 damage counters on. |
| `playmat.prompt.removespecialcondition` | Choose a Special Condition to remove from your Active Pokémon. |
| `playmat.prompt.reorder.topcardsfromdeck` | Select cards in order to put on top of your deck, starting with the card you want on the bottom. |
| `playmat.prompt.reorderbottomcardsfromdeck.new` | Select cards to put on the bottom of the deck. |
| `playmat.prompt.reordertopcardsfromdeck` | Select cards to return to the top of the deck. Start with the card you want on the bottom. |
| `playmat.prompt.reordertopcardsfromdeck.new` | Select cards to return to the top of the deck. |
| `playmat.prompt.reordertopcardsfromdeckreverse` | Select cards to return to the top of your deck. |
| `playmat.prompt.rescue.customchoicedialog` | Would you like to put this Pokémon in the Lost Zone or into your hand? |
| `playmat.prompt.rescue.customchoicedialog.hand` | My Hand |
| `playmat.prompt.rescue.customchoicedialog.lostzone` | Lost Zone |
| `playmat.prompt.restoredpokemonbench` | Choose a Restored Pokémon to put onto your Bench. |
| `playmat.prompt.retreat.body` | Check the Retreat Cost first. Are you sure you want to retreat? |
| `playmat.prompt.retreat.header` | Retreat |
| `playmat.prompt.returncardstodeckbottom` | Select cards to put on the bottom of your deck. Start with the card you want on the bottom. |
| `playmat.prompt.returnenergytohand` | Choose an Energy to put into your hand. |
| `playmat.prompt.returnpokemontoopponenthand` | Choose a Pokémon to put into your opponent's hand. |
| `playmat.prompt.rotomdexpokefindermode.reveal` | These are the top 4 cards of your deck. |
| `playmat.prompt.select.1.ninjask` | Select 1 Ninjask card. |
| `playmat.prompt.select.2.ninjask` | Select 2 Ninjask cards. |
| `playmat.prompt.select1aquapokemon` | Select a Team Aqua Pokémon. |
| `playmat.prompt.select1basicaquapokemon` | Select a Basic Team Aqua Pokémon. |
| `playmat.prompt.select1basicenergy` | Select a basic Energy card. |
| `playmat.prompt.select1basicmagmapokemon` | Select a Basic Team Magma Pokémon. |
| `playmat.prompt.select1energy` | Select an Energy card. |
| `playmat.prompt.select1evolutioncard` | Select an Evolution card. |
| `playmat.prompt.select1exor3basicpokemon` | Select 1 Basic Pokémon-<i>EX</i> or 3 Basic Pokémon that aren't Pokémon-<i>EX</i> to put onto your Bench. |
| `playmat.prompt.select1fairyenergy` | Select a {Y} Energy card. |
| `playmat.prompt.select1fossilcard` | Select 1 card with Fossil in the name. |
| `playmat.prompt.select1item` | Select 1 Item card. |
| `playmat.prompt.select1lightningenergy` | Select a {L} Energy card. |
| `playmat.prompt.select1plasmaenergycard` | Select a Plasma Energy card. |
| `playmat.prompt.select1specialenergy` | Select a Special Energy card. |
| `playmat.prompt.select1teamplasmacard` | Select a Team Plasma card. |
| `playmat.prompt.select1teamplasmapokemon` | Select a Team Plasma Pokémon. |
| `playmat.prompt.select1teamplasmatrainercard` | Select a Team Plasma Trainer card. |
| `playmat.prompt.select1uniquebasicenergy` | Select a unique basic Energy card. |
| `playmat.prompt.select1waterenergy` | Select a {W} Energy card. |
| `playmat.prompt.select2basicenergy` | Select 2 basic Energy cards. |
| `playmat.prompt.select2basicenergycards` | Select 2 basic Energy cards. |
| `playmat.prompt.select2basicgrasspokemonforbench` | Choose up to 2 Basic {G} Pokémon to put onto your Bench. |
| `playmat.prompt.select2basicmagmapokemon` | Select 2 Basic Team Magma Pokémon. |
| `playmat.prompt.select2basicpokemon` | Select 2 Basic Pokémon. |
| `playmat.prompt.select2cards` | Choose two cards to put into your hand. |
| `playmat.prompt.select2enemybenchedpokemon` | Select 2 of your opponent's Benched Pokémon. |
| `playmat.prompt.select2energy` | Select 2 Energy cards. |
| `playmat.prompt.select2energydarkness` | Select 2 {D} Energy cards. |
| `playmat.prompt.select2energyfighting` | Select 2 {F} Energy cards. |
| `playmat.prompt.select2energyfire` | Select 2 {R} Energy cards. |
| `playmat.prompt.select2fossilcard` | Select 2 cards with Fossil in the name. |
| `playmat.prompt.select2items` | Select 2 Item cards. |
| `playmat.prompt.select2playerbenchedpokemon` | Select 2 of your Benched Pokémon. |
| `playmat.prompt.select2pokemon` | Select 2 Pokémon. |
| `playmat.prompt.select2pokemongrass` | Select 2 {G} Pokémon. |
| `playmat.prompt.select2pokemonwater` | Select 2 {W} Pokémon. |
| `playmat.prompt.select2poketoolcards` | Select 2 Pokémon Tool cards. |
| `playmat.prompt.select2specialenergy` | Select 2 Special Energy cards. |
| `playmat.prompt.select2stadium` | Choose 2 Stadium cards. |
| `playmat.prompt.select2supportercard` | Select 2 Supporter cards. |
| `playmat.prompt.select3basicenergy` | Select 3 basic Energy cards. |
| `playmat.prompt.select3basicpokemon` | Select 3 Basic Pokémon. |
| `playmat.prompt.select3energyfire` | Select 3 {R} Energy cards. |
| `playmat.prompt.select3energygrass` | Select 3 {G} Energy. |
| `playmat.prompt.select3energywater` | Select 3 {W} Energy cards. |
| `playmat.prompt.select3opponentcards` | Select three of your opponent's cards. |
| `playmat.prompt.select3pokemon` | Select 3 Pokémon. |
| `playmat.prompt.select3poketoolcards` | Select 3 Pokémon Tool cards. |
| `playmat.prompt.select4basicenergy` | Select 4 basic Energy cards. |
| `playmat.prompt.select5toreplace` | Rearrange the top 5 cards of your deck by selecting each card in order, starting with the one you want on the bottom. |
| `playmat.prompt.select5toreplaceopponent` | Select cards to return to the top of the deck. |
| `playmat.prompt.select5toreplaceshort` | Select cards to return to the top of the deck. |
| `playmat.prompt.selectabasicpokemon` | Select a Basic Pokémon. |
| `playmat.prompt.selectabenchedcolorlesspokemon` | Select a {C} Pokémon on your Bench. |
| `playmat.prompt.selectabencheddarknesspokemon` | Select a {D} Pokémon on your Bench. |
| `playmat.prompt.selectabenchedfightingpokemon` | Select a {F} Pokémon on your Bench. |
| `playmat.prompt.selectabenchedfirepokemon` | Select a {R} Pokémon on your Bench. |
| `playmat.prompt.selectabenchedgrasspokemon` | Select a {G} Pokémon on your Bench. |
| `playmat.prompt.selectabenchedlightningpokemon` | Select a {L} Pokémon on your Bench. |
| `playmat.prompt.selectabenchedmetalpokemon` | Select a {M} Pokémon on your Bench. |
| `playmat.prompt.selectabenchedpokemon` | Select a Benched Pokémon. |
| `playmat.prompt.selectabenchedpsychicpokemon` | Select a {P} Pokémon on your Bench. |
| `playmat.prompt.selectabenchedwaterpokemon` | Select a {W} Pokémon on your Bench. |
| `playmat.prompt.selectacard` | Select a card. |
| `playmat.prompt.selectacardtodraw` | Select a card to put into your hand. |
| `playmat.prompt.selectacardtomovetolostzone` | Select a card to put in the Lost Zone. |
| `playmat.prompt.selectadeck` | Select a deck. |
| `playmat.prompt.selectadestinationdamagetopokemon` | Choose a Pokémon to move the damage counters to. |
| `playmat.prompt.selectadestinationenergytopokemon` | Select a Pokémon to move the Energy to. |
| `playmat.prompt.selectadestinationplacedamageonpokemon` | Select a Pokémon to put the damage counters on. |
| `playmat.prompt.selectagrassenergy` | You may attach up to 2 {G} Energy cards to Benched Pokémon to heal all damage from them. |
| `playmat.prompt.selectahurtpokemon` | Select a Pokémon that has any damage counters on it. |
| `playmat.prompt.selectandplaylegend` | Select one half of the Pokémon LEGEND you would like to play. |
| `playmat.prompt.selectanenemybenchedpokemon` | Select an opponent's Benched Pokémon. |
| `playmat.prompt.selectanenemyevolvedpokemon` | Select an opponent's evolved Pokémon. |
| `playmat.prompt.selectanenemypokemon` | Select an opponent's Pokémon. |
| `playmat.prompt.selectanenergytomovetolostzone` | Select an Energy card to put in the Lost Zone. |
| `playmat.prompt.selectanevolvedpokemon` | Select an evolved Pokémon. |
| `playmat.prompt.selectapokemon` | Select a Pokémon. |
| `playmat.prompt.selectapokemoncolorless` | Select a {C} Pokémon. |
| `playmat.prompt.selectapokemondarkness` | Select a {D} Pokémon. |
| `playmat.prompt.selectapokemondragon` | Select a {N} Pokémon. |
| `playmat.prompt.selectapokemonfighting` | Select a {F} Pokémon. |
| `playmat.prompt.selectapokemonfire` | Select a {R} Pokémon. |
| `playmat.prompt.selectapokemongrass` | Select a {G} Pokémon. |
| `playmat.prompt.selectapokemonlightning` | Select a {L} Pokémon. |
| `playmat.prompt.selectapokemonmetal` | Select a {M} Pokémon. |
| `playmat.prompt.selectapokemonorbasicenergy` | Select a Pokémon or basic Energy card. |
| `playmat.prompt.selectapokemonpsychic` | Select a {P} Pokémon. |
| `playmat.prompt.selectapokemonrestored` | Select a Restored Pokémon. |
| `playmat.prompt.selectapokemonretreatcost3ormore` | Select a Pokémon with a Retreat Cost of 3 or more. |
| `playmat.prompt.selectapokemontoevolve` | Select a Pokémon to evolve. |
| `playmat.prompt.selectapokemontoheal` | Select a Pokémon to heal. |
| `playmat.prompt.selectapokemontomovetolostzone` | Select a Pokémon to put in the Lost Zone. |
| `playmat.prompt.selectapokemontype` | Choose a type. |
| `playmat.prompt.selectapokemonwater` | Select a {W} Pokémon. |
| `playmat.prompt.selectapokemonwithenergy` | Select a Pokémon that has Energy attached to it. |
| `playmat.prompt.selectapoketoolcard` | Select a Pokémon Tool card. |
| `playmat.prompt.selectaprizecard` | Choose a Prize card. |
| `playmat.prompt.selectarandomcardfromopponenthand` | Select a card at random from your opponent's hand. |
| `playmat.prompt.selectaspecialcondition` | Choose a Special Condition. |
| `playmat.prompt.selectastadiumcard` | Select a Stadium card. |
| `playmat.prompt.selectastage2pokemon` | Select a Stage 2 Pokémon. |
| `playmat.prompt.selectasupportercard` | Select a Supporter card. |
| `playmat.prompt.selectatrainercard` | Select a Trainer card. |
| `playmat.prompt.selectattack` | Select Attack |
| `playmat.prompt.selectbasicenergy` | Select basic Energy cards. |
| `playmat.prompt.selectbasicenergydarkness` | Select a basic {D} Energy card. |
| `playmat.prompt.selectbasicenergymetal` | Select a basic {M} Energy card. |
| `playmat.prompt.selectbasicenergytodiscard` | Select a basic Energy card to discard. |
| `playmat.prompt.selectbasicpokemoncards` | Select Basic Pokémon cards. |
| `playmat.prompt.selectcardtodiscard` | Select a card to discard. |
| `playmat.prompt.selectdevolvepokemon` | Choose a Pokémon to devolve. |
| `playmat.prompt.selectdiscardtoreturn` | Choose a card from your discard pile to put into your hand. |
| `playmat.prompt.selecteeveeevolution` | Select a unique Evolution of Eevee. |
| `playmat.prompt.selectenergy` | Select Energy cards for {0} Energy cost and click OK. |
| `playmat.prompt.selectenergycolorless` | Select a {C} Energy card. |
| `playmat.prompt.selectenergydarkness` | Select a {D} Energy card. |
| `playmat.prompt.selectenergyfighting` | Select a {F} Energy card. |
| `playmat.prompt.selectenergyfightingorfire` | Select a {F} or {R} Energy card. |
| `playmat.prompt.selectenergyfire` | Select a {R} Energy card. |
| `playmat.prompt.selectenergyfireorfighting` | Once during each player's turn, that player may discard a {R} or {F} Energy card from their hand to draw 2 cards. |
| `playmat.prompt.selectenergygrass` | Select a {G} Energy card. |
| `playmat.prompt.selectenergymetal` | Select a {M} Energy card. |
| `playmat.prompt.selectenergypsychic` | Select a {P} Energy card. |
| `playmat.prompt.selectenergytodiscard` | Select the Energy to discard. |
| `playmat.prompt.selectenergytodiscard.1` | Select the Energy to discard (1 Energy required). |
| `playmat.prompt.selectenergytodiscard.2` | Select the Energy to discard (2 Energy required). |
| `playmat.prompt.selectenergytodiscard.3` | Select the Energy to discard (3 Energy required). |
| `playmat.prompt.selectenergytodiscard.4` | Select the Energy to discard (4 Energy required). |
| `playmat.prompt.selectfireenergytodiscard` | Select a {R} Energy to discard. |
| `playmat.prompt.selectgrassenergytodiscard` | Select a {G} Energy to discard. |
| `playmat.prompt.selectitemtodiscard` | Select an Item card to discard. |
| `playmat.prompt.selectitemtoreturntoopponentdeck` | Select an Item card to shuffle into your opponent's deck. |
| `playmat.prompt.selectlightningenergy` | Select {L} Energy cards. |
| `playmat.prompt.selectlimitcards` | Select up to {0} cards and click OK. |
| `playmat.prompt.selectmultiplecards` | Select {0} cards and click OK. |
| `playmat.prompt.selectmultipleenergy` | Select your Energy cards. |
| `playmat.prompt.selectnpokemonorbasicenergy` | Select Pokémon or basic Energy cards. |
| `playmat.prompt.selectnstage2pokemon` | Select the Stage 2 Pokémon. |
| `playmat.prompt.selectopponentcard` | Select one of your opponent's cards. |
| `playmat.prompt.selectpokemontodiscard` | Select the Pokémon to discard. |
| `playmat.prompt.selectpokemontomovetolostzone` | Select the Pokémon to put in the Lost Zone. |
| `playmat.prompt.selectpokemontoreturntodeck` | Select a Pokémon to shuffle into its owner's deck. |
| `playmat.prompt.selectpokemontoreturntohand` | Select a Pokémon to return to your hand. |
| `playmat.prompt.selectpokemontoreturntoownershand` | Select a Pokémon to return to its owner's hand. |
| `playmat.prompt.selectpregamecard` | Select a card to play before the game starts. |
| `playmat.prompt.selectrangecards` | Select {0} to {1} cards and click OK. |
| `playmat.prompt.selectspecialenergytodiscard` | Choose a Special Energy to discard. |
| `playmat.prompt.selectteamplasmacardtodiscard` | Select a Team Plasma card to discard. |
| `playmat.prompt.selectthreecards` | Select 3 cards. |
| `playmat.prompt.selecttools` | Select Pokémon Tool cards. |
| `playmat.prompt.selecttwocards` | Select 2 cards. |
| `playmat.prompt.selecttwoopponentcards` | Select 2 of your opponent's cards. |
| `playmat.prompt.selectupto2basicenergycards` | Select up to 2 basic Energy cards. |
| `playmat.prompt.selectupto2basicpokemon` | Select up to 2 Basic Pokémon cards. |
| `playmat.prompt.selectupto2pokemon` | Select up to 2 Pokémon cards. |
| `playmat.prompt.selectupto2tools` | Select up to 2 Pokémon Tool cards. |
| `playmat.prompt.selectupto3basicenergycards` | Select up to 3 basic Energy cards. |
| `playmat.prompt.selectupto3colorlesspokemon` | Select up to 3 {C} Pokémon. |
| `playmat.prompt.selectupto3darknessenergy` | Select up to 3 {D} Energy cards. |
| `playmat.prompt.selectupto3energycards` | Select up to 3 basic Energy cards. |
| `playmat.prompt.selectupto3fightingenergy` | Select up to 3 {F} Energy cards. |
| `playmat.prompt.selectupto3fireenergy` | Select up to 3 {R} Energy cards. |
| `playmat.prompt.selectupto3grassenergy` | Select up to 3 {G} Energy cards. |
| `playmat.prompt.selectupto3lightningenergy` | Select up to 3 {L} Energy cards. |
| `playmat.prompt.selectupto3magmapokemon` | Select up to 3 Team Magma Pokémon. |
| `playmat.prompt.selectupto3metalenergy` | Select up to 3 {M} Energy cards. |
| `playmat.prompt.selectupto3pokemonex` | Choose up to 3 Pokémon-<i>EX</i> to put into your hand. |
| `playmat.prompt.selectupto3psychicenergy` | Select up to 3 {P} Energy cards. |
| `playmat.prompt.selectupto3waterenergy` | Select up to 3 {W} Energy cards. |
| `playmat.prompt.selfdefeat` | Are you sure you want to do this? By doing this, you may lose the game. |
| `playmat.prompt.sendtobench.customchoicedialog` | Have your opponent switch their Active Pokémon with 1 of their Benched Pokémon? |
| `playmat.prompt.singlecardonbottomofdeck` | Choose a card to put on the bottom of your deck. |
| `playmat.prompt.singlecardonbottomofdeck.opponent` | Choose a card to put on the bottom of your opponent's deck. |
| `playmat.prompt.sl_40.legendaryguidance` | Choose up to 2 Energy cards to attach to your Pokémon. |
| `playmat.prompt.stadium.orientation` | Choose which direction you want this Stadium to face. The bottom faces you. |
| `playmat.prompt.stadium.scorchedearth` | Choose a {R} or {F} Energy card to discard. |
| `playmat.prompt.stadiumcard` | Use Stadium card |
| `playmat.prompt.startingcoinflip.callplayer1` | Call the Coin Flip |
| `playmat.prompt.startingcoinflip.callplayer2` | {0} Is Calling the Coin Flip |
| `playmat.prompt.startingcoinflip.cannotattack` | The first player cannot attack on their first turn. |
| `playmat.prompt.startingcoinflip.opponentchoose` | {0} Is Choosing Who Goes First |
| `playmat.prompt.startingcoinflip.playerchoose` | Would You Like to Go First? |
| `playmat.prompt.startingcoinflip.youlost` | You Lost the Coin Flip |
| `playmat.prompt.startingcoinflip.youwon` | You Won the Coin Flip |
| `playmat.prompt.steven.combined` | Choose a Supporter card and a basic Energy card to put into your hand. |
| `playmat.prompt.takeprize` | Take Prize Card |
| `playmat.prompt.teamaquasgreatball.combined` | Choose a Basic Team Aqua Pokémon and a basic {W} Energy card to put into your hand. |
| `playmat.prompt.teammagmasgreatball.combined` | Choose a Basic Team Magma Pokémon and a basic {F} Energy card to put into your hand. |
| `playmat.prompt.toomanypokemontools` | You have too many Pokémon Tools on this Pokémon. Please select the Pokémon Tool(s) to discard. |
| `playmat.prompt.topcardofthedeck` | This is the top card of the deck you selected. |
| `playmat.prompt.tormentingspray.opponent` | This is the revealed card from your hand. If it's a Supporter card, it will be discarded. |
| `playmat.prompt.tormentingspray.player` | This is the revealed card from your opponent's hand. If it's a Supporter card, it will be discarded. |
| `playmat.prompt.trainer.timerball` | Choose an Evolution Pokémon to put into your hand for each heads you flipped. |
| `playmat.prompt.trainercards.levelball` | Select a Pokémon with 90 HP or less. |
| `playmat.prompt.transferjunk.combined` | Choose a Team Plasma Pokémon, a Team Plasma Trainer card, and a Team Plasma Energy card to put into your hand. |
| `playmat.prompt.wait` | Wait |
| `playmat.prompt.wishfulbaton` | Choose up to 3 basic Energy cards to move to one of your Benched Pokémon. |
| `playmat.prompt.xerneas.geomancy` | Select a {Y} Energy to attach to this Pokémon. |
| `playmat.prompt.xybsp_153.stoke` | Choose up to 2 {R} Energy cards to attach to this Pokémon. |
| `playmat.prompt.xyp_17.megaascension` | Choose a Mega Charizard-<i>EX</i> to put into your hand. |
| `playmat.prompt.xyp_55.mudflood` | These are the top 4 cards of your deck. This attack will do 40 more damage for each revealed {W} Energy. |
| `playmat.prompt.xyp_55.mudflood.opponent` | These are the top 4 cards of your opponent's deck. This attack will do 40 more damage for each revealed {W} Energy. |
| `playmat.prompt.xyp_69.megaascension` | Choose a Mega Rayquaza-<i>EX</i> to put into your hand. |
| `playmat.prompts.bw9_90.signsofevolution.combined` | Choose 3 Pokémon of different types that evolve from Eevee to put into your hand. |
| `playmat.prompty.sm5_49.snugglygenerator` | Choose a {L} Energy card to attach to each of your Benched Pokémon that has the Nuzzle attack. |


---

## Appendix A - message inventory

Generated from the `[JsonName]` attributes in the de-obfuscated sources. Field order is declaration order. `[dead]` marks a class with no `[MessageCommandConstructor]` anywhere (see §3.3).

### A.0 Entity messages (these live in `sausage-core`, not `core`)

They are not in the generated tables below because the tables were built from
`core.dll` and `pie-src.dll` only. All five are LIVE — `sausage-core` registers
exactly these five `[MessageCommandConstructor]`s, and `pie-src` overrides all
of them with `IsOverride = true`.

| class | where | fields |
|---|---|---|
| **EntityIntroduced** | `sausage.txt:1659` | `attributeMap`:ReadOnlyAttributes, `entityID`:EntityID, `entityName`:string — pie override `m.H` `pie_d.cs:143366` |
| **EntityAdded** | `sausage.txt:1631` | `entityID`:EntityID, `owningPlayerID`:AccountID, `parentEntityID`:EntityID — pie override `m.F` `pie_d.cs:143256` |
| **EntityDestroyed** | `sausage.txt:1648` | `entityID`:EntityID — pie override `m.G` `pie_d.cs:143303` |
| **AttributeModified** | `sausage.txt:3215` | `entityID`:EntityID, `attribute`:BaseAttribute — pie override `L.S` `pie_d.cs:136779` |
| **AttributeRemoved** | `sausage.txt:3229` | `entityID`:EntityID, `attributeName`:AttributeDefinition — pie override `L.T` `pie_d.cs:136941` |

`SequenceMessage` itself is `core_d.cs:70021`:
`{sequenceID: SequenceID, msg: IGameMessage}` — `msg` uses the
`{"name":…,"value":…}` envelope (§1.1).

### A.1 `GameMessage` subclasses (top-level game messages)

| class | where | fields (`[JsonName]` -> type) |
|---|---|---|
| **ActivePlayerSet** | `core_d.cs:11413` | `accountID`:AccountID |
| **AttributesReset** | `core_d.cs:11419` | `entityID`:EntityID |
| **CakeRevealOpened** | `pie_d.cs:123025` | `revealID`:Guid, `entityIDs`:GuidDictionary<ReadOnlyAttributes>, `revealSource`:EntityID, `revealTypes`:string, `revealTitle`:LocalizableText |
| **CustomChoiceOfferMessage** | `pie_d.cs:65605` | `selectingPlayer`:AccountID, `prompt`:LocalizableText, `buttons`:LocalizableText[], `offerLength`:long, `sourceEntity`:EntityID, `selection`:int?, `correctChoice`:int? |
| **DematerializeEntity** `[dead]` | `core_d.cs:89933` | `entityID`:EntityID, `destinationID`:EntityID, `positionInParent`:int? |
| **EffectPlayed** | `core_d.cs:11589` | `effectMessage`:EffectMessage |
| **EntityMoved** | `core_d.cs:11600` | `entityID`:EntityID, `destinationID`:EntityID, `positionInParent`:int, `animDuration`:int |
| **EntityMovedWithoutID** `[dead]` | `core_d.cs:89961` | `sourceID`:EntityID, `destinationID`:EntityID, `positionInParent`:int? |
| **GameCompletedMessage** | `pie_d.cs:122686` | `coins`:int, `exp`:int, `share`:bool, `endOfGameText`:LocalizableText, `rewardList`:global::A.N[], `winner`:string, `loser`:string, `additionalParameters`:Dictionary<string, LocalizableText> |
| **GameDoesNotExist** | `core_d.cs:11620` | *(none)* |
| **GameEnded** `[dead]` | `core_d.cs:70446` | `winnerList`:IList<AccountID>, `loserMap`:Dictionary<AccountID, string>, `draw`:bool |
| **MapEntityID** `[dead]` | `core_d.cs:89989` | `oldID`:EntityID, `newID`:EntityID |
| **MaterializeEntity** `[dead]` | `core_d.cs:90003` | `entityID`:EntityID, `entityName`:string, `attributeMap`:ReadOnlyAttributes, `owningPlayerID`:AccountID, `sourceID`:EntityID, `destinationID`:EntityID, `positionInParent`:int, `animDuration`:int, `children`:MaterializeEntity[], `presentationPile`:EntityID |
| **ObserverCustomChoiceOfferMessage** | `pie_d.cs:10675` | `selectingPlayer`:AccountID, `prompt`:LocalizableText, `buttons`:LocalizableText[], `offerLength`:long, `sourceEntity`:EntityID, `selection`:int?, `correctChoice`:int? |
| **PlayerDisconnected** | `core_d.cs:11629` | `accountID`:AccountID, `waitTime`:int |
| **PlayerReconnected** | `core_d.cs:11638` | `accountID`:AccountID |
| **PlayerStillInGame** | `core_d.cs:70559` | *(none)* |
| **Replay** | `core_d.cs:70566` | `queueName`:string |
| **RevealClosed** | `core_d.cs:11644` | `revealID`:Guid |
| **RevealOpened** `[dead]` | `core_d.cs:70550` | `revealID`:Guid, `entityIDs`:Dictionary<EntityID, ReadOnlyAttributes> |
| **RunClientAction** | `core_d.cs:35993` | `clientAction`:ClientAction |
| **SelectionFinished** `[dead]` | `core_d.cs:11650` | *(none)* |
| **SelectionMessage** | `core_d.cs:73924` | `counter`:int, `prompt`:LocalizableTextVariables, `offerLength`:long, `startingTimestamp`:long |
| **SerializedGameState** | `core_d.cs:70535` | `entities`:SerializedEntity, `playerAccounts`:AccountID[], `gameOptions`:Dictionary<string, string> |
| **Shuffled** | `core_d.cs:11654` | `entityID`:Guid |
| **StartSequence** | `core_d.cs:70047` | `sequenceID`:SequenceID, `name`:string, `attributes`:ReadOnlyAttributes |
| **StopSequence** | `core_d.cs:70059` | `sequenceID`:SequenceID, `name`:string |
| **SuddenDeathGameMessage** | `pie_d.cs:123019` | `suddenDeathRound`:int |

### A.2 `SelectionMessage` subclasses (offers)

| class | where | fields (`[JsonName]` -> type) |
|---|---|---|
| **ArchetypeCustomChoiceRequired** | `core_d.cs:10899` | `buttons`:List<ReadOnlyAttributes>, `sourceEntity`:EntityID, `selectionParams`:Dictionary<string, object> |
| **CakeSelectionWithTargetsRequired** | `pie_d.cs:123049` | `prompt`:LocalizableTextVariables |
| **CoinFlipChoiceRequired** | `pie_d.cs:122431` | `sortType`:string, `buttons`:LocalizableTextVariables[], `sourceEntity`:EntityID |
| **CustomChoiceRequired** | `core_d.cs:15778` | `sortType`:string, `buttons`:LocalizableTextVariables[], `sourceEntity`:EntityID, `kind`:string |
| **CustomChoiceWithTargetsRequired** | `core_d.cs:73725` | `forced`:bool, `ignoreFirst`:bool, `choiceKind`:string, `choices`:List<SerializedEntity>, `choiceTargets`:List<TargetInformation[]>, `optimalChoices`:List<TargetPreference>, `sourceEntity`:EntityID, `selectionParams`:Dictionary<string, object> |
| **GetXCostChoiceRequired** | `core_d.cs:15793` | `lowBound`:int, `highBound`:int |
| **GoFirstChoiceRequired** | `pie_d.cs:116784` | `sortType`:string, `buttons`:LocalizableTextVariables[], `sourceEntity`:EntityID |
| **MulliganChoiceRequired** `[dead]` | `core_d.cs:10917` | `mulliganRound`:int, `maxMulligans`:int, `cardsDrawn`:int |
| **MultipleSelectionWithTargetsRequired** | `core_d.cs:15802` | `forcedTargets`:EntityID[], `forcedSecondStageTargets`:EntityID[], `targetMap`:Dictionary<EntityID, MultipleTargetInformation>, `forced`:bool |
| **ParameterizedLocCustomChoiceRequired** | `pie_d.cs:15136` | `buttons`:LocalizableTextVariables[] |
| **SelectionWithTargetsAndActionsRequired** | `core_d.cs:73965` | *(none)* |
| **SelectionWithTargetsRequired** | `core_d.cs:15817` | `targetMap`:Dictionary<EntityID, TargetInformation[]>, `optimalPlayMap`:Tuple<EntityID, TargetPreference>[], `forced`:bool, `targetType`:string, `ignoreFirst`:bool, `selectionParams`:Dictionary<string, object>, `sourceID`:EntityID |

### A.3 `EffectMessage` subclasses (carried inside `EffectPlayed`)

| class | where | fields (`[JsonName]` -> type) |
|---|---|---|
| **AIInfoMessage** | `core_d.cs:76039` | `msg`:string |
| **AbilityFinishedEffect** | `pie_d.cs:122443` | `eID`:EntityID |
| **AbilityPlayedEffect** | `pie_d.cs:116769` | `eID`:EntityID, `abilityID`:Guid, `abilityTitle`:LocalizableText, `abilityType`:AbilityType |
| **AddSpecialConditionEffect** `[dead]` | `pie_d.cs:122449` | `condition`:SpecialConditions, `source`:EntityID, `targets`:EntityID[] |
| **AnimationDelayEffect** `[dead]` | `core_d.cs:11430` | `duration`:int |
| **AnimationDelayEffectV2** `[dead]` | `core_d.cs:352` | `duration`:float |
| **BlinkEffect** `[dead]` | `core_d.cs:11436` | `entityID`:Guid |
| **BurnEffect** `[dead]` | `pie_d.cs:122461` | *(none)* |
| **CakeAbilitySelectedEffect** | `pie_d.cs:122465` | `entityID`:EntityID, `abilityName`:LocalizableText, `abilityType`:LocalizableText |
| **CakeAttackEffect** | `pie_d.cs:122477` | `damageSource`:EntityID, `entityID`:EntityID, `weaknessTriggered`:bool, `resistanceTrigger`:bool, `damageType`:string[], `attackName`:LocalizableText, `damageAmount`:int, `damageModification`:int, `visualType`:n.j.InteractionVisualizations |
| **CleanupAttackEffect** | `pie_d.cs:14387` | `entityID`:EntityID, `cleanupCurvePrefix`:string |
| **ClearDefendEffect** `[dead]` | `core_d.cs:11442` | `defenderID`:EntityID, `attackerID`:EntityID, `effectType`:string |
| **ClosePauseOnPromptEffect** | `pie_d.cs:21905` | *(none)* |
| **ConfuseEffect** `[dead]` | `pie_d.cs:122507` | *(none)* |
| **CreatureHealEvent** `[dead]` | `pie_d.cs:122511` | *(none)* |
| **CreatureHealWithContextEvent** | `pie_d.cs:122515` | `source`:EntityID, `targets`:EntityID[], `amount`:int |
| **DefendEffect** `[dead]` | `core_d.cs:11454` | `defenderID`:EntityID, `attackerID`:EntityID, `effectType`:string |
| **DoneWaitingForOpponentEffect** `[dead]` | `pie_d.cs:122527` | *(none)* |
| **DrawFromBottom** | `pie_d.cs:203414` | `playerID`:EntityID, `numOfTargets`:int |
| **EnergySwapEffect** | `pie_d.cs:226247` | `source`:EntityID, `targets`:EntityID[] |
| **EvolveEffect** `[dead]` | `pie_d.cs:122531` | *(none)* |
| **EvolveWithContextEffect** | `pie_d.cs:122535` | `source`:EntityID, `targets`:EntityID[] |
| **GXAttackUsedEffect** | `pie_d.cs:14396` | `user`:AccountID |
| **GameEffectMessage** `[dead]` | `core_d.cs:11487` | `sourceID`:EntityID, `targets`:EntityID[], `msg`:EffectMessage |
| **GameLogMessage** `[dead]` | `core_d.cs:11499` | *(none)* |
| **HealWithContextEvent** `[dead]` | `pie_d.cs:122544` | `source`:EntityID, `targets`:EntityID[] |
| **KeyValueDataEffect<T>** | `core_d.cs:70219` | `key`:string, `value`:T |
| **ModifyStackDisplay** `[dead]` | `core_d.cs:11526` | `entityID`:Guid, `targets`:Guid[] |
| **MulliganRevealCardsEffect** | `pie_d.cs:14402` | `player`:AccountID, `entityIDPiles`:List<Dictionary<EntityID, ReadOnlyAttributes>>, `prompt`:LocalizableText, `revealTitle`:LocalizableText, `revealSource`:EntityID |
| **MultipleCoinFlipEffect** `[dead]` | `pie_d.cs:65629` | `resultLst`:int[], `title`:LocalizableText, `gameText`:LocalizableText, `source`:EntityID |
| **MultipleCoinFlipWithContextEffect** | `pie_d.cs:123001` | `resultLst`:int[], `title`:LocalizableText, `source`:EntityID, `targets`:EntityID[], `gameText`:LocalizableText |
| **NonDamagingTargetsEffect** | `pie_d.cs:29928` | `targets`:EntityID[] |
| **ParalyzeEffect** `[dead]` | `pie_d.cs:122553` | *(none)* |
| **PauseOnPromptEffect** | `pie_d.cs:65644` | `buttonText`:LocalizableText, `prompt`:LocalizableTextVariables, `doPause`:bool |
| **PhaseChangeEffect** `[dead]` | `core_d.cs:11535` | `phase`:string, `duration`:int |
| **PileReordered** | `pie_d.cs:7349` | `entityID`:Guid, `children`:Guid[] |
| **PlaceDamageEffect** | `pie_d.cs:122557` | `destinationID`:EntityID, `originID`:EntityID, `amount`:int, `abilityName`:LocalizableText |
| **PlaceOnBottom** | `pie_d.cs:203423` | `entityID`:EntityID, `target`:EntityID |
| **PoisonEffect** `[dead]` | `pie_d.cs:122572` | *(none)* |
| **PostActionPhaseEffect** | `pie_d.cs:29934` | *(none)* |
| **PromptMessage** `[dead]` | `core_d.cs:11544` | `message`:LocalizableText, `blocking`:bool |
| **PushStackDisplay** `[dead]` | `core_d.cs:11553` | `entityID`:Guid, `targets`:Guid[] |
| **RemoveBurnEffect** `[dead]` | `pie_d.cs:122576` | *(none)* |
| **RemoveConfuseEffect** `[dead]` | `pie_d.cs:122580` | *(none)* |
| **RemoveParalyzeEffect** `[dead]` | `pie_d.cs:122584` | *(none)* |
| **RemovePoisonEffect** `[dead]` | `pie_d.cs:122588` | *(none)* |
| **RemoveSleepEffect** `[dead]` | `pie_d.cs:122592` | *(none)* |
| **RemoveSpecialConditionEffect** `[dead]` | `pie_d.cs:122596` | `condition`:SpecialConditions, `source`:EntityID, `targets`:EntityID[] |
| **RemoveStackDisplay** `[dead]` | `core_d.cs:11562` | `entityID`:Guid |
| **RevealCardToAllEffect** | `pie_d.cs:65656` | `entityID`:Guid, `Return`:bool, `alwaysReveal`:bool |
| **RevealCardsToAllEffect** | `pie_d.cs:122608` | `entityID`:EntityID[], `playerPrompt`:Dictionary<AccountID, LocalizableText>, `revealTitle`:LocalizableText, `revealSource`:EntityID, `prompt`:LocalizableText |
| **RevealCardsToPlayerEffect** | `pie_d.cs:122626` | `player`:AccountID, `entityID`:EntityID[], `prompt`:LocalizableTextVariables, `revealTitle`:LocalizableText, `revealSource`:EntityID |
| **RockPaperScissorsEffect** | `pie_d.cs:122644` | `choices`:Dictionary<AccountID, int?> |
| **ShieldTargetsEffect** | `pie_d.cs:14420` | `source`:EntityID, `targets`:EntityID[], `wasDamage`:bool |
| **SleepEffect** `[dead]` | `pie_d.cs:122670` | *(none)* |
| **SwapDefenderEffect** `[dead]` | `core_d.cs:11568` | `oldDefenderID`:Guid, `newDefenderID`:Guid |
| **VSTARPowerUsedEffect** | `pie_d.cs:203431` | `user`:AccountID |
| **WaitForTargetOffEffect** `[dead]` | `core_d.cs:11577` | `entityID`:Guid |
| **WaitForTargetOnEffect** `[dead]` | `core_d.cs:11583` | `entityID`:Guid |
| **WaitingForOpponentEffect** `[dead]` | `pie_d.cs:122674` | `message`:string, `maxTimeToWait`:int, `selectingAccountID`:Guid |

### A.4 `TargetInformation` subclasses (inline `name` type hint)

| class | where | fields (`[JsonName]` -> type) |
|---|---|---|
| **ActivePokemonTargetInformation** | `pie_d.cs:200149` | *(none)* |
| **AlignedEntityListTargetInformation** | `pie_d.cs:197325` | *(none)* |
| **AndCompositeRevealEntityListTargetInformation** | `pie_d.cs:195698` | `selections`:IList<EntityListTargetInformation>, `ordered`:bool |
| **AnyCompositeRevealEntityListTargetInformation** | `pie_d.cs:195714` | `selections`:IList<EntityListTargetInformation>, `ordered`:bool |
| **ArchetypeCustomChoiceTargetInformation** | `core_d.cs:10928` | `archs`:List<ReadOnlyAttributes>, `random`:bool, `sortType`:string |
| **CakeAttackCustomChoiceTargetInformation** | `pie_d.cs:121230` | `choices`:PieAbilityDescription[] |
| **CompositeEntityListTargetInformation** | `pie_d.cs:15121` | `selections`:IList<EntityListTargetInformation> |
| **CompositeRevealAssociatedEntityListTargetInformation** | `pie_d.cs:195646` | `revealEntities`:Dictionary<EntityID, ReadOnlyAttributes> |
| **CompositeRevealEntityListTargetInformation** | `pie_d.cs:195690` | `selections`:IList<EntityListTargetInformation>, `ordered`:bool |
| **CustomChoiceAsAbilitySelectTargetInformation** | `pie_d.cs:200169` | *(none)* |
| **CustomChoiceAsAbilitySelectTargetInformationWithTAGBonus** | `pie_d.cs:200172` | *(none)* |
| **CustomChoiceTargetInformation** | `core_d.cs:16661` | `sortType`:string, `choices`:LocalizableTextVariables[], `titles`:LocalizableText[] |
| **EnergyCostEntityListTargetInformation** | `pie_d.cs:198855` | `validTargets`:d.l[], `energyRequirements`:Dictionary<PokemonTypes, int>, `forced`:bool, `hintTargetMap`:d.N, `minimumEnergyRequirements`:Dictionary<PokemonTypes, int> |
| **EntityListTargetInformation** | `core_d.cs:16633` | `validTargets`:EntityID[], `numberToSelect`:int, `forced`:bool, `minimumToSelect`:int, `hintTargetMap`:Dictionary<TargetStrength, EntityID[]> |
| **ExclusiveMultiCompositeRevealEntityListTargetInformation** | `pie_d.cs:195722` | `selections`:IList<EntityListTargetInformation>, `exclusions`:Dictionary<EntityID, IList<EntityID>>, `ordered`:bool |
| **InitialBenchedTargetInformation** | `pie_d.cs:200155` | *(none)* |
| **KnockoutPokemonTargetInformation** | `pie_d.cs:200152` | *(none)* |
| **MultiSelectEntityListTargetInformation** | `pie_d.cs:193737` | `amountPerClick`:int |
| **OrCompositeRevealEntityListTargetInformation** | `pie_d.cs:195706` | `selections`:IList<EntityListTargetInformation>, `ordered`:bool |
| **OrEntityListTargetInformation** | `pie_d.cs:199471` | *(none)* |
| **OrientationCustomChoiceTargetInformation** | `pie_d.cs:200166` | *(none)* |
| **PrizeCardTargetInformation** | `pie_d.cs:200158` | `presentPrizesAllowed`:bool, `horizontalLayout`:bool |
| **RetreatCostEntityListTargetInformation** | `pie_d.cs:147746` | `validTargets`:Guid[], `valueToSelect`:int, `accountID`:Guid? |
| **RetreatNewActiveTargetInformation** | `pie_d.cs:200146` | *(none)* |
| **RevealAssociatedEntityListTargetInformation** | `pie_d.cs:15113` | `targets`:q.L[], `revealedCandidates`:List<EntityListTargetInformation> |
| **RevealDetailedEntityListTargetInformation** | `core_d.cs:26405` | `validTargetEntities`:List<SerializedEntity>, `validTargetCounts`:Dictionary<EntityID, int> |
| **RevealEntityListTargetInformation** | `core_d.cs:10939` | `revealEntities`:Dictionary<EntityID, ReadOnlyAttributes> |
| **SlotAssociatedEntityListTargetInformation** | `pie_d.cs:195971` | `targetSlots`:R.d, `slotCandidates`:Dictionary<EntityID, ReadOnlyAttributes> |
| **XTargetInformation** | `core_d.cs:16650` | `forced`:bool, `min`:int, `max`:int |

### A.5 `TargetResponse` subclasses (client -> server)

| class | where | fields (`[JsonName]` -> type) |
|---|---|---|
| **EntityListTargetResponse** | `core_d.cs:16705` | `entityList`:EntityID[], `name`:string |
| **IntTargetResponse** | `core_d.cs:16739` | `amount`:int, `name`:string |
| **MultiSelectEntityListTargetResponse** | `pie_d.cs:193888` | `name`:string, `entities`:List<Q.M> |
| **RevealAssociatedEntityListTargetResponse** | `pie_d.cs:197190` | `name`:string, `associations`:List<R.D> |
| **SlotAssociatedEntityListTargetResponse** | `pie_d.cs:197217` | `name`:string, `target`:EntityID, `associations`:List<q.Q> |


---

## 7. Opening UI - follow-up findings

A second pass over the opening sequence, driven by five questions from live
testing. Everything here was re-read for this pass; where it contradicts §2,
§3 or `CLAUDE.md`, the correction is called out explicitly in §7.6.

### 7.1 The coin: every writer of `player1Coin` / `player2Coin`

The two coins are plain `Animator`s (`PlaymatProvider.player1Coin` /
`player2Coin`, `pie_d.cs:66354`, `66356`). **Four** bools are ever written:
`InitialUp`, `up`, `heads`, `tails`. `InitialUp` and `up` are *different*
parameters — the opening coin is raised with `InitialUp`, in-game flips are
raised with `up`. Nothing that clears one clears the other.

Complete inventory (grep of `player1Coin` / `player2Coin` over `pie_d.cs`,
every hit classified). **VERIFIED** — I read the enclosing class of each.

| line | enclosing class | writes | reachable from |
|---|---|---|---|
| `30934`-`30935` | `d.D`, `[MessageCommandConstructor] D(PostActionPhaseEffect)` (ctor `pie_d.cs:30892`) | `up=false` both | **`PostActionPhaseEffect`** — a server message, no fields |
| `32590`-`32593` | `d.n`, `[SequenceCommandConstructor("DealInitialHands")]` (`pie_d.cs:32581`) | `InitialUp=false`, `up=false` **both coins** | `StartSequence{name:"DealInitialHands"}` |
| `137205` / `137264` | `L.x`, `[MessageCommandConstructor] x(MultipleCoinFlipWithContextEffect)` (ctor `pie_d.cs:137170`) | `up=true` at start, `up=false` at end, on **one** coin | `EffectPlayed{MultipleCoinFlipWithContextEffect}` — but see the short-circuit below |
| `137236` / `137252` | same | `heads` / `tails` per flip | same |
| `140682` | `l.V`, the `"CoinFlipChoice"` selection command (`pie_d.cs:140648`) | `player1Coin.InitialUp=true` | `CoinFlipChoiceRequired` |
| `141087`-`141090` | `l.Z`, `[SequenceCommandConstructor("ActivePlayerSet")]` (`pie_d.cs:141074`) | `heads=false`, `tails=false` both — **only if `model.A` (sudden death) is set** | `StartSequence{name:"ActivePlayerSet"}` |
| `141092`-`141093` | same | `InitialUp=false` **both coins**, unconditionally | same |
| `141455`-`141456` | `l.h`, `[SequenceCommandConstructor("InitialCoinFlip")]` (`pie_d.cs:141441`) | `heads` **or** `tails` `=true` on **both** coins | `StartSequence{name:"InitialCoinFlip"}` |
| `142408`/`142417` | `l.Q`, `[SequenceCommandConstructor("OpponentPickingHeadsOrTails")]` (`pie_d.cs:142402`) | `player2Coin.InitialUp=true` | that sequence, **only when it has ≥1 child** |
| `143205`-`143217` | `m.d`, `[MessageCommandConstructor] d(ForceSelectionFinished)` (`pie_d.cs:143180`) | would clear `heads`/`InitialUp`/`up` — **DEAD, see below** | — |
| `144861`-`144864` | `m.y`, `[MessageCommandConstructor] y(SuddenDeathGameMessage)` (ctor `pie_d.cs:144852`) | `heads=false`, `tails=false` both | `SuddenDeathGameMessage` |
| `149108`/`149112` | match-init | `PieCoin.LoadFromArchetypeID` — cosmetic skin only | match model load |

**So exactly two live writers set `InitialUp` back to false: the
`DealInitialHands` sequence and the `ActivePlayerSet` sequence.** Nothing else
lowers the opening coin.

#### The answer: an empty `ActivePlayerSet` sequence

`l.Z.executeSequence` (`pie_d.cs:141079`) does all of its work **before** it
runs its children, and its children may be an empty list:

```csharp
initialCoinFlipAnimator.SetBool("DonePicking", false);
initialCoinFlipAnimator.SetBool("Hidden", true);
if (playmatProvider.get_model().A) { /* heads=false, tails=false on both */ }
player1Coin.SetBool("InitialUp", false);
player2Coin.SetBool("InitialUp", false);
runAllWithGroupedMoves(WrapSequence(sequence));          // empty -> no-op
initialCoinFlipAnimator.gameObject.SetActive(false);
```

`runAllWithGroupedMoves` is `k.z` (`pie_d.cs:134484`) over the child list; its
`execute()` is a `for` loop over `sequence.Count`, so an empty list completes
on the first frame. **VERIFIED.**

Therefore a bare

```json
{"name":"StartSequence","value":{"sequenceID":"<new guid>","name":"ActivePlayerSet","attributes":{}}}
{"name":"StopSequence","value":{"sequenceID":"<same guid>"}}
```

with **no children at all** is a pure "put the coin away and tear down the
coin-flip dialog" primitive: coin down, dialog `Hidden`, dialog GameObject
deactivated. It does not touch the turn counter, the turn banner or the game
log, because those live in the *message* command, not the sequence (below).
*(VERIFIED that the sequence does this; INFERRED only that an empty child list
is acceptable to the server-side framing — the client side is verified.)*

The `heads`/`tails` clear inside it is gated on `playmatProvider.get_model().A`,
which `m.y` sets true on `SuddenDeathGameMessage` (`pie_d.cs:144857`) — i.e.
**sudden death only**. In a normal game `ActivePlayerSet` lowers the coin but
leaves the face showing on the (now hidden) coin object. Harmless.
*(INFERRED — `model.A` is one of many collapsed `N.f` bools; the
`SuddenDeathGameMessage` handler is the only other writer I found.)*

#### `ActivePlayerSet` the message does NOT touch the coin

`L.Q` (`pie_d.cs:136658`, `[MessageCommandConstructor] Q(ActivePlayerSet)`)
contains **no reference to `player1Coin`, `player2Coin` or
`initialCoinFlipAnimator`**. What it actually does (`execute()`,
`pie_d.cs:136694`):

- sets the active / inactive player flags;
- if the *inactive* player entity is Player 1, calls
  `SelectionUtils.ForceEndSelection()`;
- `turnCounter++` (the double-count `CLAUDE.md` warns about);
- **`model.c = false`** — this is the same flag the mulligan and reveal dialogs
  set, so `ActivePlayerSet` also un-sticks a leaked dialog block (see §7.4);
- game log `StartTurn`, plays `PlayerTurnIndicator` / `OpponentTurnIndicator`
  and waits for the clip;
- flushes the knockout pile.

**VERIFIED.** `CLAUDE.md`'s "ActivePlayerSet hides the coin-flip dialog and
resets both coins" is true of the **sequence** `l.Z` and false of the
**message** `L.Q`. If the server sends the message unwrapped, the coin stays up.

#### `ForceSelectionFinished` does NOT put the coin away

`m.d.execute()` (`pie_d.cs:143186`) reads:

```csharp
if (A.get_selectionNode() != null) { A.ForceEndSelection(); }
if (A.get_selectionNode() != null && Kind == "CoinFlipChoice") { ...coin... }
else if (A.get_selectionNode() != null && Kind == "GoFirstChoice") { ...coin... }
else { promptListener.OverrideShowPrompt = false; OverrideText = null; }
```

`ForceEndSelection()` -> `Selection.EndOffer()` (`pie_d.cs:192280`,
`core_d.cs:72107`) -> `set_CurrentOffer(null)`, and
`Selection.get_CurrentChoice()` returns null when `CurrentOffer` is null
(`core_d.cs:71989`). `SelectionUtils.get_selectionNode()` is exactly
`selection.get_CurrentChoice()` (`pie_d.cs:191667`). **So by the time the
`Kind` tests run, `get_selectionNode()` is always null and both coin branches
are unreachable.** The `else` branch always wins. **VERIFIED** — this is a
client bug, and it means `ForceSelectionFinished` clears the prompt override
but leaves the coin exactly where it was.

**Second hazard in the same command.** `dismissRevealAndWaitForSelectionIfNeeded`
(`pie_d.cs:143228`) spins

```csharp
while (A.get_selectionNode() == null) {
    if (model.A != model.B) { yield return new WaitForSeconds(0.1f); break; }
    yield return null;
}
```

`model.A` is the active-player AccountID and `model.B` the local one (the same
pair `L.x` uses at `pie_d.cs:137177` to pick a coin, and `L.Q` uses at
`pie_d.cs:136710` to pick a turn banner). **If it is the local player's turn and
no selection is outstanding, this loop never exits and the Queued message pump
is dead.** Only send `ForceSelectionFinished` when you know an offer is open.
*(VERIFIED the loop; INFERRED which collapsed field is which AccountID.)*

#### `InitialCoinFlip` deliberately leaves the coin up

`l.h.executeSequence` (`pie_d.cs:141447`) sets `x.A = true` on each child
`MultipleCoinFlipWithContextEffect` command **before** running it. That field is
the guard at the top of `L.x.execute()` (`pie_d.cs:137188`): `if (!this.A)` —
when true the whole animation branch is skipped, which includes the
`a.SetBool("up", false)` cleanup at `pie_d.cs:137264`. Instead the sequence
sets `heads`/`tails` directly on **both** coins and never touches `up` or
`InitialUp`. **VERIFIED.**

Consequence: the opening flip has no cleanup of its own. The coin is up because
`l.V` raised `InitialUp` when `CoinFlipChoiceRequired` arrived
(`pie_d.cs:140682`), and it stays up until `DealInitialHands` or an
`ActivePlayerSet` **sequence** runs.

#### The other three named opening sequences

- **`InitialPick`** (`pie_d.cs:147907`, class `j`) — `executeSequence` is a bare
  `foreach (item in sequence) { while (item.MoveNext()) yield ... }`. **It does
  nothing at all beyond running its children serially.** It does not lower the
  coin. **VERIFIED.**
- **`OpponentChoosingToGoFirst`** (`pie_d.cs:142346`, class `o`) — sets
  `initialCoinFlipAnimator` `OpponentPicksWhoGoesFirst` (or the
  `ObservePlayerNPicksWhoGoesFirst` spectator variants when a child
  `ObserverCustomChoiceOfferMessage` is present), then runs children. **Never
  touches the coins.** Does nothing at all if `sequence.Count == 0`.
- **`OpponentPickingHeadsOrTails`** (`pie_d.cs:142402`, class `Q`) — activates
  the dialog GameObject and **raises** `player2Coin.InitialUp`. It is a *raise*,
  not a lower. Also a no-op when it has no children.

#### `PostActionPhaseEffect` is the only *message* that lowers a coin

`d.D` (`pie_d.cs:30892`) clears the pip tray and sets `up=false` on both coins.
It does **not** touch `InitialUp`, so **it cannot lower the opening coin.** It
is the right cleanup after an in-game flip that was interrupted, and the wrong
tool for the opening. **VERIFIED.**

#### Summary answer

| goal | send |
|---|---|
| lower the opening coin without dealing | empty `StartSequence{name:"ActivePlayerSet"}` / `StopSequence` |
| lower the opening coin as part of the deal | `StartSequence{name:"DealInitialHands"}` (already does it, `pie_d.cs:32590`) |
| lower an in-game (`up`) coin left raised | `EffectPlayed{PostActionPhaseEffect}` |
| anything else | **nothing works** — `InitialPick`, `OpponentChoosingToGoFirst`, `ForceSelectionFinished`, and the bare `ActivePlayerSet` message all leave it up |

### 7.2 Prompt banner suppression — confirmation

`prompt: ""` is correct and safe. Chain, all **VERIFIED**:

1. `LocalizableTextVariablesAnalyzer.Analyze` (`core_d.cs:76082`) takes the
   `Primitive` branch for any non-null string, including `""`, and builds
   `new LocalizableTextVariables("", null)`.
2. `LocalizableText..ctor` (`core_d.cs:76405`) throws **only** on `null`;
   `"".Trim('$')` is `""`.
3. `LocalizableTextVariables.get_DisplayText()` (`core_d.cs:76064`) ->
   `L.LT("")` -> `LocalizationLookup.Localize` (`core_d.cs:76565`), whose first
   branch is `if (get_Disabled() || string.IsNullOrEmpty(key)) value = key;` ->
   returns `""`.
4. `SelectionUtils.CanShowPrompt()` (`pie_d.cs:192338`) therefore fails on
   `!string.IsNullOrEmpty(get_selectionNode().get_Prompt().get_DisplayText())`
   and `PiePromptListener.LateUpdate` (`pie_d.cs:140853`) sets `flag3 = false`,
   which skips the `suppressedKeys` loop entirely and drives
   `promptController.SetBool("Dismiss", true)`.

`prompt: null` also works and reaches the same result one step earlier
(`RootSelection.get_Prompt()` returns `message.Prompt`, `core_d.cs:71333`), but
`""` is the safer choice because it keeps the field a real object for anything
that reads `.ID`.

**No unguarded dereference exists on this path.** I enumerated all 22
`get_Prompt()` call sites in `pie_d.cs`. The only ones a `CoinFlipChoiceRequired`
/ `GoFirstChoiceRequired` node can reach are `CanShowPrompt` (the guard itself),
`pie_d.cs:140872` / `140894` (both inside `if (flag3)`, which `CanShowPrompt`
gates), and `PipTrayArea.updateCards` `pie_d.cs:95445`, which null-checks first.
Everything else is a reveal/selection node type (`selectionRevealArea.Initialize`)
that these two messages never construct. The selection commands themselves —
`l.V` (`pie_d.cs:140648`), `l.p` (`pie_d.cs:140342`) — read
`get_Buttons()`, never `get_Prompt()`. **VERIFIED, nothing throws.**

Two riders:

- **`pie_d.cs:95305` is `PipTrayArea.suppressedKeys`** (class at
  `pie_d.cs:95269`), a 2-entry list used at exactly one place,
  `pie_d.cs:95445`, which sets the **energy pip tray's own caption**. It gates
  nothing else — not the banner, not the action button. Note the comparison
  there is `string[].Contains(...)`: **ordinal, case-sensitive, no `$` trim** —
  unlike `PiePromptListener`, which uses `LocalizableText.HasId`
  (`core_d.cs:76425`): `OrdinalIgnoreCase` after `id.Trim('$')`, and with
  empty-vs-empty counted as a match.
- **`GoFirstChoiceRequired` has one path where `prompt` is ignored.**
  `PieSelectionNodeCommandFactory.Update` (`pie_d.cs:155966`) checks whether
  `initialCoinFlipAnimator` is still in its `Start` state; if it is, the node is
  handled by `R.R` (`pie_d.cs:198782`) instead of `l.p`, and `R.R`'s
  **constructor** (`pie_d.cs:198805`) does

  ```csharp
  promptListener.OverrideShowPrompt = true;
  promptListener.OverrideText = new LocalizableText("playmat.prompt.startingcoinflip.playerchoose");
  ```

  which `LateUpdate` prefers over the (empty) node prompt
  (`flag3 = flag3 || (OverrideShowPrompt && !IsNullOrEmpty(OverrideText))`).
  So **a `GoFirstChoiceRequired` sent without a preceding
  `CoinFlipChoiceRequired` will always show "…playerchoose" as a banner**, and
  will render its buttons through `PiePromptListener.MakeButton` rather than the
  coin dialog's left/right buttons. Sending the heads/tails call first (which
  `l.V` uses to activate the dialog and set `YouPickHeadsOrTails`) avoids it.
  *(VERIFIED that `R.R` forces the banner; INFERRED that `l.V` moves the
  animator out of `Start` — that transition lives in the animator controller
  asset, not in code.)* `R.R` also leaks `OverrideShowPrompt = true` — see §7.4.

### 7.3 The mulligan carousel

`MulliganRevealArea` (`pie_d.cs:7872`), instantiated by `b.N`
(`[MessageCommandConstructor] N(MulliganRevealCardsEffect)`, `pie_d.cs:14779`).

**One effect with N piles is one dialog that pages through them. VERIFIED.**

- **Paging controls exist and are real buttons.** `MulliganPageLeftButton` /
  `MulliganPageRightButton` are `[SerializeField]` at `pie_d.cs:7883` / `7886`
  and are polled in `Update()` (`pie_d.cs:7975`) alongside `DoneButton`.
  `HandleCardPaneScrollRight` / `Left` (`pie_d.cs:7948` / `7957`) step
  `currentViewIndex` and call `UpdatePageButtons()`.
- **`mulliganCount = entityIDPiles.Count - 1`** (`pie_d.cs:7924`) — it is the
  maximum index, not a count. `UpdatePageButtons` (`pie_d.cs:7965`) shows the
  left arrow when `currentViewIndex > 0` and the right arrow when
  `currentViewIndex < mulliganCount`, so a single-pile effect shows no arrows
  and an N-pile effect pages 1..N.
- **Header**: `CardPaneText.Text = string.Format(L.LT("playmat.mulligan.dialog.carousel.header"), currentViewIndex + 1)`
  (`pie_d.cs:7927`, and again on every page turn at `pie_d.cs:7968`). So
  `"Mulligan {0}"` is formatted with the **1-based page index**, and it
  re-renders as you page. **VERIFIED.**
- **Sub-header**: `SubHeaderText.Text = string.Format(L.LT(prompt), entityIDPiles.Count)`
  (`pie_d.cs:7926`). §2.6 marked this INFERRED; it is now **VERIFIED** by type —
  `L.LT(...)` takes a `string` (via the implicit `LocalizableText -> string`
  conversion, `core_d.cs:76461`) and `.Count` is the `List`, so there is no other
  possible binding of the two collapsed `message.A` references.
  **Trap: `prompt` must not be null.** A null `LocalizableText` converts to a
  null string, `Localize(null)` returns null, and `string.Format(null, count)`
  throws `ArgumentNullException`. It is not wrapped in a try/catch here.
- **Whose hands / face-up rendering.** The cards render from the inline
  `ReadOnlyAttributes` in `entityIDPiles` **regardless of `player`** —
  `UpdatePageButtons` calls `Introduce(mulliganList[currentViewIndex])`
  (`pie_d.cs:8043`) unconditionally, and `Introduce` re-introduces every id with
  the supplied attributes. `player` is used only by `PopulateIntroductions`
  (`pie_d.cs:7935`), which — **only when `player == the local account`** —
  records which of those ids were already legitimately in your hand, so that
  `Unintroduce` (`pie_d.cs:8060`, called for every pile on Done and on
  `OnDestroy`) skips them. **So `player` is a safety field, not a visibility
  field: if you set it to the wrong account and a pile contains cards that are
  in the local player's real hand, closing the dialog will un-introduce those
  cards out of the hand view.** This corrects §2.6's wording. **VERIFIED.**
- **Size limits.** There is no cap on the number of piles: paging is index-based
  and `Introduce`/`Unintroduce` are `for` loops over `mulliganList.Count`.
  There is no cap on cards per pile either — `MulliganCarouselRenderRequester.updateCards`
  (`pie_d.cs:7842`) iterates `carouselAnchors` and `continue`s past any index
  beyond `cardStackContents.Count`, with its own left/right scroll buttons
  (`pie_d.cs:7836`) for overflow within a page, and duplicates collapsed into
  counted stacks. **A 17-pile carousel is workable; so is 59, though the reader
  would need 58 clicks of the right arrow to reach the last page.**
  The only cost is memory: `clonedEntities` accumulates one clone per distinct
  entity across pages, cleared on Done (`pie_d.cs:8009`).
- **It cannot hang.** `b.N.execute()` (`pie_d.cs:14784`) spins until the dialog
  destroys itself **or** a timeout from the `"match"` `DWDDataManager` model
  elapses, then forces `handleDoneClicked()`. `model.c` is set in `Awake`
  (`pie_d.cs:7916`) and cleared in `OnDestroy` (`pie_d.cs:8015`).

### 7.4 The action / end-turn button, definitively

The component is **`NextButton`** (`pie_d.cs:137772`); `Update()` at
`pie_d.cs:137880`. The click handler is a *separate* component,
`NextButtonClickHandler` (`pie_d.cs:31371`), whose `Click()` coroutine is at
`pie_d.cs:31414`.

The two composite terms (`pie_d.cs:137888`, `137897`):

```csharp
haveZeroNonSelectionInterruptActive =
      (!viewingPrizes || prizeException) && !zooming && !coinFlipOrGoFirstChoice
   && !discardActive && (!prompter.OverrideShowPrompt || selections.IsInDropCardsInPrizeNode())
   && !clickAndDrag.dragInProgress && !view.Model.c;

haveZeroSelectionRelatedInterrupts = (CurrentOffer != null) && !(CurrentChoice is R.n)
   && (showCancel || onRootAndNotCustomChoice || mayAdvanceAndNoChild);
```

with `showCancel = SelectionUtils.CanCancel()` (`pie_d.cs:192229`),
`onRootAndNotCustomChoice = CurrentChoice == CurrentOffer && !(CurrentChoice is ICustomChoice)`,
`mayAdvanceAndNoChild = CurrentChoice != null && MayAdvance() && NodeToAdvanceTo() == null`.

`SelectingForAnAbility()` (`pie_d.cs:192178`) requires
`CurrentChoice.Parent != null && Parent.Parent != null && Parent.Parent is IEntityListSelection && Parent is IActionSelection`
— **it is false for any root node and false for any first-level child**, so it
is false in both cases below.

#### Case A — `SelectionWithTargetsAndActionsRequired`, `forced:false`, empty `targetMap`

`SelectionWithTargetsAndActionsNode` (`core_d.cs:71455`). With an empty
`targetMap` the ctor adds nothing, so `SelectionsMap` and `available` are empty.

| term | value | why |
|---|---|---|
| `CurrentChoice` | the root node itself | `RootSelection..ctor` does `set_Current(this)` (`core_d.cs:71265`) and this class has no `enter()` override |
| `get_MayAdvance()` | **true** | `core_d.cs:71499`: `forced ? selection != null : true` |
| `get_MayCancel()` | **true** | `core_d.cs:71509`, same shape |
| `NodeToAdvanceTo()` | **null** | `core_d.cs:71544`, `selection == null` |
| `CurrentChoice is ICustomChoice` | **false** | the class implements `IHintedEntitySelection, IEntityListSelection, ISelectionNode` only |
| `onRootAndNotCustomChoice` | **true** | |
| `mayAdvanceAndNoChild` | **true** | |
| `showCancel` | true | `CanCancel()` passes: not `ICustomChoice`, `MayCancel`, not selecting for an ability, `Kind != "InitialBenchedTargetInformation"` |
| `haveZeroSelectionRelatedInterrupts` | **true** | |
| `SelectingForAnAbility()` | false | root node, `Parent == null` |
| `buttonIsActive` | **= `haveZeroNonSelectionInterruptActive`** | the ability clause collapses to true |

Label (`setLabelTextFromSelectionContext`, `pie_d.cs:137970`): the node **is** an
`IEntityListSelection`, `MayCancel()` is true, and `get_MinToSelect()` is `0`
when `forced == false` (`core_d.cs:71508`), so it takes the branch at
`pie_d.cs:137993`:
`showCancel = false; text = L.LT("common.dialog.done"); labelSetEmpty = false;`

`setDisplayState` (`pie_d.cs:137926`) then does
`imageButton.gameObject.SetActive(buttonIsActive && !labelSetEmpty)`.

**Verdict: the button DOES appear, and it works.** Clicking it takes the
`MayAdvance()` branch of `Click()` (`pie_d.cs:31441`) -> `CurrentOffer.Advance()`
-> `RootSelection.Advance()` (`core_d.cs:71283`) sees `NodeToAdvanceTo() == null`
and writes `getResponseMessage(false)`, which for this node
(`core_d.cs:71527`) is `SelectionWithTargetsAndActions` with `selection: null`
— a clean pass. **No soft lock. VERIFIED.**

Two riders on Case A:

- **The label reads "Done", not "End Turn".** `playmat.controls.endTurnButton`
  is only reached by the final `else` at `pie_d.cs:138013`, which needs
  `!MayCancel && Parent == null`. For this node `MayCancel` is false only when
  `forced: true` **and** nothing is selected — and in that state `MayAdvance()`
  is also false, so `Click()` falls through to the `else` at `pie_d.cs:31469`
  that logs *"I should be turned off right now..."* and does nothing. **A
  `forced:true` empty offer therefore renders a dead "End Turn" button.**
  `forced:false` is the correct choice; accept "Done" as the caption.
- **`CheckShouldEndTurn()` (`pie_d.cs:31489`) dereferences
  `player1Active.get_Entity().Children.get_Item(0)`** whenever the current choice
  is a `SelectionWithTargetsAndActionsNode` at the root. With an empty
  `targetMap` it then finds nothing and returns false harmlessly — but if the
  local player has **no Active Pokémon**, `Children.get_Item(0)` throws inside
  the click coroutine. Do not send an action offer before the Active is placed.

#### Case B — `SelectionWithTargetsRequired`, current node is a chained `InitialBenchedTargetInformation`

| term | value | why |
|---|---|---|
| `CurrentChoice` | the bench `EntityListTargetNode`, `Kind == "InitialBenchedTargetInformation"` | chained as the second `TargetInformation`, §2.2 |
| `CurrentOffer` | the `SelectionWithTargetsNode` root | |
| `onRootAndNotCustomChoice` | **false** | child ≠ root |
| `MayAdvance()` | **true** with `minimumToSelect:0` / `forced:false` | `get_satisfied`, `core_d.cs:72880` |
| `NodeToAdvanceTo()` | **null** | the bench node has no children |
| `mayAdvanceAndNoChild` | **true** | |
| `showCancel` | **false** | `CanCancel()` explicitly returns `Kind != "InitialBenchedTargetInformation"` (`pie_d.cs:192234`) |
| `haveZeroSelectionRelatedInterrupts` | **true** | via `mayAdvanceAndNoChild` |
| `SelectingForAnAbility()` | false | `Parent.Parent == null` |
| `buttonIsActive` | **= `haveZeroNonSelectionInterruptActive`** | |

Label: `IEntityListSelection` yes; if `MayCancel()` is true the
`Kind == "InitialBenchedTargetInformation"` special case at `pie_d.cs:137991`
gives `common.dialog.done`; if `MayCancel()` is false the
`else if (Parent != null)` at `pie_d.cs:138008` gives `common.dialog.done` too.
**Either way `labelSetEmpty = false` and the caption is "Done". VERIFIED.**

**Verdict: the Done button DOES appear here too.** So neither case is killed by
anything inside `haveZeroSelectionRelatedInterrupts`.

#### The fields that actually kill the button

Both cases reduce to `buttonIsActive == haveZeroNonSelectionInterruptActive`.
Two of its terms are server-reachable, and both are **sticky**:

1. **`prompter.OverrideShowPrompt`.** `m.l`, the `PauseOnPromptEffect` handler
   (`pie_d.cs:143749`), sets it true at `pie_d.cs:143762` and then:

   ```csharp
   if (!msg.doPause) { yield break; }            // pie_d.cs:143765 - returns WITHOUT clearing
   ...
   promptListener.OverrideShowPrompt = false;    // pie_d.cs:143781, doPause:true path only
   ```

   **A `PauseOnPromptEffect{doPause:false}` — the exact "generic banner"
   recipe recommended in §3.5 — leaves `OverrideShowPrompt` true forever and
   silently disables the action button for the rest of the match.** The only
   things that clear it are `ClosePauseOnPromptEffect` (`d.C`, ctor
   `pie_d.cs:30858`: `OverrideShowPrompt = false; OverrideText = null`), the
   `doPause:true` path of `m.l`, and the `else` branch of
   `ForceSelectionFinished` (`pie_d.cs:143221`). `R.R` (§7.2) and the Cedric
   Juniper handler (`pie_d.cs:30394`) leak it the same way. **VERIFIED — this is
   the most likely cause of a "frozen, no Done button" report.** Rule: every
   `PauseOnPromptEffect` must be paired with a `ClosePauseOnPromptEffect`.

2. **`view.Model.c`** — the dialog-block flag. Set true by
   `MulliganRevealArea.Awake` (`pie_d.cs:7916`) and by the
   `RevealCardsToAllEffect` / `RevealCardsToPlayerEffect` handlers; cleared in
   `OnDestroy` (`pie_d.cs:8015`), by `RevealClosed`, and — usefully — by the
   **`ActivePlayerSet` message** (`pie_d.cs:136706`). If a reveal dialog is
   destroyed abnormally, or a `RevealClosed` is never sent, the button stays
   dead until the next `ActivePlayerSet`.
   *(`PlaymatView.Model` is `N.f` (`pie_d.cs:147378`), the same type
   `PlaymatProvider.get_model()` returns (`pie_d.cs:66519`); that they are the
   same instance is INFERRED, though `PiePromptListener` reading `view.Model.c`
   for exactly this purpose makes it near-certain.)*

The remaining terms are local UI state the server cannot cause: `viewingPrizes`,
`zooming`, `discardActive`, `clickAndDrag.dragInProgress`. The one other
server-reachable term is **`coinFlipOrGoFirstChoice`** (`pie_d.cs:137884`) — if
`targetType` on your offer happens to be the literal string `"CoinFlipChoice"`
or `"GoFirstChoice"`, the button is suppressed by design.

### 7.5 `Draw`

`m.c`, `[SequenceCommandConstructor("Draw")]` (`pie_d.cs:143090`).

The **constructor** (`pie_d.cs:143091`) walks the children once and sorts them:

- each `m.I` (an `EntityMoved` command) gets `i.A = true` and `i.a = true`, is
  added to the "all moves" list, and is bucketed by the moved card's type into
  Pokémon / Trainer / Energy / other;
- each `m.H` (an **`EntityIntroduced`** command) is collected into a separate
  list;
- **anything else is not collected at all.**

`executeSequence` (`pie_d.cs:143123`) then runs, in order:

1. **every collected `EntityIntroduced`, serially, to completion** — before
   anything animates;
2. builds a queue of `S.o` curve motions in bucket order Pokémon -> Trainer ->
   Energy -> other;
3. `ExecuteParallel` over all the `EntityMoved` commands (they were flagged not
   to animate themselves);
4. plays the queued curve motions **0.2 s apart** — this is the fan;
5. **`foreach (item in sequence) { while (item.MoveNext()) ... }`** — a final
   serial pass over the raw child list. The `m.I` / `m.H` commands are already
   complete so they return immediately; **any child that was neither runs here,
   last, after the fan.**

So: **an `EntityIntroduced` child does not interfere with the fan — it is
hoisted to the front and completes before the first card moves.** Introduce+move
pairs are exactly the right shape. Non-bucketed children (effects, nested
sequences) are not lost, they just run at the very end.

Two riders, both **VERIFIED**:

- `i.A = true` is the flag tested at `pie_d.cs:143578` (`if (!this.A)`), which
  guards both the per-move animation *and*
  `StartCoroutine(makeLogDisplayable(duration))`. **Inside a `Draw`,
  `animDuration` on the child `EntityMoved` is ignored entirely.**
- The `Draw` **constructor** resolves every moved entity with
  `model.All.get_Item(entityID)`, which bottoms out in `data[key]` on a
  `Dictionary` (`VersionedMap.get_Item`, `core_d.cs:12962`) and therefore throws
  `KeyNotFoundException` on a miss. The sibling `EntityIntroduced` has **not**
  executed yet at that point — `m.H` does its `Introduce` in `execute()`
  (`pie_d.cs:143413`), not in its ctor, and `SequenceCommandFactory.GetCommand`
  builds the whole command tree before running any of it
  (`Sequences.ConsumeQueuedMessages`, `pie_d.cs:147842`). **So the drawn card
  must already exist in the client's model; the `EntityIntroduced` inside a
  `Draw` can only be a re-introduce (attribute reveal) of a card the client
  already knows about, never a first introduction.** That is consistent with the
  deck being introduced face-down at deal time, which is why this works today.

### 7.6 Corrections to earlier sections

| where | said | actually |
|---|---|---|
| `CLAUDE.md`, §3.2 table | "`ActivePlayerSet` hides the coin-flip dialog and resets both coins" | true of the **sequence** `l.Z` (`pie_d.cs:141074`); the **message** command `L.Q` (`pie_d.cs:136658`) never mentions the coins |
| §3.3, `ForceSelectionFinished` | "…and tidies up the coin-flip and go-first dialogs" | those branches are unreachable — `ForceEndSelection()` nulls `get_selectionNode()` on the line above (`pie_d.cs:143196` vs `143203`). It clears the prompt override and nothing else |
| §3.5, `PauseOnPromptEffect` | "with `doPause: false` it sets a banner and returns immediately" | correct, but it also leaves `OverrideShowPrompt` true, which disables the action / end-turn button until a `ClosePauseOnPromptEffect` |
| §2.6, `MulliganRevealCardsEffect.player` | "the dialog only re-introduces hand cards when this is the local player" | the *pile* cards are introduced regardless of `player`; `player` gates `previouslyIntroduced`, i.e. which cards are **protected from being un-introduced** when the dialog closes |
| §2.6, `prompt` | "used as `string.Format(L.LT(prompt), piles.Count)` *(INFERRED)*" | now **VERIFIED** by type disambiguation, and `prompt` must be non-null or `string.Format` throws |
