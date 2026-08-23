using System;
using System.Collections.Generic;
using System.IO;
using System.Reflection;
using UnityEngine;

/// <summary>
/// Lets the client display loose PNG/JPG files as textures, bypassing Unity
/// asset bundles entirely.
///
/// Hook point: AssetBundleItem.LoadAsset() consults AssetBundleImageCache
/// BEFORE it ever goes near the bundle system --
///
///     if (AssetBundleImageCache.TryFind(out cache) &amp;&amp; cache.Contains(assetName))
///         setTexture(cache.GetTexture(assetName, ...));
///
/// so if a texture is already sitting in that cache under the requested asset
/// name, it gets used and no bundle is needed. Ensure() is injected at the top
/// of both Contains() and GetTexture(): it looks for a matching file on disk
/// and, if found, drops the texture straight into the cache dictionary.
///
/// Everything is done by reflection so this assembly does not reference
/// pie-bundles -- a mutual reference between the two would be circular.
///
/// Files live in &lt;game&gt;_Data/LooseArt/ and are named after the asset request
/// with '/' replaced by '_', e.g. asset "BW1/079" -> "BW1_079.png".
///
/// ------------------------------------------------------------------------
/// REF COUNTING -- do not remove, this is load-bearing
/// ------------------------------------------------------------------------
///
/// AssetBundleImageCache is an LRU capped at cacheSize (60). AddTexture()
/// evicts before inserting:
///
///     foreach (var item in imageCache)
///         if (!requesters.ContainsValue(item.Value)) {
///             AssetRefCounter.RemoveReference(item.Value);   // <-- here
///             imageCache.Remove(item.Key);
///             break;
///         }
///
/// and AssetRefCounter.RemoveReference THROWS InvalidOperationException
/// ("Tried to remove a Material reference to a Material that we weren't
/// tracking! &lt;name&gt;") for anything it has no count for.
///
/// The first version of this helper wrote straight into the dictionary and
/// never told AssetRefCounter, so every loose texture was an untracked
/// landmine. Once the player had browsed ~60 cards the cache stayed full, and
/// the very next AddTexture -- which is how a real foil mask arrives from a
/// bundle -- hit a loose texture during eviction and threw. The throw escapes
/// CardImageRenderer.updateCardImage, and Unity kills that coroutine at the
/// point it reached: immediately BEFORE setFoilMask(). The card keeps its
/// face and silently loses its foil, with nothing in the UI to show for it.
///
/// That is why "foils work for the first few cards and then stop", and why it
/// looked like an era-specific or set-specific gap rather than a cache-size
/// one. Registering the reference here makes eviction legal and the coroutine
/// survives to bind the mask.
///
/// The reference we add mirrors exactly the one AddTexture() would have added
/// for a bundle-loaded texture, so the counts stay balanced: +1 on insert into
/// imageCache, +1 per GetTexture() requester, -1 on eviction, -1 when a
/// requester goes away.
/// </summary>
public static class PtcgoLooseArt
{
    private static string dir;
    private static FieldInfo cacheField;
    private static Type cacheOwner;
    private static MethodInfo addReference;
    private static bool addReferenceResolved;
    // Remember misses so a missing file isn't stat'd on every single frame.
    private static readonly HashSet<string> missing = new HashSet<string>();
    // The miss log is the only way to discover a request name we are not
    // serving, and at 40 it ran out during the login/collection walk - which
    // is why nobody had ever seen a deckBoxes/ or cardSleeves/ line even
    // though the client asks for them. One line per distinct asset, once.
    private const int missLogLimit = 500;

    private static readonly string[] extensions = { ".png", ".jpg", ".jpeg" };

