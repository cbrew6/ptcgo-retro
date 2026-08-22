"""
Extracts Texture2D objects (name, size, format, pixels) from the client's
shipped .unity3d bundles.

The container layer is handled by bundle_index.decompress(): UnityWeb
(Unity 5.2.4f1), LZMA-alone stream at headerSize.  What that yields is

    int32BE fileCount
    per file: cstr name, int32BE offset, int32BE size

and at `offset` sits a classic SerializedFile (version 15) which this module
parses.  These bundles ship WITH type trees (hasTypeTree == 1), so objects are
deserialized generically off the tree rather than against a hand-written
Texture2D layout - the field names in the output come from the file itself.

Pixels are decoded by wrapping the raw mip-0 block in a synthetic DDS header
and handing it to PIL (DXT1/DXT5), or read straight for uncompressed formats.
Unity stores textures bottom-up, so every decoded image is flipped vertically.

CLI:
    python bundle_textures.py list <bundle.unity3d> [...]
    python bundle_textures.py dump <bundle.unity3d> [...] <outdir>
    python bundle_textures.py span <bundle.unity3d> [...]
"""

import io
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from bundle_index import decompress          # noqa: E402

from PIL import Image                        # noqa: E402

# ---------------------------------------------------------------- type tree

_COMMON = [
    "AABB", "AnimationClip", "AnimationCurve", "AnimationState", "Array",
    "Base", "BitField", "bitset", "bool", "char", "ColorRGBA", "Component",
    "data", "deque", "double", "dynamic_array", "FastPropertyName", "first",
    "float", "Font", "GameObject", "Generic Mono", "GradientNEW", "GUID",
    "GUIStyle", "int", "list", "long long", "map", "Matrix4x4f", "MdFour",
    "MonoBehaviour", "MonoScript", "m_ByteSize", "m_Curve",
    "m_EditorClassIdentifier", "m_EditorHideFlags", "m_Enabled",
    "m_ExtensionPtr", "m_GameObject", "m_Index", "m_IsArray", "m_IsStatic",
    "m_MetaFlag", "m_Name", "m_ObjectHideFlags", "m_PrefabInternal",
    "m_PrefabParentObject", "m_Script", "m_StaticEditorFlags", "m_Type",
    "m_Version", "Object", "pair", "PPtr<Component>", "PPtr<GameObject>",
    "PPtr<Material>", "PPtr<MonoBehaviour>", "PPtr<MonoScript>",
    "PPtr<Object>", "PPtr<Prefab>", "PPtr<Sprite>", "PPtr<TextAsset>",
    "PPtr<Texture>", "PPtr<Texture2D>", "PPtr<Transform>", "Prefab",
    "Quaternionf", "Rectf", "RectInt", "RectOffset", "second", "set", "short",
    "size", "SInt16", "SInt32", "SInt64", "SInt8", "staticvector", "string",
    "TextAsset", "TextMesh", "Texture", "Texture2D", "Transform",
    "TypelessData", "UInt16", "UInt32", "UInt64", "UInt8", "unsigned int",
    "unsigned long long", "unsigned short", "vector", "Vector2f", "Vector3f",
    "Vector4f", "m_ScriptingClassIdentifier", "Gradient", "Type*",
    "int2_storage", "int3_storage", "BoundsInt",
    "m_CorrespondingSourceObject", "m_PrefabInstance", "m_PrefabAsset",
    "FileSize", "Hash128",
]
COMMON_BLOB = b"".join(s.encode() + b"\0" for s in _COMMON)
# sanity: offsets Unity bakes into the tree must land on these exact strings
assert COMMON_BLOB[49:54] == b"Array"
assert COMMON_BLOB[427:433] == b"m_Name"
assert COMMON_BLOB[874:883] == b"Texture2D"
assert COMMON_BLOB[894:906] == b"TypelessData"

PRIMS = {
    "SInt8": ("b", 1), "UInt8": ("B", 1), "char": ("B", 1),
    "short": ("h", 2), "SInt16": ("h", 2),
    "UInt16": ("H", 2), "unsigned short": ("H", 2),
    "int": ("i", 4), "SInt32": ("i", 4), "Type*": ("I", 4),
    "UInt32": ("I", 4), "unsigned int": ("I", 4),
    "long long": ("q", 8), "SInt64": ("q", 8),
    "UInt64": ("Q", 8), "unsigned long long": ("Q", 8), "FileSize": ("Q", 8),
    "float": ("f", 4), "double": ("d", 8), "bool": ("?", 1),
}
BYTEISH = {"SInt8", "UInt8", "char"}


