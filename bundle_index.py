"""
Extracts asset names from the client's shipped .unity3d bundles.

Why this is needed: every art request in the client is gated behind

    bundleManager.DoesAssetExistInManifest(assetName)

which is just `assetPaths.ContainsKey(name)`, and `assetPaths` is built solely
from each descriptor's `assets[]` array (AssetBundleRequester.SetBundle).
Ship an empty `assets[]` and the client never requests a single bundle -
backgrounds, sleeves, deck boxes and card art all silently render black.

The names live in the AssetBundle object's m_Container map inside each bundle.
Two container formats appear in StreamingAssets:

  * UnityWeb (Unity 5.2.4f1) - the 233 bundles that shipped with the client.
  * UnityFS  (Unity 2018.4.11f1) - bundles recovered from a donated
    LocalLow bundleCache. The client reads both, so they are served as-is;
    only the name extraction below has to understand each layout.

UnityWeb layout:

    "UnityWeb\\0", int32BE format, cstr unityVersion, cstr unityRevision,
    int32BE minimumStreamedBytes, int32BE headerSize,
    int32BE levelsBeforeStreaming, int32BE levelCount,
    per level: int32BE compressed, int32BE uncompressed
    ... then at headerSize: an LZMA-alone stream

UnityFS layout:

    "UnityFS\0", int32BE version, cstr unityVersion, cstr unityRevision,
    int64BE size, int32BE compressedBlocksInfoSize,
    int32BE uncompressedBlocksInfoSize, int32BE flags
    blocksInfo (at end of file when flags & 0x80, else inline), compressed
    per flags & 0x3f; it holds a 16-byte hash, an int32BE block count and
    then (uncompressedSize, compressedSize, flags) per block. Concatenating
    the decompressed blocks yields the same SerializedFile payload the
    UnityWeb path produces. The donated bundles use uncompressed blocksInfo
    and LZ4 data blocks.

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


def _lzma_alone(b):
    return lzma.LZMADecompressor(format=lzma.FORMAT_ALONE).decompress(b)


try:
    from lz4.block import decompress as _lz4_block

    def _lz4(src, size):
        return _lz4_block(src, uncompressed_size=size)
except ImportError:
    def _lz4(src, size):
        """Minimal LZ4 block decoder, so this script needs no dependencies."""
        dst, i, n = bytearray(), 0, len(src)
        while i < n:
            token = src[i]; i += 1
            ln = token >> 4
            if ln == 15:
                while True:
                    b = src[i]; i += 1; ln += b
                    if b != 255:
                        break
            dst += src[i:i + ln]; i += ln
            if i >= n:
                break
            off = src[i] | (src[i + 1] << 8); i += 2
            ml = token & 0x0F
            if ml == 15:
                while True:
                    b = src[i]; i += 1; ml += b
                    if b != 255:
                        break
            ml += 4
            start = len(dst) - off
            if off >= ml:
                dst += dst[start:start + ml]
            else:                       # overlapping run
                for k in range(ml):
                    dst.append(dst[start + k])
        return bytes(dst)


def _unpack(mode, blob, size):
    if mode == 0:
        return blob
    if mode == 1:
        return _lzma_alone(blob)
    if mode in (2, 3):
        return _lz4(blob, size)
    raise ValueError("unknown compression mode %d" % mode)


def unityweb_payload(d):
    o = 0
    _, o = _cstr(d, o)
    o += 4                      # format
    _, o = _cstr(d, o)          # unity version
    _, o = _cstr(d, o)          # unity revision
    _, header_size = struct.unpack_from(">2i", d, o)
    return _lzma_alone(d[header_size:])


def unityfs_payload(d):
    o = 0
    _, o = _cstr(d, o)
    version = struct.unpack_from(">i", d, o)[0]; o += 4
    _, o = _cstr(d, o)          # unity version
    _, o = _cstr(d, o)          # unity revision
    _size, c_info, u_info, flags = struct.unpack_from(">qiii", d, o); o += 20
    if version >= 7:
        o = (o + 15) & ~15      # 16-byte alignment was added in v7
    if flags & 0x80:            # blocksInfo lives at the end of the file
        info, data_off = d[len(d) - c_info:], o
    else:
        info, data_off = d[o:o + c_info], o + c_info
    info = _unpack(flags & 0x3F, info, u_info)

    q = 16                      # skip uncompressedDataHash
    count = struct.unpack_from(">i", info, q)[0]; q += 4
    out = []
    for _ in range(count):
        usize, csize, bflags = struct.unpack_from(">iih", info, q); q += 10
        out.append(_unpack(bflags & 0x3F, d[data_off:data_off + csize], usize))
        data_off += csize
    return b"".join(out)


def decompress(path):
    d = open(path, "rb").read()
    if d.startswith(b"UnityWeb"):
        return unityweb_payload(d)
    if d.startswith(b"UnityFS"):
        return unityfs_payload(d)
    return None


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
