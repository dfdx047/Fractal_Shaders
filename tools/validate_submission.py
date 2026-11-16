#!/usr/bin/env python3
"""Validate shader cache submissions.

A submission is one folder, submissions/<titleid>/<name>/, in one of two forms:

  1. Release assets (what the Fractal app sends): the folder holds only meta.json, and
     meta.json["assets"] names the cache files, uploaded as assets of the "submissions" release:
         {"assets": {"shaders": "<titleid>_shaders_<id>.bin", "vkpipeline": "<titleid>_vkpipeline_<id>.bin"}}
     (one or both keys; <id> is [A-Za-z0-9-]{1,40})

  2. Files in the folder (manual pull requests):
         <titleid>_shaders.bin, <titleid>_vkpipeline.bin (one or both) and meta.json

A folder may use both forms only for different kinds. <titleid> is 16 lowercase hex digits (the base
game, 00050000xxxxxxxx). <name> is [A-Za-z0-9._-], up to 64 characters.

meta.json: {"titleId": str (= folder), "gameName": str (may be empty), optional strings "appVersion",
"device", "gpu", "driver", optional "assets" object as above; other keys may be strings, numbers or
booleans}. At most 16 KB and 32 keys.

Usage:
    validate_submission.py --root REPO --diff BASE HEAD   check the files a PR adds (CI)
    validate_submission.py --root REPO --all              check every folder under submissions/
    validate_submission.py --root REPO PATH...            check the given changed paths
    --local-submissions-dir DIR   use a directory instead of the "submissions" release (tests)
"""

import argparse
import json
import os
import re
import shutil
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
ASSET_RE = re.compile(r"^([0-9a-f]{16})_(shaders|vkpipeline)_([A-Za-z0-9-]{1,40})\.bin$")
META_REQUIRED = ("titleId", "gameName")
META_OPTIONAL = ("appVersion", "device", "gpu", "driver")
META_MAX_KEYS = 32
META_MAX_STR = 200
SUBMISSIONS_TAG = "submissions"


# ------------------------------------------------------------------ where submitted assets live
class LocalAssets:
    """A directory standing in for the "submissions" release (tests)."""

    def __init__(self, path):
        self.path = path
        self.label = "directory %s" % path

    def size(self, name):
        p = os.path.join(self.path, name)
        return os.path.getsize(p) if os.path.isfile(p) else None

    def fetch(self, name, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, name)
        shutil.copyfile(os.path.join(self.path, name), dest)
        return dest

    def delete(self, name):
        p = os.path.join(self.path, name)
        if os.path.isfile(p):
            os.remove(p)


class GhAssets:
    """Assets of the GitHub release <tag> (default "submissions"), through the gh CLI."""

    def __init__(self, tag=SUBMISSIONS_TAG, repo=None):
        self.tag = tag
        self.repo_args = ["-R", repo] if repo else []
        self.label = "release '%s'" % tag
        self._sizes = None

    def _gh(self, *args, check=True):
        return subprocess.run(["gh", *args, *self.repo_args], check=check, capture_output=True, text=True)

    def _api(self, *args, check=True):
        env = dict(os.environ)
        if self.repo_args:
            env["GH_REPO"] = self.repo_args[1]
        return subprocess.run(["gh", "api", *args], check=check, capture_output=True, text=True, env=env)

    def size(self, name):
        if self._sizes is None:
            # the asset list is paginated (a release can hold up to 1000 assets)
            r = self._api("repos/{owner}/{repo}/releases/tags/" + self.tag, "--jq", ".id", check=False)
            if r.returncode != 0:
                if "404" not in r.stderr and "Not Found" not in r.stderr:
                    raise ValueError("cannot read release '%s': %s" % (self.tag, r.stderr.strip()))
                self._sizes = {}  # release does not exist (yet)
            else:
                r = self._api("--paginate", "repos/{owner}/{repo}/releases/%s/assets?per_page=100" % r.stdout.strip(),
                              "--jq", ".[] | [.name, (.size | tostring)] | join(\"\\t\")")
                self._sizes = {}
                for line in r.stdout.splitlines():
                    n, _, sz = line.partition("\t")
                    self._sizes[n] = int(sz)
        return self._sizes.get(name)

    def fetch(self, name, dest_dir):
        os.makedirs(dest_dir, exist_ok=True)
        self._gh("release", "download", self.tag, "--pattern", name, "--dir", dest_dir, "--clobber")
        return os.path.join(dest_dir, name)

    def delete(self, name):
        r = self._gh("release", "delete-asset", self.tag, name, "-y", check=False)
        if r.returncode != 0:
            print("warning: could not delete asset %s: %s" % (name, r.stderr.strip()), file=sys.stderr)


# ------------------------------------------------------------------ checks
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


def _check_assets_field(assets, title_id):
    if not isinstance(assets, dict) or not assets:
        raise ValueError("meta.json: 'assets' must be an object with 'shaders' and/or 'vkpipeline'")
    for kind, name in assets.items():
        if kind not in KINDS:
            raise ValueError("meta.json: unknown asset kind '%s' (use shaders or vkpipeline)" % kind)
        if not isinstance(name, str):
            raise ValueError("meta.json: assets.%s must be a string" % kind)
        m = ASSET_RE.match(name)
        if not m:
            raise ValueError("meta.json: asset name '%s' must be <titleid>_%s_<id>.bin with id [A-Za-z0-9-]{1,40}"
                             % (name, kind))
        if m.group(1) != title_id:
            raise ValueError("meta.json: asset '%s' is not for the folder's title id %s" % (name, title_id))
        if m.group(2) != kind:
            raise ValueError("meta.json: asset '%s' is listed as %s" % (name, kind))
    return dict(assets)


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
        if k == "assets":
            continue
        if not isinstance(v, (str, int, float, bool)):
            raise ValueError("meta.json: '%s' must be a string or a number" % k)
        if k in META_OPTIONAL and not isinstance(v, str):
            raise ValueError("meta.json: '%s' must be a string" % k)
        if isinstance(v, str) and len(v) > META_MAX_STR:
            raise ValueError("meta.json: '%s' is longer than %d characters" % (k, META_MAX_STR))
    if meta["titleId"].lower() != title_id:
        raise ValueError("meta.json: titleId '%s' does not match the folder %s" % (meta["titleId"], title_id))
    out = {k: _clean_str(meta.get(k, "")) for k in META_REQUIRED + META_OPTIONAL}
    out["assets"] = _check_assets_field(meta["assets"], title_id) if "assets" in meta else {}
    return out


