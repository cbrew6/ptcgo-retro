"""
Finds the GameObject names that a given MonoBehaviour script is attached to.

Why this exists: the landing page's images are keyed by GameObject NAME -
`item.images.TryGetValue(gameObject.name, out ...)` - so serving one means
knowing what the scene calls its image slots. Guessing plausible names does
not work; the miss is silent and indistinguishable from the page being empty.

`resources.assets` is a SerializedFile like the bundles, but the shipped one
has **no type trees** (`has_type_tree == 0`), so bundle_textures' generic
reader cannot touch it. It does not need to: only three layouts matter, and
all three are stable in Unity 2018.4.

    MonoScript (115)     m_Name, m_ExecutionOrder, m_PropertiesHash[16],
                         m_ClassName, m_Namespace, m_AssemblyName
    MonoBehaviour (114)  m_GameObject(PPtr), m_Enabled(u8)+align,
                         m_Script(PPtr), m_Name
    GameObject (1)       m_Component(vector<PPtr>), m_Layer, m_Name

A PPtr is (int32 fileID, int64 pathID); only pathID is followed, because a
non-zero fileID points into another file and none of these do.

Usage:
    python tools/scene_names.py DynamicImage
    python tools/scene_names.py DynamicImage DynamicTemplate --file resources.assets
"""

import argparse
import os
import struct
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(HERE, "tools"))

import bundle_textures as bt          # noqa: E402  - Reader/SerializedFile

GAME_DIR = os.path.join(
    os.path.dirname(HERE), "PokemonTradingCardGameOnline",
    "Pokemon Trading Card Game Online_Data")

CLASS_GAMEOBJECT, CLASS_MONOBEHAVIOUR, CLASS_MONOSCRIPT = 1, 114, 115


def _string(r):
    """Unity's length-prefixed UTF-8, aligned to 4 afterwards."""
    n = r.u("i", 4)
    if n < 0 or n > 1 << 20:
        raise ValueError("implausible string length %d" % n)
    v = r.raw(n).decode("utf-8", "replace")
    r.align()
    return v


def _pptr(r):
    file_id = r.u("i", 4)
    path_id = r.u("q", 8)
    return file_id, path_id


def read_monoscript(sf, obj):
    r = bt.Reader(sf.data, obj["start"], little=True, base=obj["start"])
    name = _string(r)
    r.u("i", 4)                        # m_ExecutionOrder
    r.raw(16)                          # m_PropertiesHash
    class_name = _string(r)
    namespace = _string(r)
    assembly = _string(r)
    return {"name": name, "class": class_name,
            "namespace": namespace, "assembly": assembly}


def read_monobehaviour(sf, obj):
    r = bt.Reader(sf.data, obj["start"], little=True, base=obj["start"])
    game_object = _pptr(r)
    r.u("B", 1)                        # m_Enabled
    r.align()
    script = _pptr(r)
    name = _string(r)
    return {"game_object": game_object[1], "script": script[1], "name": name}


def read_gameobject(sf, obj):
    r = bt.Reader(sf.data, obj["start"], little=True, base=obj["start"])
    count = r.u("i", 4)
    if count < 0 or count > 4096:
        raise ValueError("implausible component count %d" % count)
    for _ in range(count):
        _pptr(r)
    r.u("i", 4)                        # m_Layer
    return {"name": _string(r)}


def scan(path, wanted):
    data = open(path, "rb").read()
    sf = bt.SerializedFile(data)
    by_id = {o["path_id"]: o for o in sf.objects}

    scripts = {}
    for obj in sf.objects:
        if obj["class_id"] != CLASS_MONOSCRIPT:
            continue
        try:
            info = read_monoscript(sf, obj)
        except Exception:
            continue                   # a layout we do not know; skip it
        if info["class"] in wanted:
            scripts.setdefault(info["class"], []).append(obj["path_id"])

    found = {}
    for obj in sf.objects:
        if obj["class_id"] != CLASS_MONOBEHAVIOUR:
            continue
        try:
            mb = read_monobehaviour(sf, obj)
        except Exception:
            continue
        cls = next((c for c, ids in scripts.items() if mb["script"] in ids),
                   None)
        if cls is None:
            continue
        holder = by_id.get(mb["game_object"])
        if holder is None or holder["class_id"] != CLASS_GAMEOBJECT:
            continue
        try:
            go = read_gameobject(sf, holder)
        except Exception:
            continue
        found.setdefault(cls, {}).setdefault(go["name"], 0)
        found[cls][go["name"]] += 1
    return sf, scripts, found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("classes", nargs="+", help="MonoScript class names")
    ap.add_argument("--file", default="resources.assets")
    args = ap.parse_args(argv)

    path = args.file
    if not os.path.isabs(path):
        candidate = os.path.join(GAME_DIR, path)
        path = candidate if os.path.exists(candidate) else path
    if not os.path.exists(path):
        sys.exit("no such file: %s" % path)

    wanted = set(args.classes)
    sf, scripts, found = scan(path, wanted)
    print("%s: %d objects, type trees %s"
          % (os.path.basename(path), len(sf.objects),
             "present" if sf.has_type_tree else "STRIPPED"))
    for cls in sorted(wanted):
        ids = scripts.get(cls)
        if not ids:
            print("  %-22s script not found in this file" % cls)
            continue
        names = found.get(cls) or {}
        print("  %-22s %d script(s), %d attached GameObject name(s)"
              % (cls, len(ids), len(names)))
        for name, count in sorted(names.items(), key=lambda kv: -kv[1]):
            print("      %-40s x%d" % (name, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
