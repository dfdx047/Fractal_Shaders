#!/usr/bin/env python3
"""Merge Cemu FileCache shader/pipeline caches, or print information about them.

    merge_cache.py --out OUT IN1 [IN2 ...]   union of entries keyed by (name1, name2); first one wins
    merge_cache.py --info FILE [FILE ...]    entry count, extraVersion and size

All inputs must have the same magic and extraVersion (extraVersion encodes the title id and the kind
of cache, so this also refuses mixing games or mixing _shaders.bin with _vkpipeline.bin).
"""

import argparse
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filecache import CacheError, FileCache, describe_extra_version  # noqa: E402

NAME_RE = re.compile(r"^([0-9a-fA-F]{16})_(shaders|vkpipeline)\.bin$")


def title_from_name(path):
    m = NAME_RE.match(os.path.basename(path))
    return (int(m.group(1), 16), m.group(2)) if m else (None, None)


def load_checked(path):
    fc = FileCache.load(path)
    fc.check_payloads()
    return fc


def merge(paths):
    """Return (merged FileCache, per-input added counts). Raises CacheError with the reason."""
    if not paths:
        raise CacheError("no input files")
    caches = []
    for p in paths:
        try:
            caches.append(load_checked(p))
        except (OSError, CacheError) as e:
            raise CacheError("%s: %s" % (p, e))
    first = caches[0]
    for p, fc in zip(paths[1:], caches[1:]):
        if fc.magic != first.magic:
            raise CacheError("%s: magic 0x%08x differs from %s (0x%08x)" % (p, fc.magic, paths[0], first.magic))
        if fc.extra_version != first.extra_version:
            tid, _ = title_from_name(p)
            raise CacheError(
                "%s: extraVersion 0x%08x (%s) differs from %s (0x%08x, %s): different game or cache kind"
                % (p, fc.extra_version, describe_extra_version(fc.extra_version, tid),
                   paths[0], first.extra_version, describe_extra_version(first.extra_version, title_from_name(paths[0])[0])))
    out = FileCache(first.extra_version)
    added = []
    for fc in caches:
        n = 0
        for e in fc.entries:
            if out.add(e):
                n += 1
        added.append(n)
    return out, added


def info(path):
    fc = load_checked(path)
    tid, kind = title_from_name(path)
    comp = sum(1 for e in fc.entries if e.compressed)
    print("%s\n  size %d bytes, magic 0x%08x, extraVersion 0x%08x (%s)\n  entries %d (%d zlib), table slots %d"
          % (path, fc.file_size, fc.magic, fc.extra_version, describe_extra_version(fc.extra_version, tid),
             len(fc.entries), comp, fc.table_slots))
    for w in fc.warnings:
        print("  warning:", w)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", help="output file")
    ap.add_argument("--info", action="store_true", help="print information about the given files")
    ap.add_argument("--compress", choices=["none", "keep", "zlib"], default="none",
                    help="entry storage in the output (default none, like Cemu at runtime)")
    ap.add_argument("inputs", nargs="+")
    a = ap.parse_args()
    try:
        if a.info:
            for p in a.inputs:
                info(p)
            return 0
        if not a.out:
            ap.error("--out is required unless --info is given")
        out, added = merge(a.inputs)
        tmp = a.out + ".part"
        size = out.save(tmp, compress=a.compress)
        os.replace(tmp, a.out)
        for p, n in zip(a.inputs, added):
            print("%s: +%d" % (p, n))
        print("%s: %d entries, %d bytes, extraVersion 0x%08x" % (a.out, len(out.entries), size, out.extra_version))
        return 0
    except (OSError, CacheError) as e:
        print("error:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
