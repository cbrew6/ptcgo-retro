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
///
/// ------------------------------------------------------------------------
/// AND THE CACHE HAS TO BE BOUNDED -- see Evict()
/// ------------------------------------------------------------------------
///
/// Writing into imageCache directly gets the entry in, but it also skips
/// AddTexture()'s eviction, so nothing ever takes a loose texture back out.
/// The client's own cap of 60 only applies on the AddTexture() path, and a
/// collection scroll is served almost entirely from here -- so the dictionary
/// simply grows. One session reached 583 textures.
///
/// Compressing them (see Shrink) took the per-texture cost from ~8 MB to
/// ~0.5-1 MB, which is what stopped the process dying at 3.4 GB of address
/// space, but "smaller" is not "bounded": a long enough session still walks
/// off the end. Evict() applies our own cap on the entries we inserted.
/// </summary>
public static class PtcgoLooseArt
{
    private static string dir;
    private static FieldInfo cacheField;
    private static Type cacheOwner;
    private static MethodInfo addReference;
    private static MethodInfo removeReference;
    private static bool addReferenceResolved;

    // The keys we put into imageCache, oldest first. Only ours: an entry the
    // bundle system added is the bundle system's to evict.
    private static readonly List<string> inserted = new List<string>();

    /// <summary>How many loose textures may sit in the cache at once.
    ///
    /// The client's own cap is 60, chosen for RGBA32 bundle textures. Ours are
    /// DXT by the time they land (Shrink), so 150 costs about the same memory
    /// as 20 of the client's would, and re-reading an evicted one means
    /// decoding a PNG off disk again - worth avoiding for anything the player
    /// might scroll back to.</summary>
    private const int looseCacheLimit = 150;
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
            Shrink(tex);
            // Evict BEFORE inserting, the way AddTexture() does, so the cap
            // is a ceiling rather than a ceiling plus one.
            Evict(map);
            map[request] = tex;
            // The client's own AddTexture() can evict one of ours behind our
            // back, after which the same request is loaded again. Dropping any
            // stale entry keeps one bookkeeping row per reference we hold; two
            // rows for one texture would release it once and count it twice.
            inserted.Remove(request);
            inserted.Add(request);
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

    /// <summary>Drop our oldest entries until the cache is under the cap.
    ///
    /// This mirrors AddTexture()'s eviction with one deliberate difference: it
    /// does not consult the `requesters` map first. Skipping that check is
    /// safe, and the reason is worth stating because it looks unsafe.
    ///
    /// Removing a key from imageCache does NOT destroy the Texture. A renderer
    /// that is currently showing it holds its own reference, so the object
    /// stays alive and the card on screen is unaffected; the only consequence
    /// is that asking for it again re-reads the PNG from disk. The reference
    /// counts also stay balanced, because we remove exactly the one reference
    /// we added on insert and leave every requester's own count alone.
    ///
    /// So the worst case here is a redundant decode, not a black card and not
    /// an unbalanced counter. Trying to find `requesters` by reflection to
    /// avoid that, on the other hand, means guessing which of two identically
    /// shaped dictionaries is which - and guessing wrong there evicts nothing
    /// or evicts everything.</summary>
    private static void Evict(IDictionary<string, Texture> map)
    {
        // >= because the caller inserts one immediately after, so the cap is
        // a ceiling on what the dictionary ever holds, not on what it held.
        while (inserted.Count >= looseCacheLimit)
        {
            string key = inserted[0];
            inserted.RemoveAt(0);
            Texture old;
            if (!map.TryGetValue(key, out old) || old == null)
                continue;              // already gone, taken by AddTexture
            map.Remove(key);
            Untrack(old);
        }
    }

    /// <summary>DXT-compress the decoded PNG and drop its CPU-side copy.
    ///
    /// This is a crash fix, not an optimisation. The client is a 32-bit
    /// process, so it dies at roughly 3.4 GB of address space no matter how
    /// much RAM the machine has. LoadImage() decodes to RGBA32, which is
    /// 4 MB for a 1024x1024 card face and keeps a second 4 MB copy on the CPU
    /// side because the texture stays readable. Scrolling the collection
    /// loaded 583 distinct textures in one session and the process died on an
    /// access violation inside a memcpy at a 3348 MB working set.
    ///
    /// Compress() takes that to DXT1 (opaque, 512 KB) or DXT5 (alpha, 1 MB) -
    /// which is exactly the format the real bundles ship, so it is the
    /// authentic quality rather than a downgrade - and Apply(..., true) frees
    /// the readable copy. Together that is an 8-16x reduction.
    ///
    /// Compression needs both dimensions to be a multiple of 4. Card art and
    /// masks are powers of two, but a stray file need not be, so a failure
    /// here leaves the texture usable and merely large.</summary>
    private static void Shrink(Texture2D tex)
    {
        try
        {
            if ((tex.width & 3) == 0 && (tex.height & 3) == 0)
                tex.Compress(false);
            // Uploads and releases the CPU copy. Nothing reads these back.
            tex.Apply(false, true);
        }
        catch (Exception e)
        {
            Debug.LogWarning("[LooseArt] could not compress " + tex.name +
                             ": " + e.Message);
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
                {
                    addReference = t.GetMethod(
                        "AddReference",
                        BindingFlags.Static | BindingFlags.Public |
                        BindingFlags.NonPublic,
                        null, new[] { typeof(UnityEngine.Object) }, null);
                    removeReference = t.GetMethod(
                        "RemoveReference",
                        BindingFlags.Static | BindingFlags.Public |
                        BindingFlags.NonPublic,
                        null, new[] { typeof(UnityEngine.Object) }, null);
                }
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

    /// <summary>Give back the one reference Track() added.
    ///
    /// Never lets a throw out. RemoveReference raises if it is not counting
    /// the texture, which should be impossible for anything we inserted, but
    /// this runs inside the loader coroutine and a throw here would kill it -
    /// which is the exact failure the ref counting exists to prevent.</summary>
    private static void Untrack(Texture tex)
    {
        if (removeReference == null) return;
        try
        {
            removeReference.Invoke(null, new object[] { tex });
        }
        catch (Exception e)
        {
            Debug.LogWarning("[LooseArt] could not release " + tex.name +
                             ": " + e.Message);
        }
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
