#!/usr/bin/env python3
"""Reader/writer for Cemu "FileCache" containers (pure Python, stdlib only).

This mirrors src/Cemu/FileCache/FileCache.cpp in Cemu (and Fractal, its Android fork).
Transferable shader caches use it:

    shaderCache/transferable/<titleid>_shaders.bin     guest shaders
    shaderCache/transferable/<titleid>_vkpipeline.bin  Vulkan pipeline states

On-disk layout (all integers little-endian, as Cemu writes them with native x86/ARM byte order):

    0x00  u32  magic         0x8371b696 (V3, zlib-capable); 0x8371b695 (V2) is still readable,
                             0x8371b694 (V1) is rejected by Cemu
    0x04  u32  extraVersion  checked by the caller against an expected value (see below)
    0x08  u64  dataOffset    always 128 in files written by Cemu (the header is padded to 128 bytes)
    0x10  u64  tableOffset   relative to dataOffset
    0x18  u32  tableSize     bytes; must be a multiple of 0x20
    (zero padding up to dataOffset)

    File table at dataOffset + tableOffset, tableSize / 0x20 entries of:
    0x00  u64  name1
    0x08  u64  name2
    0x10  u64  offset        relative to dataOffset
    0x18  u32  size          size of the stored (raw) data
    0x1C  u8   flags         bit0 = zlib compressed (ignored and treated as 0 in V2 files)
    0x1D  u8[3] reserved

    Special names: (0, 0) is a free slot; (0xEFEFEFEFEFEFEFEF, 0xFEFEFEFEFEFEFEFE) is the file table's own
    entry (it describes the table itself and is never returned as a file).

    A compressed entry is a big-endian u32 with the uncompressed size followed by one zlib stream
    (compress2/uncompress2). Cemu requires the stream to consume exactly size - 4 bytes and produce
    exactly the announced size.

extraVersion expected by Cemu (titleId = the 64-bit title id in the file name):
    shaders     ((titleId >> 32) + (titleId & 0xffffffff) * 3 + 1 + 0xe97af1ad) mod 2**32
                (the legacy value 2 from Cemu 1.25.0 - 1.25.1b is also accepted when opening)
    vkpipeline  ((titleId >> 32) + (titleId & 0xffffffff) * 3 + 1) mod 2**32
"""

import struct
import zlib

MAGIC_V1 = 0x8371B694
MAGIC_V2 = 0x8371B695
MAGIC_V3 = 0x8371B696

HEADER_FMT = "<IIQQI"
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 28
HEADER_RESERVED = 128
ENTRY_FMT = "<QQQIB3s"
ENTRY_SIZE = struct.calcsize(ENTRY_FMT)  # 32
assert ENTRY_SIZE == 0x20

TABLE_NAME = (0xEFEFEFEFEFEFEFEF, 0xFEFEFEFEFEFEFEFE)
FREE_NAME = (0, 0)
FLAG_COMPRESSED = 0x01

SHADER_CACHE_LEGACY_EXTRA_VERSION = 2

# Safety limits for untrusted input (far above anything a real game produces).
MAX_ENTRIES = 2_000_000
MAX_ENTRY_UNCOMPRESSED = 64 * 1024 * 1024


class CacheError(Exception):
    pass


def shader_extra_version(title_id):
    """extraVersion of <titleid>_shaders.bin (LatteShaderCache_getShaderCacheExtraVersion)."""
    return ((title_id >> 32) + (title_id & 0xFFFFFFFF) * 3 + 1 + 0xE97AF1AD) & 0xFFFFFFFF


def pipeline_extra_version(title_id):
    """extraVersion of <titleid>_vkpipeline.bin (LatteShaderCache_getPipelineCacheExtraVersion)."""
    return ((title_id >> 32) + (title_id & 0xFFFFFFFF) * 3 + 1) & 0xFFFFFFFF


def expected_extra_version(kind, title_id):
    if kind == "shaders":
        return shader_extra_version(title_id)
    if kind == "vkpipeline":
        return pipeline_extra_version(title_id)
    raise ValueError("unknown cache kind %r" % kind)


