"""
Extracts asset names from the client's shipped .unity3d bundles.

Why this is needed: every art request in the client is gated behind

    bundleManager.DoesAssetExistInManifest(assetName)

which is just `assetPaths.ContainsKey(name)`, and `assetPaths` is built solely
from each descriptor's `assets[]` array (AssetBundleRequester.SetBundle).
Ship an empty `assets[]` and the client never requests a single bundle -
backgrounds, sleeves, deck boxes and card art all silently render black.

The names live in the AssetBundle object's m_Container map inside each bundle.
Bundles are UnityWeb (Unity 5.2.4f1) containers:

    "UnityWeb\\0", int32BE format, cstr unityVersion, cstr unityRevision,
    int32BE minimumStreamedBytes, int32BE headerSize,
    int32BE levelsBeforeStreaming, int32BE levelCount,
    per level: int32BE compressed, int32BE uncompressed
    ... then at headerSize: an LZMA-alone stream

Inside, m_Container entries are little-endian length-prefixed ASCII strings
padded to a 4-byte boundary.

Writes bundle_index.json: {bundle_name: [asset names]}.
Run after changing the set of bundles; asset_server.py loads the result.
"""

import json
import lzma
import os
import re
import struct
import sys

LOCALE = "en_US"
BUNDLE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data",
    "StreamingAssets",
    LOCALE,
)
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bundle_index.json")

# Type-tree field names and Unity internals also appear as plain strings; asset
# names are lowercase-ish paths. Exclude the obvious engine metadata.
ENGINE = re.compile(r"^(m_|GL|AssetInfo$|AssetBundle$|PPtr|Texture2D$|Sprite$|"
                    r"preload|asset$|data$|image data$|Base$|Array$|int$|"
                    r"float$|string$|char$|bool$|unsigned |SInt|UInt)")
NAMEOK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./+\- ]*$")


def _cstr(b, o):
    e = b.index(b"\0", o)
    return b[o:e].decode("ascii", "replace"), e + 1


def decompress(path):
    d = open(path, "rb").read()
    if not d.startswith(b"UnityWeb"):
        return None
    o = 0
    _, o = _cstr(d, o)
    o += 4                      # format
    _, o = _cstr(d, o)          # unity version
    _, o = _cstr(d, o)          # unity revision
    _, header_size = struct.unpack_from(">2i", d, o)
    return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(d[header_size:])


def asset_names(raw):
    """Walk length-prefixed, 4-byte-aligned ASCII strings."""
    names, i, n = [], 0, len(raw)
    while i + 4 <= n:
        ln = struct.unpack_from("<i", raw, i)[0]
        if 1 <= ln <= 200 and i + 4 + ln <= n:
            chunk = raw[i + 4:i + 4 + ln]
            pad = (-ln) % 4
            if (all(32 <= c < 127 for c in chunk)
                    and raw[i + 4 + ln:i + 4 + ln + pad] == b"\0" * pad):
                s = chunk.decode("ascii")
                if NAMEOK.match(s) and not ENGINE.match(s):
                    names.append(s)
                i += 4 + ln + pad
                continue
        i += 4
    seen, out = set(), []
    for s in names:
        k = s.lower()
        if k not in seen:
            seen.add(k)
            out.append(s)
    return out


def main():
    if not os.path.isdir(BUNDLE_DIR):
        sys.exit("no bundle directory: %s" % BUNDLE_DIR)
    index, total = {}, 0
    for fn in sorted(os.listdir(BUNDLE_DIR)):
        if not fn.endswith(".unity3d"):
            continue
        stem = fn[:-len(".unity3d")]
        if stem.startswith(LOCALE + "_"):
            stem = stem[len(LOCALE) + 1:]
        name, _, ver = stem.rpartition("_")
        if not name or not ver.isdigit():
            continue
        try:
            raw = decompress(os.path.join(BUNDLE_DIR, fn))
        except (lzma.LZMAError, ValueError, struct.error) as exc:
            print("  SKIP %s: %s" % (fn, exc))
            continue
        if raw is None:
            continue
        names = asset_names(raw)
        index[name] = names
        total += len(names)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(index, fh, indent=1)
    print("indexed %d bundles, %d asset names -> %s" % (len(index), total, OUT))


if __name__ == "__main__":
    main()
