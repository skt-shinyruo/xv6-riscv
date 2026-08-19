#!/usr/bin/env python3
"""Read-only xv6 image checker with validated shadow-log replay."""

import argparse
import hashlib
import json
import struct
import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from pathlib import Path


BSIZE = 1024
FSMAGIC = 0x10203040
ROOTINO = 1
NDIRECT = 12
NINDIRECT = BSIZE // 4
MAXFILE = NDIRECT + NINDIRECT
IPB = BSIZE // 64
BPB = BSIZE * 8
LOGBLOCKS = 30
T_DIR = 1
T_FILE = 2
T_DEVICE = 3
VALID_TYPES = {T_DIR, T_FILE, T_DEVICE}
DINODE = struct.Struct("<hhhhI13I")
DIRENT = struct.Struct("<H14s")
SUPER = struct.Struct("<8I")


class ImageError(Exception):
    pass


@dataclass(frozen=True)
class Geometry:
    size: int
    nblocks: int
    ninodes: int
    nlog: int
    logstart: int
    inodestart: int
    bmapstart: int
    data_start: int


@dataclass(frozen=True)
class Inode:
    inum: int
    type: int
    major: int
    minor: int
    nlink: int
    size: int
    addrs: tuple


class Diagnostics:
    def __init__(self):
        self.items = set()

    def add(self, code, **fields):
        detail = ",".join(f"{key}={fields[key]}" for key in sorted(fields))
        self.items.add(code if not detail else f"{code}:{detail}")

    def codes(self):
        return sorted(self.items)


def block(data, number):
    start = number * BSIZE
    return data[start:start + BSIZE]


def parse_geometry(data, diagnostics):
    if len(data) < 2 * BSIZE:
        diagnostics.add("E_IMAGE_TRUNCATED", bytes=len(data))
        return None
    magic, size, nblocks, ninodes, nlog, logstart, inodestart, bmapstart = \
        SUPER.unpack_from(data, BSIZE)
    if magic != FSMAGIC:
        diagnostics.add("E_SUPER_MAGIC", observed=hex(magic))
        return None
    nbitmap = (size + BPB - 1) // BPB
    data_start = bmapstart + nbitmap
    valid = (
        size > 0 and len(data) == size * BSIZE and
        0 < ninodes <= size * IPB and
        nlog == LOGBLOCKS + 1 and
        logstart == 2 and
        inodestart == logstart + nlog and
        bmapstart == inodestart + (ninodes + IPB - 1) // IPB and
        data_start < size and nblocks == size - data_start
    )
    if not valid:
        diagnostics.add("E_SUPER_GEOMETRY", bytes=len(data), size=size)
        return None
    return Geometry(size, nblocks, ninodes, nlog, logstart, inodestart,
                    bmapstart, data_start)


def parse_log(data, geometry, diagnostics):
    header = block(data, geometry.logstart)
    count = struct.unpack_from("<i", header, 0)[0]
    if count < 0 or count > LOGBLOCKS:
        diagnostics.add("E_LOG_COUNT", count=count)
        return count, (), None
    targets = struct.unpack_from(f"<{count}I", header, 4) if count else ()
    seen = set()
    for index, target in enumerate(targets):
        if target < geometry.inodestart or target >= geometry.size or \
                geometry.logstart <= target < geometry.logstart + geometry.nlog:
            diagnostics.add("E_LOG_TARGET", index=index, target=target)
        if target in seen:
            diagnostics.add("E_LOG_DUP", index=index, target=target)
        seen.add(target)
    if diagnostics.codes():
        return count, targets, None
    shadow = bytearray(data)
    for index, target in enumerate(targets):
        source = geometry.logstart + 1 + index
        shadow[target * BSIZE:(target + 1) * BSIZE] = block(data, source)
    struct.pack_into("<i", shadow, geometry.logstart * BSIZE, 0)
    return count, targets, bytes(shadow)


def parse_inodes(data, geometry, diagnostics):
    inodes = {}
    for inum in range(1, geometry.ninodes):
        offset = geometry.inodestart * BSIZE + inum * DINODE.size
        values = DINODE.unpack_from(data, offset)
        inode = Inode(inum, values[0], values[1], values[2], values[3],
                      values[4], tuple(values[5:]))
        if inode.type == 0:
            continue
        if inode.type not in VALID_TYPES:
            diagnostics.add("E_INODE_TYPE", inum=inum, type=inode.type)
            continue
        if inode.size > MAXFILE * BSIZE:
            diagnostics.add("E_INODE_SIZE", inum=inum, size=inode.size)
        inodes[inum] = inode
    if ROOTINO not in inodes or inodes[ROOTINO].type != T_DIR:
        diagnostics.add("E_ROOT")
    return inodes