class Node(object):
    __slots__ = ("type", "name", "level", "is_array", "byte_size", "index",
                 "meta_flag")


class Reader(object):
    """Little- or big-endian cursor with Unity's 4-byte stream alignment."""

    def __init__(self, buf, pos=0, little=True, base=0):
        self.b = buf
        self.p = pos
        self.e = "<" if little else ">"
        self.base = base

    def u(self, fmt, size):
        v = struct.unpack_from(self.e + fmt, self.b, self.p)[0]
        self.p += size
        return v

    def i32(self):
        return self.u("i", 4)

    def raw(self, n):
        v = self.b[self.p:self.p + n]
        self.p += n
        return v

    def cstr(self):
        e = self.b.index(b"\0", self.p)
        v = self.b[self.p:e].decode("utf-8", "replace")
        self.p = e + 1
        return v

    def align(self, n=4):
        self.p += (-(self.p - self.base)) % n


def _tree_string(local, off):
    if off & 0x80000000:
        blob, o = COMMON_BLOB, off & 0x7FFFFFFF
    else:
        blob, o = local, off
    e = blob.index(b"\0", o)
    return blob[o:e].decode("utf-8", "replace")


def _read_type_tree(r):
    count = r.u("I", 4)
    strbuf_size = r.u("I", 4)
    packed = []
    for _ in range(count):
        ver = r.u("H", 2)
        lvl = r.u("B", 1)
        arr = r.u("B", 1)
        toff = r.u("I", 4)
        noff = r.u("I", 4)
        bs = r.i32()
        idx = r.i32()
        mf = r.i32()
        packed.append((ver, lvl, arr, toff, noff, bs, idx, mf))
    local = r.raw(strbuf_size)
    nodes = []
    for ver, lvl, arr, toff, noff, bs, idx, mf in packed:
        n = Node()
        n.type = _tree_string(local, toff)
        n.name = _tree_string(local, noff)
        n.level = lvl
        n.is_array = arr
        n.byte_size = bs
        n.index = idx
        n.meta_flag = mf
        nodes.append(n)
    return nodes


def _skip_children(nodes, i, level):
    while i < len(nodes) and nodes[i].level > level:
        i += 1
    return i


def _read_value(nodes, i, r):
    """Deserialize nodes[i] and its subtree from r. Returns (value, next_i)."""
    node = nodes[i]
    lvl = node.level
    t = node.type
    align = bool(node.meta_flag & 0x4000)
    i += 1

    if t in PRIMS:
        fmt, size = PRIMS[t]
        val = r.u(fmt, size)
        if align:
            r.align()
        return val, _skip_children(nodes, i, lvl)

    if t == "string":
        n = r.i32()
        val = r.raw(n).decode("utf-8", "replace")
        r.align()                       # strings are always aligned
        i = _skip_children(nodes, i, lvl)
        if align:
            r.align()
        return val, i

    if t == "TypelessData":
        n = r.i32()
        val = r.raw(n)
        i = _skip_children(nodes, i, lvl)
        if align:
            r.align()
        return val, i

    # vector / map wrapper: the next node is the Array
    if i < len(nodes) and nodes[i].type == "Array" and nodes[i].level == lvl + 1:
        arr = nodes[i]
        align = align or bool(arr.meta_flag & 0x4000)
        elem_i = i + 2                  # Array -> [size, element]
        n = r.i32()
        if nodes[elem_i].type in BYTEISH:
            val = r.raw(n)
        else:
            val = []
            for _ in range(n):
                v, _e = _read_value(nodes, elem_i, r)
                val.append(v)
        i = _skip_children(nodes, i, lvl)
        if align:
            r.align()
        return val, i

    # struct
    val = {}
    while i < len(nodes) and nodes[i].level == lvl + 1:
        child = nodes[i]
        v, i = _read_value(nodes, i, r)
        val[child.name] = v
    i = _skip_children(nodes, i, lvl)
    if align:
        r.align()
    return val, i


# ------------------------------------------------------------ serializedfile

