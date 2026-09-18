#!/usr/bin/env python3
"""Rewrite a PMTiles archive's DIRECTORIES without touching a single tile.

    python tools/repack_pmtiles.py out/ in/*.pmtiles

WHY THIS EXISTS RATHER THAN A REBUILD. Six of the ten published imagery packs
could not be opened by any spec-conformant reader: the builder put every entry
in the root directory, and the PMTiles v3 spec requires the header and the whole
root to fit in the first 16,384 bytes, with the rest in leaf directories. The
root of `gb-north-satellite-high` ended at byte 102,706.

The TILES in those archives are perfectly good - every one matches the sha256 the
index published. Only the index inside the file is laid out wrongly. Rebuilding
would mean refetching 143,637 tiles per country from a free service that has
done nothing wrong; repacking rewrites 127 bytes of header and a few hundred KB
of directory and copies the tile body across verbatim.

The output is byte-identical in its tile data, which is the thing worth being
able to say: this changes where the map looks, not what it sees.
"""
import argparse
import gzip
import importlib.util
import os
import sys

_spec = importlib.util.spec_from_file_location(
    "build_satellite",
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "build_satellite.py"))
bs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bs)


def read_varint(buf, i):
    shift = 0
    value = 0
    while True:
        b = buf[i]
        i += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, i
        shift += 7


def parse_directory(blob):
    """The inverse of `build_satellite.serialise_directory`."""
    i = 0
    count, i = read_varint(blob, i)
    ids = []
    last = 0
    for _ in range(count):
        delta, i = read_varint(blob, i)
        last += delta
        ids.append(last)
    runs = []
    for _ in range(count):
        run, i = read_varint(blob, i)
        runs.append(run)
    lengths = []
    for _ in range(count):
        length, i = read_varint(blob, i)
        lengths.append(length)
    offsets = []
    prev_end = None
    for k in range(count):
        raw, i = read_varint(blob, i)
        if raw == 0:
            if prev_end is None:
                raise SystemExit("first entry claims to follow nothing")
            offset = prev_end
        else:
            offset = raw - 1
        offsets.append(offset)
        prev_end = offset + lengths[k]
    return [(ids[k], offsets[k], lengths[k], runs[k]) for k in range(count)]


def u64(buf, at):
    return int.from_bytes(buf[at:at + 8], "little")


def repack(src, dst):
    with open(src, "rb") as f:
        header = bytearray(f.read(bs.HEADER_LENGTH))
        if bytes(header[:7]) != b"PMTiles" or header[7] != 3:
            raise SystemExit("%s is not a PMTiles v3 archive" % src)
        root_off, root_len = u64(header, 8), u64(header, 16)
        meta_off, meta_len = u64(header, 24), u64(header, 32)
        leaf_off, leaf_len = u64(header, 40), u64(header, 48)
        data_off, data_len = u64(header, 56), u64(header, 64)

        f.seek(root_off)
        root = gzip.decompress(f.read(root_len))
        entries = parse_directory(root)
        # A pointer entry (run_length 0) means this archive already uses
        # leaves, and unpicking those is work this tool does not need to do:
        # the archives it exists for have none.
        if any(e[3] == 0 for e in entries):
            return None
        f.seek(meta_off)
        metadata = f.read(meta_len)
        f.seek(data_off)
        body = f.read(data_len)

    new_root, leaves, count = bs.build_directories(entries)
    new_root_off = bs.HEADER_LENGTH
    new_meta_off = new_root_off + len(new_root)
    new_leaf_off = new_meta_off + len(metadata)
    new_data_off = new_leaf_off + len(leaves)

    header[8:16] = new_root_off.to_bytes(8, "little")
    header[16:24] = len(new_root).to_bytes(8, "little")
    header[24:32] = new_meta_off.to_bytes(8, "little")
    header[32:40] = len(metadata).to_bytes(8, "little")
    header[40:48] = new_leaf_off.to_bytes(8, "little")
    header[48:56] = len(leaves).to_bytes(8, "little")
    header[56:64] = new_data_off.to_bytes(8, "little")
    header[64:72] = len(body).to_bytes(8, "little")

    os.makedirs(os.path.dirname(os.path.abspath(dst)), exist_ok=True)
    with open(dst, "wb") as f:
        f.write(header)
        f.write(new_root)
        f.write(metadata)
        f.write(leaves)
        f.write(body)
    return (len(entries), count, bs.HEADER_LENGTH + len(new_root), len(body))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("out")
    ap.add_argument("archives", nargs="+")
    args = ap.parse_args()
    for src in args.archives:
        dst = os.path.join(args.out, os.path.basename(src))
        got = repack(src, dst)
        if got is None:
            print("%-34s already uses leaves, left alone"
                  % os.path.basename(src))
            continue
        entries, leaves, root_end, body = got
        print("%-34s %8d entries, %4d leaves, header+root %5d B, body %d B"
              % (os.path.basename(src), entries, leaves, root_end, body))


if __name__ == "__main__":
    main()
