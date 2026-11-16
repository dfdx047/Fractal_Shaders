#!/usr/bin/env python3
"""Validate shader cache submissions.

A submission is one folder:

    submissions/<titleid>/<name>/<titleid>_shaders.bin      (optional)
    submissions/<titleid>/<name>/<titleid>_vkpipeline.bin   (optional, at least one of the two)
    submissions/<titleid>/<name>/meta.json                  (required)

<titleid> is 16 lowercase hex digits (the base game, 00050000xxxxxxxx). <name> is free
([A-Za-z0-9._-], up to 64 characters, e.g. "<device>-<date>").

Usage:
    validate_submission.py --root REPO --diff BASE HEAD   check the files a PR adds (CI)
    validate_submission.py --root REPO --all              check every folder under submissions/
    validate_submission.py --root REPO PATH...            check the given changed paths
"""

import argparse
import json
import os
import re
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filecache import CacheError, FileCache, expected_extra_version, describe_extra_version  # noqa: E402

MAX_FILE_BYTES = 95 * 1024 * 1024
MAX_META_BYTES = 16 * 1024
MAX_FOLDERS_PER_PR = 20
KINDS = ("shaders", "vkpipeline")
TITLE_RE = re.compile(r"^[0-9a-f]{16}$")
FOLDER_RE = re.compile(r"^[A-Za-z0-9_-][A-Za-z0-9._-]{0,63}$")
META_REQUIRED = ("titleId", "gameName")
META_OPTIONAL = ("appVersion", "device", "gpu", "driver")
META_MAX_KEYS = 32
META_MAX_STR = 200


def cache_file_name(title_id, kind):
    return "%s_%s.bin" % (title_id, kind)


def allowed_names(title_id):
    return {cache_file_name(title_id, k): k for k in KINDS} | {"meta.json": "meta"}


def check_path(rel):
    """Check a repository path a PR touches. Returns (title_id, folder) or raises ValueError."""
    parts = rel.split("/")
    if parts[0] != "submissions":
        raise ValueError("%s: pull requests with submissions may only change files under submissions/" % rel)
    if len(parts) != 4:
        raise ValueError("%s: expected submissions/<titleid>/<name>/<file>" % rel)
    _, tid, folder, name = parts
    if not TITLE_RE.match(tid):
        raise ValueError("%s: '%s' is not a 16 digit lowercase hex title id" % (rel, tid))
    if not tid.startswith("00050000"):
        raise ValueError("%s: use the base game title id (00050000...), not an update or DLC id" % rel)
    if not FOLDER_RE.match(folder):
        raise ValueError("%s: folder name '%s' must be 1-64 characters of A-Z a-z 0-9 . _ -" % (rel, folder))
    if name not in allowed_names(tid):
        if re.match(r"^[0-9a-fA-F]{16}_(shaders|vkpipeline)\.bin$", name):
            raise ValueError("%s: the title id in the file name does not match the folder %s" % (rel, tid))
        raise ValueError("%s: only %s, %s and meta.json are allowed"
                         % (rel, cache_file_name(tid, "shaders"), cache_file_name(tid, "vkpipeline")))
    return tid, folder


def _clean_str(v):
    return "".join(ch for ch in v if ch.isprintable()).strip()