def zlib_unpack(raw, max_size=MAX_ENTRY_UNCOMPRESSED):
    """Decode a compressed entry exactly like Cemu's _uncompressFileData."""
    if len(raw) < 4:
        raise CacheError("compressed entry shorter than 4 bytes")
    size = struct.unpack(">I", raw[:4])[0]
    if size > max_size:
        raise CacheError("compressed entry announces %d bytes (limit %d)" % (size, max_size))
    d = zlib.decompressobj()
    try:
        out = d.decompress(raw[4:], size + 1)
    except zlib.error as e:
        raise CacheError("zlib error: %s" % e)
    if not d.eof:
        raise CacheError("zlib stream incomplete or larger than announced size")
    if d.unused_data or d.unconsumed_tail:
        raise CacheError("trailing bytes after zlib stream")
    if len(out) != size:
        raise CacheError("zlib output is %d bytes, header says %d" % (len(out), size))
    return out


def zlib_pack(data, level=9):
    """Encode like Cemu's _fileCache_compressFileData (Cemu uses level 4; any level decodes the same)."""
    return struct.pack(">I", len(data)) + zlib.compress(data, level)


class Entry:
    __slots__ = ("name1", "name2", "raw", "compressed")

    def __init__(self, name1, name2, raw, compressed=False):
        self.name1 = name1
        self.name2 = name2
        self.raw = bytes(raw)
        self.compressed = bool(compressed)

    @property
    def key(self):
        return (self.name1, self.name2)

    def data(self):
        return zlib_unpack(self.raw) if self.compressed else self.raw

    @classmethod
    def from_data(cls, name1, name2, data, compress=False, level=9):
        if compress:
            packed = zlib_pack(data, level)
            if len(packed) < len(data):
                return cls(name1, name2, packed, True)
        return cls(name1, name2, data, False)