    public static void Ensure(object cache, string request)
    {
        try
        {
            if (cache == null || string.IsNullOrEmpty(request)) return;
            if (missing.Contains(request)) return;

            IDictionary<string, Texture> map = GetMap(cache);
            if (map == null || map.ContainsKey(request)) return;

            if (dir == null)
                dir = Path.Combine(Application.dataPath, "LooseArt");

            string stem = request.Replace('/', '_').Replace('\\', '_');
            string file = null;
            for (int i = 0; i < extensions.Length; i++)
            {
                string candidate = Path.Combine(dir, stem + extensions[i]);
                if (File.Exists(candidate)) { file = candidate; break; }
            }
            if (file == null)
            {
                // Log the first miss for each asset. This is how the exact
                // request naming was discovered - drop a file named after what
                // shows up here and it will be picked up next time.
                missing.Add(request);
                if (missing.Count <= missLogLimit)
                    Debug.Log("[LooseArt] miss: " + request + "  (expected " +
                              stem + ".png)");
                return;
            }

            Texture2D tex = new Texture2D(2, 2, TextureFormat.RGBA32, false);
            tex.LoadImage(File.ReadAllBytes(file));
            tex.name = request;
            map[request] = tex;
            // MUST happen for every texture that enters imageCache; see the
            // class comment. Without it the cache's own eviction throws and
            // takes CardImageRenderer's loader coroutine down with it.
            Track(cache, tex);
            Debug.Log("[LooseArt] " + request + " <- " + Path.GetFileName(file));
        }
        catch (Exception e)
        {
            // Never let this break the caller: a throw here would take out the
            // texture path for every asset in the game.
            Debug.LogWarning("[LooseArt] " + request + ": " + e.Message);
        }
    }

    /// <summary>Register the texture with pie-bundles' AssetRefCounter, the
    /// way AddTexture() does for bundle-loaded ones.</summary>
    private static void Track(object cache, Texture tex)
    {
        if (!addReferenceResolved)
        {
            addReferenceResolved = true;
            try
            {
                // Same assembly as the cache, so no assembly reference and no
                // dependency on the obfuscated name surviving.
                Type t = cache.GetType().Assembly
                              .GetType("pie.bundles.bundlemanager.AssetRefCounter");
                if (t == null)
                {
                    // Fall back to shape: a static class with both
                    // AddReference(Object) and RemoveReference(Object).
                    foreach (Type candidate in cache.GetType().Assembly.GetTypes())
                    {
                        MethodInfo add = candidate.GetMethod(
                            "AddReference",
                            BindingFlags.Static | BindingFlags.Public |
                            BindingFlags.NonPublic,
                            null, new[] { typeof(UnityEngine.Object) }, null);
                        MethodInfo rem = candidate.GetMethod(
                            "RemoveReference",
                            BindingFlags.Static | BindingFlags.Public |
                            BindingFlags.NonPublic,
                            null, new[] { typeof(UnityEngine.Object) }, null);
                        if (add != null && rem != null) { t = candidate; break; }
                    }
                }
                if (t != null)
                    addReference = t.GetMethod(
                        "AddReference",
                        BindingFlags.Static | BindingFlags.Public |
                        BindingFlags.NonPublic,
                        null, new[] { typeof(UnityEngine.Object) }, null);
            }
            catch (Exception e)
            {
                Debug.LogWarning("[LooseArt] no AssetRefCounter: " + e.Message);
            }
            if (addReference == null)
                Debug.LogWarning("[LooseArt] AssetRefCounter.AddReference not " +
                                 "found - cache eviction will throw and foils " +
                                 "will stop loading after ~60 cards");
        }
        if (addReference != null)
            addReference.Invoke(null, new object[] { tex });
    }

    /// <summary>Find the cache's Dictionary&lt;string, Texture&gt; by shape, not
    /// by name, so obfuscation or a renamed field doesn't break it.</summary>
    private static IDictionary<string, Texture> GetMap(object cache)
    {
        Type t = cache.GetType();
        if (cacheField == null || cacheOwner != t)
        {
            cacheOwner = t;
            cacheField = null;
            FieldInfo[] fields = t.GetFields(BindingFlags.Instance |
                                             BindingFlags.NonPublic |
                                             BindingFlags.Public);
            foreach (FieldInfo f in fields)
            {
                Type ft = f.FieldType;
                if (!ft.IsGenericType) continue;
                Type[] args = ft.GetGenericArguments();
                if (args.Length == 2 &&
                    args[0] == typeof(string) &&
                    typeof(Texture).IsAssignableFrom(args[1]) &&
                    typeof(IDictionary<string, Texture>).IsAssignableFrom(ft))
                {
                    cacheField = f;
                    break;
                }
            }
            if (cacheField == null)
                Debug.LogWarning("[LooseArt] no image cache dictionary on " + t.FullName);
        }
        return cacheField == null
            ? null
            : cacheField.GetValue(cache) as IDictionary<string, Texture>;
    }
}