class SerializedFile(object):
    def __init__(self, data, base=0):
        self.data = data
        self.base = base
        h = Reader(data, base, little=False, base=base)
        self.metadata_size = h.u("I", 4)
        self.file_size = h.u("I", 4)
        self.version = h.u("I", 4)
        self.data_offset = h.u("I", 4)
        if self.version < 9:
            raise ValueError("serialized version %d unsupported" % self.version)
        endian = h.u("B", 1)
        h.raw(3)
        r = Reader(data, h.p, little=(endian == 0), base=base)
        self.unity_version = r.cstr()
        self.target_platform = r.i32()
        self.has_type_tree = bool(r.u("B", 1)) if self.version >= 13 else True

        self.types = []
        for _ in range(r.i32()):
            class_id = r.i32()
            if self.version >= 16:
                r.u("B", 1)
            if self.version >= 17:
                r.u("h", 2)
            if self.version >= 13:
                if ((self.version < 16 and class_id < 0)
                        or (self.version >= 16 and class_id == 114)):
                    r.raw(16)
                r.raw(16)
            nodes = _read_type_tree(r) if self.has_type_tree else None
            self.types.append((class_id, nodes))

        if self.version < 14:
            r.i32()                     # bigIDEnabled
        self.objects = []
        for _ in range(r.i32()):
            if self.version < 14:
                path_id = r.i32()
            else:
                r.align()
                path_id = r.u("q", 8)
            byte_start = r.u("q", 8) if self.version >= 22 else r.u("I", 4)
            byte_size = r.u("I", 4)
            type_id = r.i32()
            if self.version < 16:
                class_id = r.u("H", 2)
            else:
                class_id = self.types[type_id][0]
            if self.version < 11:
                r.u("H", 2)
            if 11 <= self.version < 17:
                r.u("h", 2)
            if 15 <= self.version <= 16:
                r.u("B", 1)
            if self.version < 16:
                # pre-16 the object's "typeID" IS the class id; resolve it to
                # an index into self.types
                tidx = next((k for k, t in enumerate(self.types)
                             if t[0] == type_id), None)
            else:
                tidx = type_id
            self.objects.append({
                "path_id": path_id,
                "start": base + self.data_offset + byte_start,
                "size": byte_size,
                "type_index": tidx,
                "class_id": class_id,
            })

    def read_object(self, obj):
        if obj["type_index"] is None:
            raise ValueError("no type for class %d" % obj["class_id"])
        nodes = self.types[obj["type_index"]][1]
        if nodes is None:
            raise ValueError("bundle has no type tree; cannot deserialize")
        r = Reader(self.data, obj["start"], little=True, base=obj["start"])
        val, _ = _read_value(nodes, 0, r)
        consumed = r.p - obj["start"]
        if consumed > obj["size"]:
            raise ValueError("overran object %d: %d > %d"
                             % (obj["path_id"], consumed, obj["size"]))
        return val


def open_bundle(path):
    """-> SerializedFile for the first file inside a UnityWeb bundle."""
    raw = decompress(path)
    if raw is None:
        raise ValueError("not a UnityWeb bundle: %s" % path)
    n = struct.unpack_from(">i", raw, 0)[0]
    p = 4
    entries = []
    for _ in range(n):
        e = raw.index(b"\0", p)
        name = raw[p:e].decode("ascii", "replace")
        p = e + 1
        off, size = struct.unpack_from(">2i", raw, p)
        p += 8
        entries.append((name, off, size))
    return SerializedFile(raw, entries[0][1])


# ------------------------------------------------------------------ decoding

TEXTURE_FORMATS = {
    1: "Alpha8", 2: "ARGB4444", 3: "RGB24", 4: "RGBA32", 5: "ARGB32",
    7: "RGB565", 9: "R16", 10: "DXT1", 12: "DXT5", 13: "RGBA4444",
    14: "BGRA32", 34: "ETC_RGB4", 47: "ETC2_RGBA8",
}