def check_cache_file(path, title_id, kind, label=None):
    """Parse and fully check one cache file. Returns the FileCache."""
    label = label or os.path.basename(path)
    size = os.path.getsize(path)
    if size > MAX_FILE_BYTES:
        raise ValueError("%s is %.1f MB (limit %d MB)" % (label, size / 1048576, MAX_FILE_BYTES // 1048576))
    try:
        fc = FileCache.load(path)
        fc.check_payloads()
    except CacheError as e:
        raise ValueError("%s is not a valid Cemu cache: %s" % (label, e))
    want = expected_extra_version(kind, int(title_id, 16))
    if fc.extra_version != want:
        raise ValueError("%s has extraVersion 0x%08x (%s), expected 0x%08x for %s of %s"
                         % (label, fc.extra_version, describe_extra_version(fc.extra_version, int(title_id, 16)),
                            want, kind, title_id))
    if not fc.entries:
        raise ValueError("%s is empty" % label)
    return fc


def check_folder(folder_path, title_id, assets=None, work_dir=None):
    """Validate a whole submission folder.

    Returns {"meta": dict, "shaders": FileCache|None, "vkpipeline": FileCache|None, "assets": [names]}.
    assets: LocalAssets/GhAssets resolving meta.json["assets"]; work_dir: where to download them.
    """
    names = allowed_names(title_id)
    result = {"meta": None, "shaders": None, "vkpipeline": None, "assets": []}
    present = sorted(os.listdir(folder_path))
    for name in present:
        p = os.path.join(folder_path, name)
        if name not in names or not os.path.isfile(p) or os.path.islink(p):
            raise ValueError("unexpected entry '%s'" % name)
    if "meta.json" not in present:
        raise ValueError("meta.json is missing")
    meta = read_meta(os.path.join(folder_path, "meta.json"), title_id)
    result["meta"] = meta
    for kind in KINDS:
        name = cache_file_name(title_id, kind)
        asset = meta["assets"].get(kind)
        if name in present and asset:
            raise ValueError("%s is both in the folder and in meta.json assets" % kind)
        if name in present:
            result[kind] = check_cache_file(os.path.join(folder_path, name), title_id, kind)
        elif asset:
            if assets is None:
                raise ValueError("meta.json lists release assets but no asset source was given")
            size = assets.size(asset)
            if size is None:
                raise ValueError("asset %s not found in %s" % (asset, assets.label))
            if size > MAX_FILE_BYTES:
                raise ValueError("asset %s is %.1f MB (limit %d MB)" % (asset, size / 1048576, MAX_FILE_BYTES // 1048576))
            path = assets.fetch(asset, work_dir or os.path.join(folder_path, ".assets"))
            try:
                result[kind] = check_cache_file(path, title_id, kind, "asset " + asset)
            finally:
                os.remove(path)
            result["assets"].append(asset)
    if result["shaders"] is None and result["vkpipeline"] is None:
        raise ValueError("no cache file (%s / %s in the folder or meta.json assets)"
                         % (cache_file_name(title_id, "shaders"), cache_file_name(title_id, "vkpipeline")))
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


def validate_changes(root, changes, assets=None, work_dir=None):
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
    used = {}
    for tid, folder in sorted(folders):
        fp = os.path.join(root, "submissions", tid, folder)
        try:
            r = check_folder(fp, tid, assets, work_dir)
            for a in r["assets"]:
                if a in used:
                    raise ValueError("asset %s is also used by %s" % (a, used[a]))
                used[a] = folder
            kinds = ", ".join("%s %d entries" % (k, len(r[k].entries)) for k in KINDS if r[k])
            src = " via assets" if r["assets"] else ""
            print("OK  submissions/%s/%s: %s (%s%s)" % (tid, folder, r["meta"]["gameName"], kinds, src))
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
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


def asset_source(local_dir=None, tag=SUBMISSIONS_TAG, repo=None):
    return LocalAssets(local_dir) if local_dir else GhAssets(tag, repo)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repository checkout to check")
    ap.add_argument("--diff", nargs=2, metavar=("BASE", "HEAD"), help="check files changed between two commits")
    ap.add_argument("--all", action="store_true", help="check every submission folder")
    ap.add_argument("--submissions-tag", default=SUBMISSIONS_TAG, help="release holding submitted assets")
    ap.add_argument("--repo", help="owner/name for gh (default: GH_REPO or the checkout's remote)")
    ap.add_argument("--local-submissions-dir", help="use this directory instead of the submissions release")
    ap.add_argument("--work-dir", help="where to download assets (default: <root>/.work/assets)")
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
    work = os.path.abspath(a.work_dir or os.path.join(a.root, ".work", "assets"))
    errors = validate_changes(a.root, changes, asset_source(a.local_submissions_dir, a.submissions_tag, a.repo), work)
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
