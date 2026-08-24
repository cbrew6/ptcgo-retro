"""
Imports asset bundles out of a donated `bundleCache` into the ones we serve.

The client caches every bundle it downloads under

    <persistentDataPath>/bundleCache/<bundleName>/<hash>/__data

and `__data` IS the .unity3d payload, byte for byte. Our asset server serves
`StreamingAssets/en_US/<bundleName>.unity3d`, so importing is a copy and a
rename - no repacking, no re-signing.

A cache may still be a .zip. Those run 4 GB and unpacking one to take 118
bundles out of it is a waste of an hour and 4 GB of disk, so a zip is read in
place: entries are streamed straight to their destination.

This is how the art that the shut-down CDN used to hold comes back. Nothing
here can be synthesised: a foil mask traces the card's own artwork silhouette,
so it exists only in a cache belonging to someone who actually played that set.

Two rules this does not bend:

  - A bundle already on disk is never overwritten unless it differs AND the
    donated one is the larger, because a truncated cache entry looks exactly
    like a complete one until something tries to read it.
  - Only whole, non-empty payloads are taken. A partially downloaded bundle in
    a donor cache is indistinguishable from a good one by name alone.

Usage:
    python tools/import_bundle_cache.py <cacheDir|cache.zip>
    python tools/import_bundle_cache.py <cacheDir|cache.zip> --apply
    python tools/import_bundle_cache.py <cacheDir|cache.zip> --apply --only=_wp_
"""

import argparse
import collections
import hashlib
import os
import re
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import asset_server  # noqa: E402

# en_US_BW1_wp_std_Foil2_CR111_11 -> set BW1, kind wp_std
SET_RE = re.compile(r"^en_US_([A-Za-z0-9]+)_")
KIND_RE = re.compile(r"_(wp_[a-z]+)")


class Payload(object):
    """One complete `__data`, wherever it physically lives.

    Size is known without reading, which is what the "only take the larger
    one" rule compares; bytes are only pulled when a copy actually happens.
    """

    def __init__(self, size, reader):
        self.size = size
        self._reader = reader

    def read(self):
        return self._reader()

    def digest(self):
        h = hashlib.sha1()
        h.update(self.read())
        return h.hexdigest()


def payloads(cache):
    """bundleName -> Payload, for every complete entry in a dir or a .zip."""
    if os.path.isfile(cache) and cache.lower().endswith(".zip"):
        return _zip_payloads(cache)
    return _dir_payloads(cache)


def _dir_payloads(cache_dir):
    found = {}
    for name in sorted(os.listdir(cache_dir)):
        entry = os.path.join(cache_dir, name)
        if not os.path.isdir(entry):
            continue
        best = None
        for sub in os.listdir(entry):
            data = os.path.join(entry, sub, "__data")
            if not os.path.isfile(data):
                continue
            size = os.path.getsize(data)
            if size <= 0:
                continue                 # a started download, not a bundle
            if best is None or size > best[0]:
                best = (size, data)
        if best is not None:
            found[name] = Payload(
                best[0], lambda p=best[1]: open(p, "rb").read())
    return found


def _zip_payloads(path):
    """The same rule, over a zipped cache read in place.

    The bundleCache may sit at any depth (donors zip from different roots), so
    the entry is located by the `bundleCache/<name>/<hash>/__data` shape rather
    than by a fixed prefix.
    """
    archive = zipfile.ZipFile(path)
    best = {}
    for info in archive.infolist():
        if info.is_dir() or "/bundleCache/" not in info.filename:
            continue
        parts = info.filename.split("/bundleCache/")[1].split("/")
        if len(parts) < 3 or parts[-1] != "__data" or info.file_size <= 0:
            continue
        name = parts[0]
        if name not in best or info.file_size > best[name].file_size:
            best[name] = info
    return {n: Payload(i.file_size, lambda i=i: archive.read(i))
            for n, i in best.items()}


def digest(path):
    h = hashlib.sha1()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def classify(name):
    set_match = SET_RE.match(name)
    kind_match = KIND_RE.search(name)
    return (set_match.group(1) if set_match else "?",
            kind_match.group(1) if kind_match else "art")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cache", help="a bundleCache directory, or a .zip of one")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--only", default="",
                    help="only bundles whose name contains this")
    args = ap.parse_args(argv)

    if not (os.path.isdir(args.cache) or os.path.isfile(args.cache)):
        sys.exit("no such cache: %s" % args.cache)
    target = asset_server.BUNDLE_DIR
    if not os.path.isdir(target):
        sys.exit("no bundle directory at %s" % target)

    donated = payloads(args.cache)
    if args.only:
        donated = {k: v for k, v in donated.items() if args.only in k}
    have = {f[:-len(".unity3d")] for f in os.listdir(target)
            if f.endswith(".unity3d")}

    new, replace, same = [], [], 0
    for name, payload in sorted(donated.items()):
        destination = os.path.join(target, name + ".unity3d")
        if name not in have:
            new.append((name, payload, destination))
            continue
        # Same name already served. Only take the donated one if it is both
        # different and bigger - a truncated entry has a valid name too.
        if payload.size > os.path.getsize(destination) \
                and payload.digest() != digest(destination):
            replace.append((name, payload, destination))
        else:
            same += 1

    per_set = collections.defaultdict(collections.Counter)
    for name, _payload, _d in new + replace:
        set_code, kind = classify(name)
        per_set[set_code][kind] += 1

    print("%d bundles in the cache, %d already served identically"
          % (len(donated), same))
    print("%d new, %d larger than what we have" % (len(new), len(replace)))
    if per_set:
        print("\n%-14s %s" % ("set", "new bundles by kind"))
        for set_code in sorted(per_set):
            counts = per_set[set_code]
            print("%-14s %s" % (set_code, ", ".join(
                "%s x%d" % (k, n) for k, n in sorted(counts.items()))))

    if not args.apply:
        print("\n(dry run - pass --apply to copy %d files)"
              % (len(new) + len(replace)))
        return 0

    written = 0
    for name, payload, destination in new + replace:
        tmp = destination + ".part"
        with open(tmp, "wb") as fh:
            fh.write(payload.read())
        os.replace(tmp, destination)       # never a half-written bundle
        written += 1
    print("\nwrote %d bundles into %s" % (written, target))
    print("now: python bundle_index.py, then bump asset_server.MANIFEST_VERSION,"
          " then python tools/unshadow_foils.py --apply")
    return 0


if __name__ == "__main__":
    sys.exit(main())