def inode_blocks(data, geometry, inode, diagnostics):
    addresses = []
    logical = list(inode.addrs[:NDIRECT]) + [0] * NINDIRECT
    for index, address in enumerate(logical[:NDIRECT]):
        if address:
            addresses.append(("data", index, address))
    indirect = inode.addrs[NDIRECT]
    if indirect:
        addresses.append(("indirect", NDIRECT, indirect))
        if geometry.data_start <= indirect < geometry.size:
            indirect_entries = struct.unpack("<256I", block(data, indirect))
            logical[NDIRECT:] = indirect_entries
            for index, address in enumerate(indirect_entries):
                if address:
                    addresses.append(("data", NDIRECT + index, address))
    required = (inode.size + BSIZE - 1) // BSIZE
    for index, address in enumerate(logical):
        if index < required and not address:
            diagnostics.add("E_INODE_HOLE", index=index, inum=inode.inum)
        elif index >= required and address:
            diagnostics.add("E_INODE_TRAILING", index=index, inum=inode.inum)
    if required <= NDIRECT and indirect:
        diagnostics.add("E_INODE_TRAILING", index=NDIRECT, inum=inode.inum)
    for kind, index, address in addresses:
        if not geometry.data_start <= address < geometry.size:
            diagnostics.add("E_BLOCK_REGION", block=address, index=index,
                            inum=inode.inum, kind=kind)
    return addresses


