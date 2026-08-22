"""
Writes the client's on-disk archetype cache from carddata/.

WargArchetypesSource.loadFromDisk() reads persistentDataPath/archetypes/:

    checksum          plain text
    keys              protobuf-net UUID[]            (repeated field 1)
    <archetype-guid>  protobuf-net IList<Attribute>  (repeated field 1)

protobuf-net serializes a bare list/array as repeated field 1, each element
length-delimited.

Populating this is better than shipping all 9,940 archetypes over the wire:
the client loads them locally, then asks the server to confirm the checksum,
and the server replies AllArchetypesChecksumMatch. No 9.7MB frame.

Run once after regenerating carddata/, then restart the client.
"""

import os
import shutil
import sys

import server  # reuse the protobuf encoders and card loader

# persistentDataPath/archetypes - name comes from sausage-core string 'ad'
ARCH_DIR = os.path.join(
    os.environ["USERPROFILE"], "AppData", "LocalLow",
    "The Pokémon Company International", "Pokemon Trading Card Game Online",
    "archetypes",
)


def main():
    cards = server.load_cards()
    if not cards:
        sys.exit("no card data - run the exporter first")

    if os.path.isdir(ARCH_DIR):
        shutil.rmtree(ARCH_DIR)
    os.makedirs(ARCH_DIR)

    # keys: UUID[]
    keys = b""
    for a in cards:
        keys += server._len_field(1, server.pb_uuid_lohi(a["lo"], a["hi"]))
    with open(os.path.join(ARCH_DIR, "keys"), "wb") as fh:
        fh.write(keys)

    # one file per archetype, named by its GUID
    for a in cards:
        guid = server.uuid_to_guid_str(a["lo"], a["hi"])
        body = b""
        for at in a.get("attrs", []):
            body += server._len_field(
                1, server.pb_attribute(at["n"], server.pb_object(at.get("v"))))
        with open(os.path.join(ARCH_DIR, guid), "wb") as fh:
            fh.write(body)

    with open(os.path.join(ARCH_DIR, "checksum"), "w", encoding="utf-8") as fh:
        fh.write(server.CARD_CHECKSUM)

    print("wrote %d archetypes to %s" % (len(cards), ARCH_DIR))
    print("checksum: %s" % server.CARD_CHECKSUM)


if __name__ == "__main__":
    main()
