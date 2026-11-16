#!/usr/bin/env python3
"""Merge accepted submissions into the canonical caches and update index.json.

Called by .github/workflows/merge.yml after a pull request is merged into main. For every
submissions/<titleid>/<name>/ folder:

  1. download the current <titleid>_shaders.bin / <titleid>_vkpipeline.bin from the release (if any)
  2. merge: current release file first, then the submissions in folder-name order; for entries with
     the same (name1, name2) the first one wins, so existing entries are never replaced
  3. upload the merged files back to the release (creating the release if needed)
  4. update index.json and delete the processed folders (the workflow commits the result)
  5. delete the release assets those folders used from the "submissions" release; with
     --used-assets FILE the names are only written to FILE, and the workflow deletes them with
     --delete-assets FILE after the commit is pushed (so a failed push never leaves a folder in main
     that points to deleted assets)

A folder's caches are either files in the folder or assets of the "submissions" release named in
meta.json["assets"] (see validate_submission.py).

Every step is idempotent: if the run dies before the commit, the next run merges the same folders
again and gets the same caches (only the contributor count is applied at the end).

    process_submissions.py [--root REPO] [--tag caches] [--work-dir DIR]
    process_submissions.py --local-release-dir DIR   use a directory instead of the GitHub release
    process_submissions.py --local-submissions-dir DIR   use a directory instead of the submissions release
    process_submissions.py --delete-assets FILE   delete the submission assets listed in FILE
"""

import argparse
import datetime
import json
import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from filecache import CacheError, FileCache  # noqa: E402
from validate_submission import (KINDS, SUBMISSIONS_TAG, TITLE_RE, all_folders, asset_source,  # noqa: E402
                                 cache_file_name, check_folder)

INDEX_VERSION = 1
RELEASE_NOTES = ("Canonical community shader caches for Fractal. Files are updated automatically "
                 "from accepted submissions; see index.json in the repository for the list of games.")


class LocalRelease:
    """A directory standing in for the GitHub release (for tests)."""

    def __init__(self, path):
        self.path = path
        os.makedirs(path, exist_ok=True)

    def download(self, name, dest):
        src = os.path.join(self.path, name)
        if not os.path.isfile(src):
            return False
        shutil.copyfile(src, dest)
        return True

    def upload(self, files):
        for f in files:
            shutil.copyfile(f, os.path.join(self.path, os.path.basename(f)))


class GhRelease:
    """The GitHub release <tag>, through the gh CLI (GH_TOKEN must be set)."""

    def __init__(self, tag, repo=None):
        self.tag = tag
        self.repo_args = ["-R", repo] if repo else []
        self._assets = None

    def _gh(self, *args, check=True):
        return subprocess.run(["gh", *args, *self.repo_args], check=check, capture_output=True, text=True)

    def _ensure(self):
        if self._assets is not None:
            return
        r = self._gh("release", "view", self.tag, "--json", "assets", check=False)
        if r.returncode != 0:
            print("Release '%s' not found, creating it" % self.tag)
            self._gh("release", "create", self.tag, "--title", "Shader caches", "--notes", RELEASE_NOTES, "--latest=false")
            self._assets = set()
        else:
            self._assets = {a["name"] for a in json.loads(r.stdout).get("assets", [])}

    def download(self, name, dest):
        self._ensure()
        if name not in self._assets:
            return False
        d = os.path.dirname(dest)
        self._gh("release", "download", self.tag, "--pattern", name, "--dir", d, "--clobber")
        got = os.path.join(d, name)
        if got != dest:
            os.replace(got, dest)
        return True

    def upload(self, files):
        self._ensure()
        if files:
            self._gh("release", "upload", self.tag, *files, "--clobber")
            self._assets |= {os.path.basename(f) for f in files}


def load_index(path):
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as f:
            idx = json.load(f)
    else:
        idx = {}
    idx.setdefault("version", INDEX_VERSION)
    idx.setdefault("titles", {})
    return idx


