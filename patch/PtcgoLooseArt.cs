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
/// </summary>
public static class PtcgoLooseArt
{
    private static string dir;
    private static FieldInfo cacheField;
    private static Type cacheOwner;
    // Remember misses so a missing file isn't stat'd on every single frame.
    private static readonly HashSet<string> missing = new HashSet<string>();
    private const int missLogLimit = 40;

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
            Debug.Log("[LooseArt] " + request + " <- " + Path.GetFileName(file));
        }
        catch (Exception e)
        {
            // Never let this break the caller: a throw here would take out the
            // texture path for every asset in the game.
            Debug.LogWarning("[LooseArt] " + request + ": " + e.Message);
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