class FileCache:
    def __init__(self, extra_version=0, magic=MAGIC_V3):
        self.magic = magic
        self.extra_version = extra_version
        self.entries = []        # normal entries in table order (no free slots, no table entry)
        self.table_slots = 0     # number of slots in the table (as read)
        self.file_size = 0
        self.warnings = []
        self._keys = None

    # ---------------------------------------------------------------- reading
    @classmethod
    def load(cls, path):
        with open(path, "rb") as f:
            return cls.parse(f.read())

    @classmethod
    def parse(cls, buf):
        """Parse a FileCache. Raises CacheError for anything Cemu would refuse or that is corrupt."""
        buf = memoryview(buf)
        n = len(buf)
        if n < HEADER_SIZE:
            raise CacheError("file too small for a header (%d bytes)" % n)
        magic, extra, data_off, table_off, table_size = struct.unpack_from(HEADER_FMT, buf, 0)
        if magic == MAGIC_V1:
            raise CacheError("V1 cache (Cemu < 1.7.4) is not supported by Cemu anymore")
        if magic not in (MAGIC_V2, MAGIC_V3):
            raise CacheError("bad magic 0x%08x (not a Cemu FileCache)" % magic)
        if table_size == 0 or table_size % ENTRY_SIZE:
            raise CacheError("bad file table size %d (must be a non-zero multiple of 32)" % table_size)
        if data_off < HEADER_SIZE:
            raise CacheError("data offset %d overlaps the header" % data_off)
        slots = table_size // ENTRY_SIZE
        if slots > MAX_ENTRIES:
            raise CacheError("file table has %d slots (limit %d)" % (slots, MAX_ENTRIES))
        tpos = data_off + table_off
        if tpos + table_size > n:
            raise CacheError("file table (0x%x + 0x%x) runs past end of file (0x%x): incomplete cache"
                             % (tpos, table_size, n))
        fc = cls(extra, magic)
        fc.table_slots = slots
        fc.file_size = n
        seen = set()
        table_entry_ok = False
        ranges = []
        for i in range(slots):
            name1, name2, off, size, flags, resv = struct.unpack_from(ENTRY_FMT, buf, tpos + i * ENTRY_SIZE)
            if magic == MAGIC_V2:
                flags = 0  # V2 did not define the extra bytes
            key = (name1, name2)
            if key == FREE_NAME:
                continue
            if key == TABLE_NAME:
                if off == table_off and size == table_size:
                    table_entry_ok = True
                else:
                    raise CacheError("file table entry (slot %d) does not describe the table" % i)
                ranges.append((off, size, i))
                continue
            if flags & ~FLAG_COMPRESSED:
                fc.warnings.append("slot %d: unknown flag bits 0x%02x" % (i, flags))
            if key in seen:
                raise CacheError("duplicate entry %016x:%016x (slot %d)" % (name1, name2, i))
            seen.add(key)
            start = data_off + off
            if start + size > n:
                raise CacheError("entry %016x:%016x (slot %d) runs past end of file" % (name1, name2, i))
            ranges.append((off, size, i))
            fc.entries.append(Entry(name1, name2, buf[start:start + size], flags & FLAG_COMPRESSED))
        if not table_entry_ok:
            # Cemu does not need it to read, but would later write new data over the table.
            raise CacheError("file table has no self entry")
        ranges.sort()
        end_prev, slot_prev = 0, None
        for off, size, i in ranges:
            if size and off < end_prev:
                raise CacheError("slot %d overlaps slot %s" % (i, slot_prev))
            if size and off + size > end_prev:
                end_prev, slot_prev = off + size, i
        return fc

    def check_payloads(self):
        """Decode every compressed entry. Raises CacheError on the first bad one."""
        for e in self.entries:
            if e.compressed:
                try:
                    zlib_unpack(e.raw)
                except CacheError as err:
                    raise CacheError("entry %016x:%016x: %s" % (e.name1, e.name2, err))
            elif len(e.raw) > MAX_ENTRY_UNCOMPRESSED:
                raise CacheError("entry %016x:%016x is %d bytes" % (e.name1, e.name2, len(e.raw)))

    # ---------------------------------------------------------------- editing
    def keys(self):
        return {e.key for e in self.entries}

    def add(self, entry):
        """Append an entry unless its name already exists (first one wins). Returns True if added."""
        if entry.key in (FREE_NAME, TABLE_NAME):
            raise CacheError("reserved entry name %016x:%016x" % entry.key)
        if self._keys is None:
            self._keys = self.keys()
        if entry.key in self._keys:
            return False
        self._keys.add(entry.key)
        self.entries.append(entry)
        return True

    # ---------------------------------------------------------------- writing
    def to_bytes(self, compress="keep", level=9, spare_slots=32):
        """Serialize as a V3 FileCache the way FileCache::Create lays it out.

        compress: "keep" stores each entry as it was read, "none" stores everything uncompressed
        (what Cemu writes at runtime), "zlib" compresses every entry where that saves space.
        The table goes first (tableOffset 0, like a freshly created cache) with some free slots so
        Cemu can append new entries without relocating the table.
        """
        entries = []
        for e in self.entries:
            if compress == "keep":
                entries.append(e)
            elif compress == "none":
                entries.append(Entry(e.name1, e.name2, e.data(), False))
            elif compress == "zlib":
                entries.append(e if e.compressed else Entry.from_data(e.name1, e.name2, e.raw, True, level))
            else:
                raise ValueError("compress must be keep, none or zlib")
        slots = len(entries) + 1 + spare_slots
        slots = (slots + 31) // 32 * 32
        table_size = slots * ENTRY_SIZE
        if table_size > 0xFFFFFFFF:
            raise CacheError("too many entries")
        table = bytearray(table_size)
        struct.pack_into(ENTRY_FMT, table, 0, TABLE_NAME[0], TABLE_NAME[1], 0, table_size, 0, b"\0\0\0")
        off = table_size
        for i, e in enumerate(entries, 1):
            if len(e.raw) > 0xFFFFFFFF:
                raise CacheError("entry too large")
            struct.pack_into(ENTRY_FMT, table, i * ENTRY_SIZE, e.name1, e.name2, off, len(e.raw),
                             FLAG_COMPRESSED if e.compressed else 0, b"\0\0\0")
            off += len(e.raw)
        header = bytearray(HEADER_RESERVED)
        struct.pack_into(HEADER_FMT, header, 0, MAGIC_V3, self.extra_version, HEADER_RESERVED, 0, table_size)
        return b"".join([bytes(header), bytes(table)] + [e.raw for e in entries])

    def save(self, path, **kw):
        data = self.to_bytes(**kw)
        with open(path, "wb") as f:
            f.write(data)
        return len(data)


def describe_extra_version(extra, title_id=None, kind=None):
    """Human readable explanation of an extraVersion value."""
    if title_id is not None:
        if extra == shader_extra_version(title_id):
            return "shaders of %016x" % title_id
        if extra == pipeline_extra_version(title_id):
            return "vkpipeline of %016x" % title_id
    if extra == SHADER_CACHE_LEGACY_EXTRA_VERSION:
        return "legacy shaders (Cemu 1.25.0 - 1.25.1b)"
    return "unknown"


if __name__ == "__main__":
    import sys
    for p in sys.argv[1:]:
        fc = FileCache.load(p)
        fc.check_payloads()
        print("%s: %d entries, extraVersion 0x%08x, magic 0x%08x" % (p, len(fc.entries), fc.extra_version, fc.magic))
