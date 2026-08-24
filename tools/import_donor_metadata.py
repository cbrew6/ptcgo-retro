"""
Extracts the metadata a donated cache carries alongside its bundles.

`import_bundle_cache.py` takes the art. This takes the four files next to it,
each of which answers a question we were previously guessing at:

    <cache>/<name>.manifestbf     the real CDN AssetBundleManifest, as JSON.
                                  3,058 bundles and 32,929 asset names, with
                                  the exact `versionings` the URL scheme wants.
                                  Several revisions may be present; the highest
                                  `version` wins.
    <cache>/AttributeDB.db        the client's own card index: 19,760 archetype
                                  GUIDs, 13,528 ability GUIDs, per-type energy
                                  costs, damage, and every attack's English
                                  text already resolved.
    <cache>/LocalizationDB-*.db   40,306 strings, against 27,550 in the copy
                                  that ships in StreamingAssets.
    <cache>/cachedMessages/GetSetData
                                  the server's own set list - 87 sets with
                                  external id, card count, block and formats.

Why the manifest matters more than it sounds: every art request in the client
is gated behind `DoesAssetExistInManifest(name)`, and we have been building
that name map by decoding bundles ourselves. Declaring a name the real server
never declared, or missing one it did, is invisible until a card renders black.
The manifest is the authority, so `bundle_index.py` should prefer it and fall
back to extraction only for bundles it does not list.

Two rules this does not bend:

  - `cake.cfg` and `output_log.txt` are never taken. They are the donor's own
    configuration and logs, not game data, and they can carry account details.
  - Nothing already in `donor/` is overwritten unless --force is passed, so a
    second cache cannot quietly downgrade a newer manifest.

Usage:
    python tools/import_donor_metadata.py <cache-or-zip> [...]
    python tools/import_donor_metadata.py <cache-or-zip> --apply
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
import zipfile

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DONOR_DIR = os.path.join(HERE, "donor")

# Never taken, whatever it is named or wherever it sits in the tree.
EXCLUDED = ("cake.cfg", "output_log.txt")

MANIFEST_RE = re.compile(r"\.manifestbf$", re.I)


def _excluded(name):
    return os.path.basename(name).lower() in EXCLUDED


class Source(object):
    """A donated cache, whether it is a directory or still a .zip."""

    def __init__(self, path):
        self.path = path
        self.zip = None
        if os.path.isfile(path) and path.lower().endswith(".zip"):
            self.zip = zipfile.ZipFile(path)
            self.names = [n for n in self.zip.namelist()
                          if not n.endswith("/") and not _excluded(n)]
        elif os.path.isdir(path):
            self.names = []
            for root, _dirs, files in os.walk(path):
                for f in files:
                    full = os.path.join(root, f)
                    if not _excluded(full):
                        self.names.append(full)
        else:
            raise ValueError("not a cache directory or zip: %s" % path)

    def read(self, name):
        if self.zip is not None:
            return self.zip.read(name)
        with open(name, "rb") as fh:
            return fh.read()

    def find(self, predicate):
        return [n for n in self.names if predicate(n)]


def newest_manifest(sources):
    """The manifest revision with the highest version across every source.

    A cache holds every revision the client ever downloaded, and the file names
    are content GUIDs rather than version numbers, so the version has to be read
    out of the JSON. Picking by file order or by size gets an old one.
    """
    best = None
    for src in sources:
        for name in src.find(lambda n: MANIFEST_RE.search(n)):
            raw = src.read(name)
            try:
                doc = json.loads(raw.decode("utf-8"))
                version = int(doc["version"])
            except Exception:
                continue                      # a revision we cannot read; skip
            if best is None or version > best[0]:
                best = (version, doc, raw, name, src.path)
    return best


def _rowcount(blob, table):
    """Row count without writing the database to disk first."""
    con = sqlite3.connect(":memory:")
    try:
        con.deserialize(blob)
        return con.execute("select count(*) from [%s]" % table).fetchone()[0]
    except Exception:
        return None
    finally:
        con.close()


def pick_localization(sources):
    """The LocalizationDB with the most rows - caches differ by play history."""
    best = None
    for src in sources:
        for name in src.find(
                lambda n: os.path.basename(n).lower().startswith(
                    "localizationdb") and n.lower().endswith(".db")):
            blob = src.read(name)
            rows = _rowcount(blob, "Lookup")
            if rows and (best is None or rows > best[0]):
                best = (rows, blob, name, src.path)
    return best


def pick_attributedb(sources):
    best = None
    for src in sources:
        for name in src.find(
                lambda n: os.path.basename(n).lower() == "attributedb.db"):
            blob = src.read(name)
            rows = _rowcount(blob, "AttributeCache")
            if rows and (best is None or rows > best[0]):
                best = (rows, blob, name, src.path)
    return best


def pick_setdata(sources):
    """The GetSetData cache listing the most sets."""
    best = None
    for src in sources:
        for name in src.find(
                lambda n: os.path.basename(n) == "GetSetData"):
            raw = src.read(name)
            try:
                doc = json.loads(raw.decode("utf-8", "replace"))
                sets = (doc["value"]["ckSumDiff"][0]["value"]
                           ["newData"]["value"]["setDataList"])
            except Exception:
                continue
            if best is None or len(sets) > best[0]:
                best = (len(sets), sets, raw, name, src.path)
    return best


def write(target, blob, apply_it, force):
    if os.path.exists(target) and not force:
        return "already present (use --force to replace)"
    if not apply_it:
        return "would write %.1f MB" % (len(blob) / 1e6)
    tmp = target + ".part"
    with open(tmp, "wb") as fh:
        fh.write(blob)
    os.replace(tmp, target)                  # never a half-written file
    return "wrote %.1f MB" % (len(blob) / 1e6)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("caches", nargs="+", help="donated cache directories or zips")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--force", action="store_true",
                    help="replace files already in donor/")
    args = ap.parse_args(argv)

    sources = []
    for path in args.caches:
        try:
            sources.append(Source(path))
        except ValueError as exc:
            sys.exit(str(exc))
    print("scanning %d source(s), %d files (cake.cfg and output_log.txt excluded)"
          % (len(sources), sum(len(s.names) for s in sources)))

    if args.apply and not os.path.isdir(DONOR_DIR):
        os.makedirs(DONOR_DIR)

    manifest = newest_manifest(sources)
    if manifest is None:
        print("  manifest        none found")
    else:
        version, doc, raw, name, origin = manifest
        assets = sum(len(b.get("assets") or ()) for b in doc["bundles"])
        print("  manifest        v%d - %d bundles, %d asset names  [%s]"
              % (version, len(doc["bundles"]), assets, os.path.basename(origin)))
        print("                  %s" % write(
            os.path.join(DONOR_DIR, "manifest.json"), raw, args.apply, args.force))

    attributes = pick_attributedb(sources)
    if attributes is None:
        print("  AttributeDB     none found")
    else:
        rows, blob, name, origin = attributes
        print("  AttributeDB     %d archetypes  [%s]" % (rows, os.path.basename(origin)))
        print("                  %s" % write(
            os.path.join(DONOR_DIR, "AttributeDB.db"), blob, args.apply, args.force))

    localization = pick_localization(sources)
    if localization is None:
        print("  LocalizationDB  none found")
    else:
        rows, blob, name, origin = localization
        print("  LocalizationDB  %d strings  [%s]" % (rows, os.path.basename(origin)))
        print("                  %s" % write(
            os.path.join(DONOR_DIR, "LocalizationDB-UTF16.db"),
            blob, args.apply, args.force))

    setdata = pick_setdata(sources)
    if setdata is None:
        print("  GetSetData      none found")
    else:
        count, sets, raw, name, origin = setdata
        print("  GetSetData      %d sets  [%s]" % (count, os.path.basename(origin)))
        print("                  %s" % write(
            os.path.join(DONOR_DIR, "setdata.json"),
            json.dumps(sets, indent=1).encode("utf-8"), args.apply, args.force))

    if not args.apply:
        print("\n(dry run - pass --apply to write into %s)" % DONOR_DIR)
    return 0


if __name__ == "__main__":
    sys.exit(main())