def _dds(fourcc, w, h, data):
    flags = 0x1 | 0x2 | 0x4 | 0x1000 | 0x80000
    blocks = ((w + 3) // 4) * ((h + 3) // 4)
    linear = blocks * (8 if fourcc == b"DXT1" else 16)
    hdr = b"DDS " + struct.pack("<7I44x", 124, flags, h, w, linear, 0, 0)
    hdr += struct.pack("<2I4s5I", 32, 0x4, fourcc, 0, 0, 0, 0, 0)
    hdr += struct.pack("<5I", 0x1000, 0, 0, 0, 0)
    assert len(hdr) == 128, len(hdr)
    return hdr + data[:linear]


def decode_texture(tex):
    """Texture2D dict -> RGBA PIL.Image, top-down (Unity stores bottom-up)."""
    w = tex["m_Width"]
    h = tex["m_Height"]
    fmt = tex["m_TextureFormat"]
    data = tex.get("image data") or b""
    if not data:
        return None
    name = TEXTURE_FORMATS.get(fmt, "fmt%d" % fmt)
    if name in ("DXT1", "DXT5"):
        img = Image.open(io.BytesIO(_dds(name.encode(), w, h, data)))
        img.load()
    elif name == "RGBA32":
        img = Image.frombytes("RGBA", (w, h), data[:w * h * 4])
    elif name == "ARGB32":
        a, r_, g, b = Image.frombytes("RGBA", (w, h), data[:w * h * 4]).split()
        img = Image.merge("RGBA", (r_, g, b, a))
    elif name == "BGRA32":
        b, g, r_, a = Image.frombytes("RGBA", (w, h), data[:w * h * 4]).split()
        img = Image.merge("RGBA", (r_, g, b, a))
    elif name == "RGB24":
        img = Image.frombytes("RGB", (w, h), data[:w * h * 3])
    elif name == "Alpha8":
        img = Image.frombytes("L", (w, h), data[:w * h])
    else:
        raise ValueError("unhandled texture format %s (%d)" % (name, fmt))
    return img.convert("RGBA").transpose(Image.FLIP_TOP_BOTTOM)


def textures(path):
    """Yield (info, raw texture dict) for every Texture2D in a bundle."""
    sf = open_bundle(path)
    for obj in sf.objects:
        if obj["class_id"] != 28:
            continue
        tex = sf.read_object(obj)
        info = {
            "name": tex.get("m_Name"),
            "width": tex.get("m_Width"),
            "height": tex.get("m_Height"),
            "format": TEXTURE_FORMATS.get(tex.get("m_TextureFormat"),
                                          str(tex.get("m_TextureFormat"))),
            "format_id": tex.get("m_TextureFormat"),
            "mipmap": tex.get("m_MipMap"),
            "bytes": len(tex.get("image data") or b""),
        }
        yield info, tex


# ------------------------------------------------------------- content span

def content_span(img, thresh=250, alpha_min=8):
    """Bbox of non-white, non-transparent pixels -> (x0, y0, x1, y1, w, h)."""
    w, h = img.size
    px = img.load()
    x0, y0, x1, y1 = w, h, -1, -1
    for y in range(h):
        row_hit = False
        for x in range(w):
            r, g, b, a = px[x, y]
            if a < alpha_min:
                continue
            if r >= thresh and g >= thresh and b >= thresh:
                continue
            if x < x0:
                x0 = x
            if x > x1:
                x1 = x
            row_hit = True
        if row_hit:
            if y < y0:
                y0 = y
            y1 = y
    if x1 < 0:
        return None
    return (x0, y0, x1, y1, x1 - x0 + 1, y1 - y0 + 1)


def _main(argv):
    if not argv:
        print(__doc__)
        return 1
    cmd = argv[0]
    if cmd == "list":
        for p in argv[1:]:
            for info, _tex in textures(p):
                print("%-40s %-30s %4dx%-4d %-6s %d bytes"
                      % (os.path.basename(p), info["name"], info["width"],
                         info["height"], info["format"], info["bytes"]))
    elif cmd == "dump":
        outdir = argv[-1]
        os.makedirs(outdir, exist_ok=True)
        for p in argv[1:-1]:
            for info, tex in textures(p):
                img = decode_texture(tex)
                if img is None:
                    print("  (no pixels) %s" % info["name"])
                    continue
                out = os.path.join(outdir, "%s.png" % info["name"])
                img.save(out)
                print("%s -> %s (%dx%d %s)"
                      % (info["name"], out, img.width, img.height,
                         info["format"]))
    elif cmd == "span":
        for p in argv[1:]:
            for info, tex in textures(p):
                img = decode_texture(tex)
                if img is None:
                    continue
                print("%-32s %4dx%-4d %-6s span=%s"
                      % (info["name"], info["width"], info["height"],
                         info["format"], content_span(img)))
    else:
        print("unknown command %r" % cmd)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