def bitmap_bit(data, geometry, number):
    bitmap_block = geometry.bmapstart + number // BPB
    offset = number % BPB
    return (block(data, bitmap_block)[offset // 8] >> (offset % 8)) & 1


def read_inode_bytes(data, geometry, inode, diagnostics):
    chunks = []
    data_blocks = {index: address for kind, index, address in
                   inode_blocks(data, geometry, inode, diagnostics)
                   if kind == "data" and geometry.data_start <= address < geometry.size}
    remaining = inode.size
    for index in range((inode.size + BSIZE - 1) // BSIZE):
        address = data_blocks.get(index)
        if address is None:
            diagnostics.add("E_INODE_HOLE", index=index, inum=inode.inum)
            count = min(remaining, BSIZE)
            chunks.append(bytes(count))
            remaining -= count
            continue
        count = min(remaining, BSIZE)
        chunks.append(block(data, address)[:count])
        remaining -= count
    return b"".join(chunks)


def decode_name(raw):
    return raw.split(b"\0", 1)[0]


def inspect_view(data, geometry):
    diagnostics = Diagnostics()
    inodes = parse_inodes(data, geometry, diagnostics)
    owners = defaultdict(list)
    for inode in inodes.values():
        for kind, index, address in inode_blocks(data, geometry, inode, diagnostics):
            if geometry.data_start <= address < geometry.size:
                owners[address].append((inode.inum, kind, index))
    for number, entries in owners.items():
        if len(entries) > 1:
            diagnostics.add("E_BLOCK_DUP", block=number, owners=len(entries))
        if not bitmap_bit(data, geometry, number):
            diagnostics.add("E_BITMAP_UNMARKED", block=number)
    for number in range(geometry.data_start):
        if not bitmap_bit(data, geometry, number):
            diagnostics.add("E_BITMAP_METADATA", block=number)
    for number in range(geometry.data_start, geometry.size):
        if bitmap_bit(data, geometry, number) and number not in owners:
            diagnostics.add("E_BITMAP_LEAK", block=number)

    edges = defaultdict(list)
    directory_children = defaultdict(int)
    dots = set()
    dotdot = {}
    references = defaultdict(int)
    for inode in inodes.values():
        if inode.type != T_DIR:
            continue
        payload = read_inode_bytes(data, geometry, inode, diagnostics)
        if len(payload) % DIRENT.size:
            diagnostics.add("E_DIR_SIZE", inum=inode.inum, size=len(payload))
        names = set()
        for offset in range(0, len(payload) - DIRENT.size + 1, DIRENT.size):
            target, raw_name = DIRENT.unpack_from(payload, offset)
            if target == 0:
                continue
            name = decode_name(raw_name)
            if not name:
                diagnostics.add("E_DIR_NAME", inum=inode.inum, offset=offset)
                continue
            if name in names:
                diagnostics.add("E_DIR_NAME_DUP", inum=inode.inum,
                                name=name.hex())
            names.add(name)
            if target not in inodes:
                diagnostics.add("E_DIRENT_INUM", inum=inode.inum, target=target)
                continue
            if name == b".":
                dots.add(inode.inum)
                if target != inode.inum:
                    diagnostics.add("E_DOT", inum=inode.inum, target=target)
            elif name == b"..":
                dotdot[inode.inum] = target
            else:
                edges[inode.inum].append((name, target))
                references[target] += 1

    reachable = set()
    parents = {ROOTINO: ROOTINO}
    directory_incoming = defaultdict(int)
    queue = deque([ROOTINO]) if ROOTINO in inodes else deque()
    while queue:
        current = queue.popleft()
        if current in reachable:
            continue
        reachable.add(current)
        for _, target in edges[current]:
            child = inodes.get(target)
            if child and child.type == T_DIR:
                directory_children[current] += 1
                directory_incoming[target] += 1
                if directory_incoming[target] > 1:
                    diagnostics.add("E_DIR_MULTIPARENT", inum=target)
                if target in parents and parents[target] != current:
                    diagnostics.add("E_DIR_PARENT", inum=target)
                else:
                    parents[target] = current
                if target in reachable:
                    diagnostics.add("E_DIR_CYCLE", inum=target)
                else:
                    queue.append(target)
            elif child:
                reachable.add(target)
    for inum, inode in inodes.items():
        if inode.type == T_DIR and inum not in dots:
            diagnostics.add("E_DOT", inum=inum, target=-1)
        if inum not in reachable:
            if inode.nlink == 0:
                diagnostics.add("E_ORPHAN", inum=inum)
            else:
                diagnostics.add("E_UNREACHABLE", inum=inum)
        if inode.type == T_DIR and inum in reachable:
            expected_parent = ROOTINO if inum == ROOTINO else parents.get(inum)
            if dotdot.get(inum) != expected_parent:
                diagnostics.add("E_DOTDOT", inum=inum,
                                target=dotdot.get(inum, -1))
        if inode.type == T_DIR:
            expected_links = 1 + directory_children[inum]
            if inum in reachable and inode.nlink != expected_links:
                diagnostics.add("E_NLINK", expected=expected_links,
                                inum=inum, observed=inode.nlink)
        elif inode.nlink != references[inum]:
            diagnostics.add("E_NLINK", expected=references[inum],
                            inum=inum, observed=inode.nlink)

    return {
        "allocated_inodes": len(inodes),
        "owned_blocks": len(owners),
        "reachable_inodes": len(reachable),
        "diagnostics": diagnostics.codes(),
    }


def check_bytes(data):
    top = Diagnostics()
    geometry = parse_geometry(data, top)
    if geometry is None:
        return {
            "clean": False, "raw_clean": False, "recovered_clean": False,
            "log_pending": None, "diagnostics": top.codes(),
        }
    count, targets, shadow = parse_log(data, geometry, top)
    if shadow is None:
        return {
            "clean": False,
            "raw_clean": False,
            "recovered_clean": False,
            "log_pending": None,
            "geometry": geometry.__dict__,
            "log": {"count": count, "targets": list(targets)},
            "diagnostics": top.codes(),
        }
    raw = inspect_view(data, geometry)
    replayed = inspect_view(shadow, geometry)
    raw_clean = not top.codes() and not raw["diagnostics"]
    recovered_clean = not top.codes() and not replayed["diagnostics"]
    return {
        "clean": recovered_clean,
        "raw_clean": raw_clean,
        "recovered_clean": recovered_clean,
        "log_pending": count != 0,
        "geometry": geometry.__dict__,
        "log": {"count": count, "targets": list(targets)},
        "raw_sha256": hashlib.sha256(data).hexdigest(),
        "shadow_sha256": hashlib.sha256(shadow).hexdigest(),
        "raw": raw,
        "replayed": replayed,
        "diagnostics": top.codes() + replayed["diagnostics"],
    }


def check_path(path):
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = check_bytes(path.read_bytes())
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    if before != after:
        raise ImageError("checker modified its input")
    result["input_sha256"] = before
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    args = parser.parse_args()
    try:
        result = check_path(args.image.resolve(strict=True))
    except (ImageError, OSError, struct.error, ValueError) as exc:
        print(json.dumps({"clean": False, "error": str(exc)}, sort_keys=True))
        raise SystemExit(2)
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    raise SystemExit(0 if result["clean"] else 1)


if __name__ == "__main__":
    main()
