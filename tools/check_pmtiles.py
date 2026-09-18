#!/usr/bin/env python3
"""Refuse to publish a PMTiles archive no reader can open.

    python tools/check_pmtiles.py satellite/*.pmtiles height/*.pmtiles

THE FAILURE THIS EXISTS FOR SHIPPED, and shipped silently. Six of the ten
published imagery packs - every high-detail pack, and the whole of the North -
could not be opened by the app at all. They downloaded, matched their sha256,
and sat on the phone doing nothing, because the builder wrote every directory
entry into the ROOT and the PMTiles v3 spec requires the header and the whole
root to fit inside the first 16,384 bytes so a reader can fetch both in one
range request. The root of `gb-north-satellite-high` ended at byte 102,706.

Nothing caught it because every check the pipeline had asked whether the BYTES
were the bytes: the sha256 matched, the length matched, the file downloaded
cleanly. None of them asked whether the file was a valid archive. A checksum
proves a file arrived intact; it says nothing about whether it was ever right.
"""
import argparse
import os
import struct
import sys

ROOT_LIMIT = 16384
HEADER_LENGTH = 127


def problems_with(path):
    out = []
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(HEADER_LENGTH)
    if len(head) < HEADER_LENGTH:
        return ["shorter than a header"]
    if head[:7] != b"PMTiles":
        return ["not a PMTiles archive (magic is %r)" % head[:7]]
    if head[7] != 3:
        return ["PMTiles version %d, and this app reads v3" % head[7]]

    def u64(at):
        return struct.unpack_from("<Q", head, at)[0]

    root_off, root_len = u64(8), u64(16)
    leaf_off, leaf_len = u64(40), u64(48)
    data_off, data_len = u64(56), u64(64)

    # THE ONE THAT SHIPPED.
    if root_off + root_len > ROOT_LIMIT:
        out.append(
            "header and root directory end at byte %d, past the spec's %d - "
            "no conformant reader will open this. The entries belong in leaf "
            "directories; see build_directories in tools/build_satellite.py."
            % (root_off + root_len, ROOT_LIMIT))
    # And the ordinary ways a file can be wrong, while we are here.
    for name, off, length in (("root", root_off, root_len),
                              ("leaf", leaf_off, leaf_len),
                              ("data", data_off, data_len)):
        if off + length > size:
            out.append("%s section runs to byte %d, past the end of a %d byte "
                       "file" % (name, off + length, size))
    if root_len == 0:
        out.append("no root directory at all")
    if data_len == 0:
        out.append("no tile data at all")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("archives", nargs="+")
    args = ap.parse_args()

    bad = 0
    for path in args.archives:
        if not os.path.isfile(path):
            print("MISSING  %s" % path)
            bad += 1
            continue
        found = problems_with(path)
        name = os.path.basename(path)
        if found:
            bad += 1
            print("REFUSED  %s" % name)
            for p in found:
                print("           %s" % p)
        else:
            print("ok       %s" % name)
    if bad:
        print("\n%d archive(s) would not open on a rider's phone." % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
