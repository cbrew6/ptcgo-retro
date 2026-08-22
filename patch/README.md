# Loose-art patch

Lets the client display ordinary PNG/JPG files instead of Unity asset bundles,
so card art and backgrounds can come from any source. This is the workaround
for the CDN content that no longer exists.

## How it works

`AssetBundleItem.LoadAsset()` consults `AssetBundleImageCache` *before* it
touches the bundle system:

```csharp
if (AssetBundleImageCache.TryFind(out cache) && cache.Contains(assetName))
    setTexture(cache.GetTexture(assetName, ...));
```

So a texture already sitting in that cache is used as-is, no bundle required.
`PtcgoLooseArt.Ensure()` is injected at the top of both `Contains()` and
`GetTexture()`; it looks for a matching file on disk and drops the texture
straight into the cache dictionary.

Both are plain methods - no coroutine surgery, no call-site changes. The
helper uses reflection to reach the cache dictionary so it doesn't reference
pie-bundles (that would be a circular assembly reference), and it finds the
dictionary *by shape* rather than by field name.

`Ensure()` never throws: an exception there would break the texture path for
every asset in the game.

## Applying

```
csc -target:library -out:PtcgoLooseArt.dll \
    -r:<Managed>/UnityEngine.dll -r:<Managed>/UnityEngine.CoreModule.dll \
    -r:<Managed>/UnityEngine.ImageConversionModule.dll PtcgoLooseArt.cs
copy PtcgoLooseArt.dll <Managed>/
powershell -File patch.ps1 -CecilDir <cecil> -Managed <Managed> -Helper <Managed>/PtcgoLooseArt.dll
```

Needs Mono.Cecil (nuget `Mono.Cecil` 0.11.5, `lib/net40`). Close the game
first - Cecil can't write a loaded DLL, and `ReaderParameters.InMemory` must
be true or it holds the file open itself.

`patch.ps1` backs up to `pie-bundles.dll.orig` on first run and restores from
it before re-patching, so it's safe to run repeatedly. **To revert:** copy
`pie-bundles.dll.orig` back over `pie-bundles.dll`.

## Naming

Files go in `<game>_Data/LooseArt/`, named after the asset request with `/`
replaced by `_`:

| Request | File |
| --- | --- |
| `Background/Background` | `Background_Background.png` |
| `BW10/008` | `BW10_008.png` |
| `BW10_wp_ph/008` | `BW10_wp_ph_008.png` |
| `Logos/globalNavLogo` | `Logos_globalNavLogo.png` |

Card art is `{SET}/{3-digit collection number}`. The `_wp_ph`, `_wp_std` and
`_wp_pcd` suffixes are alternate printings of the same card.

Misses are harmless - the client falls through to the normal bundle path. The
first miss for each asset is logged to `output_log.txt` as
`[LooseArt] miss: <request>  (expected <file>.png)`, which is how to discover
names for anything not listed above.