def read_meta(path, title_id):
    size = os.path.getsize(path)
    if size > MAX_META_BYTES:
        raise ValueError("meta.json is %d bytes (limit %d)" % (size, MAX_META_BYTES))
    try:
        with open(path, "rb") as f:
            meta = json.loads(f.read().decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ValueError("meta.json is not valid UTF-8 JSON: %s" % e)
    if not isinstance(meta, dict):
        raise ValueError("meta.json must be a JSON object")
    if len(meta) > META_MAX_KEYS:
        raise ValueError("meta.json has more than %d keys" % META_MAX_KEYS)
    for k in META_REQUIRED:
        if not isinstance(meta.get(k), str):
            raise ValueError("meta.json: '%s' must be a string" % k)
    for k, v in meta.items():
        if not isinstance(v, (str, int, float, bool)) or v is None:
            raise ValueError("meta.json: '%s' must be a string or a number" % k)
        if k in META_OPTIONAL and not isinstance(v, str):
            raise ValueError("meta.json: '%s' must be a string" % k)
        if isinstance(v, str) and len(v) > META_MAX_STR:
            raise ValueError("meta.json: '%s' is longer than %d characters" % (k, META_MAX_STR))
    if meta["titleId"].lower() != title_id:
        raise ValueError("meta.json: titleId '%s' does not match the folder %s" % (meta["titleId"], title_id))
    return {k: _clean_str(meta.get(k, "")) for k in META_REQUIRED + META_OPTIONAL}


def check_cache_file(path, title_id, kind):
    """Parse and fully check one cache file. Returns the FileCache."""
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise ValueError("%s is %.1f MB (limit %d MB)" % (os.path.basename(path), size / 1048576, MAX_FILE_BYTES // 1048576))
    try:
        fc = FileCache.load(path)
        fc.check_payloads()
    except CacheError as e:
        raise ValueError("%s is not a valid Cemu cache: %s" % (os.path.basename(path), e))
    want = expected_extra_version(kind, int(title_id, 16))
    if fc.extra_version != want:
        raise ValueError("%s has extraVersion 0x%08x (%s), expected 0x%08x for %s of %s"
                         % (os.path.basename(path), fc.extra_version,
                            describe_extra_version(fc.extra_version, int(title_id, 16)), want, kind, title_id))
    if not fc.entries:
        raise ValueError("%s is empty" % os.path.basename(path))
    return fc


def check_folder(folder_path, title_id):
    """Validate a whole submission folder. Returns {"meta": dict, "shaders": FileCache|None, ...}."""
    names = allowed_names(title_id)
    result = {"meta": None, "shaders": None, "vkpipeline": None}
    present = sorted(os.listdir(folder_path))
    for name in present:
        p = os.path.join(folder_path, name)
        if name not in names or not os.path.isfile(p) or os.path.islink(p):
            raise ValueError("unexpected entry '%s'" % name)
    if "meta.json" not in present:
        raise ValueError("meta.json is missing")
    result["meta"] = read_meta(os.path.join(folder_path, "meta.json"), title_id)
    for kind in KINDS:
        name = cache_file_name(title_id, kind)
        if name in present:
            result[kind] = check_cache_file(os.path.join(folder_path, name), title_id, kind)
    if result["shaders"] is None and result["vkpipeline"] is None:
        raise ValueError("no cache file (%s or %s)" % (cache_file_name(title_id, "shaders"), cache_file_name(title_id, "vkpipeline")))
    return result


def git_changes(root, base, head):
    out = subprocess.run(["git", "-C", root, "diff", "--name-status", "--no-renames", "-z", base + "..." + head],
                         check=True, capture_output=True).stdout.decode("utf-8", "surrogateescape")
    fields = out.split("\0")
    changes = []
    i = 0
    while i + 1 < len(fields):
        changes.append((fields[i], fields[i + 1]))
        i += 2
    return changes


def validate_changes(root, changes):
    """changes: list of (status, path). Returns a list of error strings (empty = OK)."""
    errors = []
    sub = [(s, p) for s, p in changes if p.startswith("submissions/")]
    if not sub:
        return errors
    folders = set()
    for status, path in changes:
        try:
            tid, folder = check_path(path)
        except ValueError as e:
            errors.append(str(e))
            continue
        if status != "A":
            errors.append("%s: submissions may only add new files (status %s)" % (path, status))
            continue
        folders.add((tid, folder))
    if len(folders) > MAX_FOLDERS_PER_PR:
        errors.append("too many submission folders in one pull request (%d, limit %d)" % (len(folders), MAX_FOLDERS_PER_PR))
    for tid, folder in sorted(folders):
        fp = os.path.join(root, "submissions", tid, folder)
        try:
            r = check_folder(fp, tid)
            kinds = ", ".join("%s %d entries" % (k, len(r[k].entries)) for k in KINDS if r[k])
            print("OK  submissions/%s/%s: %s (%s)" % (tid, folder, r["meta"]["gameName"], kinds))
        except (OSError, ValueError) as e:
            errors.append("submissions/%s/%s: %s" % (tid, folder, e))
    return errors


def all_folders(root):
    base = os.path.join(root, "submissions")
    if not os.path.isdir(base):
        return []
    out = []
    for tid in sorted(os.listdir(base)):
        tp = os.path.join(base, tid)
        if not os.path.isdir(tp):
            continue
        for folder in sorted(os.listdir(tp)):
            out.append((tid, folder))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repository checkout to check")
    ap.add_argument("--diff", nargs=2, metavar=("BASE", "HEAD"), help="check files changed between two commits")
    ap.add_argument("--all", action="store_true", help="check every submission folder")
    ap.add_argument("paths", nargs="*", help="changed paths (relative to --root), treated as added")
    a = ap.parse_args()
    if a.diff:
        changes = git_changes(a.root, *a.diff)
    elif a.all:
        changes = []
        for tid, folder in all_folders(a.root):
            fp = os.path.join(a.root, "submissions", tid, folder)
            names = os.listdir(fp) if os.path.isdir(fp) else [""]
            changes += [("A", "submissions/%s/%s/%s" % (tid, folder, n)) for n in names]
    else:
        changes = [("A", p) for p in a.paths]
    if not any(p.startswith("submissions/") for _, p in changes):
        print("No submissions in this change; nothing to validate.")
        return 0
    errors = validate_changes(a.root, changes)
    if errors:
        print("\nSubmission check failed:", file=sys.stderr)
        for e in errors:
            print("  - " + e, file=sys.stderr)
            if os.environ.get("GITHUB_ACTIONS"):
                print("::error::" + e.replace("\n", " "))
        return 1
    print("All submissions are valid.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