def save_index(path, idx):
    idx["titles"] = dict(sorted(idx["titles"].items()))
    tmp = path + ".part"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(idx, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def process_title(root, tid, folders, release, work, assets, used_assets):
    """Merge the valid folders of one title. Returns (result dict, processed folders, errors).

    used_assets: {asset name: folder} of assets already claimed in this run (updated here).
    """
    errors = []
    accepted = []
    for folder in folders:
        fp = os.path.join(root, "submissions", tid, folder)
        try:
            r = check_folder(fp, tid, assets, os.path.join(work, "assets"))
            for a in r["assets"]:
                if a in used_assets:
                    raise ValueError("asset %s is also used by %s" % (a, used_assets[a]))
            for a in r["assets"]:
                used_assets[a] = "%s/%s" % (tid, folder)
            accepted.append((folder, r))
        except (OSError, ValueError, subprocess.CalledProcessError) as e:
            errors.append("submissions/%s/%s: %s (left in place)" % (tid, folder, e))
    if not accepted:
        return None, [], errors

    stats = {}
    uploads = []
    for kind in KINDS:
        name = cache_file_name(tid, kind)
        current = os.path.join(work, "release_" + name)
        merged = None
        before = 0
        if release.download(name, current):
            try:
                merged = FileCache.load(current)
                merged.check_payloads()
            except CacheError as e:
                raise SystemExit("release asset %s is corrupt (%s); fix it by hand before merging" % (name, e))
            before = len(merged.entries)
        for folder, r in accepted:
            fc = r[kind]
            if fc is None:
                continue
            if merged is None:
                merged = FileCache(fc.extra_version)
            elif fc.extra_version != merged.extra_version:
                # check_folder already compared both against the expected value for this title
                raise SystemExit("extraVersion mismatch for %s" % name)
            n = sum(1 for e in fc.entries if merged.add(e))
            print("  %s <- %s: +%d" % (name, folder, n))
        if merged is None:
            stats[kind] = (0, 0)
            continue
        out = os.path.join(work, name)
        size = merged.save(out, compress="none")
        # read it back before publishing it
        check = FileCache.load(out)
        check.check_payloads()
        if len(check.entries) != len(merged.entries):
            raise SystemExit("written %s does not read back correctly" % name)
        if len(merged.entries) != before or before == 0:
            uploads.append(out)
        else:
            size = os.path.getsize(current)
        print("  %s: %d -> %d entries, %d bytes" % (name, before, len(merged.entries), size))
        stats[kind] = (len(merged.entries), size)
    release.upload(uploads)
    names = [r["meta"]["gameName"] for _, r in accepted if r["meta"]["gameName"]]
    res = {"stats": stats, "gameName": names[-1] if names else "", "count": len(accepted),
           "assets": [a for _, r in accepted for a in r["assets"]]}
    return res, [f for f, _ in accepted], errors


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", default=".", help="repository checkout (default: current directory)")
    ap.add_argument("--tag", default="caches", help="release tag holding the canonical caches")
    ap.add_argument("--repo", help="owner/name for gh (default: the repository of --root)")
    ap.add_argument("--local-release-dir", help="use this directory instead of the GitHub release")
    ap.add_argument("--work-dir", help="scratch directory (default: <root>/.work)")
    ap.add_argument("--keep-submissions", action="store_true", help="do not delete processed folders or assets")
    ap.add_argument("--submissions-tag", default=SUBMISSIONS_TAG, help="release holding submitted assets")
    ap.add_argument("--local-submissions-dir", help="use this directory instead of the submissions release")
    ap.add_argument("--used-assets", metavar="FILE",
                    help="write the names of the merged submission assets to FILE instead of deleting them")
    ap.add_argument("--delete-assets", metavar="FILE", help="only delete the submission assets listed in FILE")
    a = ap.parse_args()

    assets = asset_source(a.local_submissions_dir, a.submissions_tag, a.repo)
    if a.delete_assets:
        with open(a.delete_assets, encoding="utf-8") as f:
            names = [n.strip() for n in f if n.strip()]
        for n in names:
            assets.delete(n)
        print("Deleted %d submission asset(s)." % len(names))
        return 0

    root = os.path.abspath(a.root)
    work = os.path.abspath(a.work_dir or os.path.join(root, ".work"))
    os.makedirs(work, exist_ok=True)
    release = LocalRelease(a.local_release_dir) if a.local_release_dir else GhRelease(a.tag, a.repo)
    index_path = os.path.join(root, "index.json")
    idx = load_index(index_path)

    by_title = {}
    errors = []
    for tid, folder in all_folders(root):
        if not TITLE_RE.match(tid):
            errors.append("submissions/%s: not a title id folder (left in place)" % tid)
            continue
        by_title.setdefault(tid, []).append(folder)
    if not by_title:
        print("No submissions to process.")

    now = datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    processed = []
    used_assets = {}
    merged_assets = []
    for tid in sorted(by_title):
        print("%s: %d submission(s)" % (tid, len(by_title[tid])))
        res, done, errs = process_title(root, tid, by_title[tid], release, work, assets, used_assets)
        errors += errs
        if res is None:
            continue
        entry = idx["titles"].get(tid, {})
        s, p = res["stats"]["shaders"], res["stats"]["vkpipeline"]
        prev_s = (entry.get("shaders", 0), entry.get("shadersSize", 0))
        prev_p = (entry.get("pipelines", 0), entry.get("pipelinesSize", 0))
        idx["titles"][tid] = {
            "name": entry.get("name") if entry.get("name") not in (None, "", tid) else (res["gameName"] or tid),
            "shaders": s[0] if s[0] else prev_s[0],
            "pipelines": p[0] if p[0] else prev_p[0],
            "shadersSize": s[1] if s[0] else prev_s[1],
            "pipelinesSize": p[1] if p[0] else prev_p[1],
            "updated": now,
            "contributors": entry.get("contributors", 0) + res["count"],
        }
        processed += [(tid, f) for f in done]
        merged_assets += res["assets"]

    save_index(index_path, idx)
    if not a.keep_submissions:
        for tid, folder in processed:
            shutil.rmtree(os.path.join(root, "submissions", tid, folder))
            tdir = os.path.join(root, "submissions", tid)
            if not os.listdir(tdir):
                os.rmdir(tdir)
        if a.used_assets:
            with open(a.used_assets, "w", encoding="utf-8") as f:
                f.write("".join(n + "\n" for n in merged_assets))
        else:
            for n in merged_assets:
                assets.delete(n)
    shutil.rmtree(work, ignore_errors=True)
    print("Processed %d submission(s), %d submission asset(s) %s." % (
        len(processed), len(merged_assets),
        "kept" if a.keep_submissions else ("to delete" if a.used_assets else "deleted")))
    if errors:
        print("\nSome submissions were not processed:", file=sys.stderr)
        for e in errors:
            print("  - " + e, file=sys.stderr)
            if os.environ.get("GITHUB_ACTIONS"):
                print("::error::" + e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
