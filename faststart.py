#!/usr/bin/env python3
"""
m4b-faststart — recursively move the moov atom to the front of .m4b
audiobook files so playback can start before the whole file downloads.

Skips files that are already faststart-optimized. Verifies chapters,
cover art, and duration are preserved before replacing the original.

Designed to run as a one-shot job (cron / NAS scheduled task / docker run),
not a daemon. No database, no web UI.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("m4b-faststart")

LEDGER_VERSION = 1
FINGERPRINT_SAMPLE_BYTES = 1024 * 1024  # 1MB from head + 1MB from tail


# --------------------------------------------------------------------------
# ffprobe helpers
# --------------------------------------------------------------------------

def ffprobe_json(path: Path) -> dict:
    """Run ffprobe and return parsed JSON (format + streams + chapters)."""
    cmd = [
        "ffprobe",
        "-v", "error",
        "-print_format", "json",
        "-show_format",
        "-show_streams",
        "-show_chapters",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {path}: {result.stderr.strip()}")
    return json.loads(result.stdout)


def is_faststart(path: Path) -> bool:
    """
    Detect whether the moov atom precedes mdat by scanning top-level box
    order directly — this is the actual definition of "faststart", and is
    cheap (reads only box headers, not full file content).
    """
    try:
        with open(path, "rb") as f:
            pos = 0
            file_size = path.stat().st_size
            while pos < file_size:
                f.seek(pos)
                header = f.read(8)
                if len(header) < 8:
                    break
                size = int.from_bytes(header[0:4], "big")
                box_type = header[4:8].decode("ascii", errors="replace")
                if box_type == "moov":
                    return True
                if box_type == "mdat":
                    return False
                if size == 1:
                    # 64-bit extended size
                    ext = f.read(8)
                    if len(ext) < 8:
                        break
                    size = int.from_bytes(ext, "big")
                elif size == 0:
                    # box extends to EOF — nothing meaningful follows
                    break
                if size < 8:
                    break
                pos += size
    except OSError as e:
        raise RuntimeError(f"could not read {path}: {e}")
    # Neither found (unexpected/corrupt) — treat as not-faststart so it
    # gets processed and re-verified rather than silently skipped.
    return False


def quick_fingerprint(path: Path, size: int) -> str:
    """
    Cheap content fingerprint: sha256 of file size + first/last 1MB. Reads
    at most ~2MB regardless of file size, so it stays fast on a library of
    large audiobooks while still catching an edited/replaced file at the
    same path (a real content change almost always touches the head, where
    metadata/cover art live, or the tail, where moov often lives pre-fix).
    """
    h = hashlib.sha256()
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(FINGERPRINT_SAMPLE_BYTES))
        if size > FINGERPRINT_SAMPLE_BYTES:
            f.seek(max(size - FINGERPRINT_SAMPLE_BYTES, 0))
            h.update(f.read(FINGERPRINT_SAMPLE_BYTES))
    return h.hexdigest()


# --------------------------------------------------------------------------
# processed-file ledger
# --------------------------------------------------------------------------

class Ledger:
    """
    Persistent record of files already confirmed faststart, keyed by
    absolute path. Lets repeat runs skip the box-order check entirely
    (just a stat()) for files that haven't changed since last time, and
    gives an audit trail of what was processed/skipped/failed and when.

    Stored as a single JSON file, meant to live outside the audiobook
    library (e.g. a small mounted appdata volume) so the library directory
    stays free of tool bookkeeping.
    """

    def __init__(self, log_path: Path | None):
        self.log_path = log_path
        self.data = {"version": LEDGER_VERSION, "files": {}, "runs": []}
        self._dirty = False
        if self.log_path and self.log_path.exists():
            try:
                loaded = json.loads(self.log_path.read_text(encoding="utf-8"))
                if loaded.get("version") == LEDGER_VERSION:
                    self.data = loaded
                else:
                    log.warning(
                        "Ledger at %s has unknown version %r — starting fresh",
                        self.log_path, loaded.get("version"),
                    )
            except (OSError, json.JSONDecodeError) as e:
                log.warning("Could not read ledger %s (%s) — starting fresh", self.log_path, e)

    def is_known_good(self, path: Path, size: int, mtime: float) -> bool:
        """True if the ledger already confirmed this exact file (by path +
        size + mtime) is faststart, so the box-order check can be skipped."""
        entry = self.data["files"].get(str(path))
        if not entry or entry.get("result") not in ("processed", "already-faststart"):
            return False
        return entry.get("size") == size and entry.get("mtime") == mtime

    def record(self, path: Path, result: str, size: int | None = None,
               mtime: float | None = None, fingerprint: str | None = None,
               detail: str | None = None) -> None:
        entry = {
            "result": result,
            "size": size,
            "mtime": mtime,
            "fingerprint": fingerprint,
            "last_checked": time.time(),
        }
        if detail:
            entry["detail"] = detail
        self.data["files"][str(path)] = entry
        self._dirty = True

    def record_run(self, root: str, counts: dict, dry_run: bool) -> None:
        self.data["runs"].append({
            "timestamp": time.time(),
            "root": root,
            "dry_run": dry_run,
            **counts,
        })
        # Keep run history bounded — this is a log, not an unbounded archive.
        self.data["runs"] = self.data["runs"][-200:]
        self._dirty = True

    def save(self) -> None:
        if not self.log_path or not self._dirty:
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.log_path.with_suffix(self.log_path.suffix + ".tmp")
            tmp.write_text(json.dumps(self.data, indent=2), encoding="utf-8")
            os.replace(tmp, self.log_path)
        except OSError as e:
            log.warning("Could not write ledger %s: %s", self.log_path, e)


# --------------------------------------------------------------------------
# core processing
# --------------------------------------------------------------------------

class VerificationError(Exception):
    pass


def run_faststart_remux(src: Path, dst: Path) -> None:
    # Map only audio and video (cover art) streams explicitly, and skip any
    # other streams (e.g. an existing mov-text chapter track). Chapters are
    # carried across separately via -map_metadata 0, which makes the muxer
    # regenerate a single chapter track from the chapter list. Mapping "0"
    # wholesale would copy the existing chapter-text stream *and* have the
    # muxer add a new one from chapter metadata, silently doubling it.
    cmd = [
        "ffmpeg",
        "-y",
        "-v", "error",
        "-i", str(src),
        "-map", "0:a",
        "-map", "0:v?",
        "-map_metadata", "0",
        "-c", "copy",
        "-movflags", "+faststart",
        "-f", "mp4",
        str(dst),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg remux failed: {result.stderr.strip()}")


def verify(src_probe: dict, dst_path: Path, duration_tolerance: float = 1.0) -> None:
    """Compare chapters, streams (incl. cover art), and duration."""
    dst_probe = ffprobe_json(dst_path)

    # Duration
    try:
        src_dur = float(src_probe["format"]["duration"])
        dst_dur = float(dst_probe["format"]["duration"])
    except (KeyError, ValueError) as e:
        raise VerificationError(f"missing duration in probe data: {e}")
    if abs(src_dur - dst_dur) > duration_tolerance:
        raise VerificationError(
            f"duration mismatch: src={src_dur:.2f}s dst={dst_dur:.2f}s"
        )

    # Chapters
    src_chapters = src_probe.get("chapters", [])
    dst_chapters = dst_probe.get("chapters", [])
    if len(src_chapters) != len(dst_chapters):
        raise VerificationError(
            f"chapter count mismatch: src={len(src_chapters)} dst={len(dst_chapters)}"
        )

    # Streams — count and codec parity, grouped by codec_type rather than by
    # raw index. run_faststart_remux() only ever maps "0:a" and "0:v?", so
    # only audio and video (cover art) streams are ours to verify. The mp4
    # muxer rebuilds a chapter track from -map_metadata 0, which also probes
    # as a "data"-typed stream and can land at a different index than the
    # source's own chapter stream did -- e.g. ahead of the cover art instead
    # of behind it. That shifts positional indices without changing anything
    # we actually asked to preserve, so a plain zip-by-index falsely flags
    # those files as broken. Compare each relevant codec_type's streams as a
    # multiset of codec_names instead; players key off codec_type and
    # disposition flags (default, attached_pic), not raw stream order, so
    # order among same-type streams was never something worth enforcing.
    src_streams = src_probe.get("streams", [])
    dst_streams = dst_probe.get("streams", [])
    for codec_type in ("audio", "video"):
        src_group = sorted(
            s.get("codec_name") for s in src_streams if s.get("codec_type") == codec_type
        )
        dst_group = sorted(
            d.get("codec_name") for d in dst_streams if d.get("codec_type") == codec_type
        )
        if src_group != dst_group:
            raise VerificationError(
                f"{codec_type} stream mismatch: src={src_group} dst={dst_group}"
            )

    # Not faststart-verified? ffmpeg with +faststart on a copy remux should
    # always produce moov-first output; double check anyway.
    if not is_faststart(dst_path):
        raise VerificationError("output is not faststart after remux")


def overwrite_in_place(target: Path, source: Path, chunk_size: int = 4 * 1024 * 1024) -> None:
    """
    Overwrite target's contents with source's, WITHOUT changing target's
    inode — critical for tools like Audiobookshelf that track library
    files by inode, not just path. A rename-based swap (os.replace) always
    creates a new inode at the same path and gets seen as a different file,
    which is exactly what this avoids.

    Safe write ordering: write all new bytes and fsync BEFORE truncating.
    A crash after the write but before the truncate leaves harmless old
    trailing bytes past the new (shorter) EOF — truncate() on most
    filesystems is a single atomic metadata update, so it either applies
    fully or not at all. The reverse order (truncate-then-write) is never
    used here because a crash mid-write would leave target corrupted with
    no way to recover its original content.

    Trade-off: unlike the old rename-swap, this is not atomic to a
    concurrent reader — a reader that has the file open across the
    overwrite could see a torn mix of old and new bytes. See the README
    for why this is accepted rather than worked around.
    """
    new_size = source.stat().st_size
    with open(target, "r+b") as dst, open(source, "rb") as src:
        while True:
            chunk = src.read(chunk_size)
            if not chunk:
                break
            dst.write(chunk)
        dst.flush()
        os.fsync(dst.fileno())
        dst.truncate(new_size)
        dst.flush()
        os.fsync(dst.fileno())


def process_file(path: Path, ledger: Ledger, work_dir: Path, dry_run: bool = False,
                  keep_temp_on_fail: bool = False) -> str:
    """
    Returns one of: "skipped", "processed", "failed". Never raises —
    any error (including from stat/probe/box-scan, not just the
    remux/verify/swap) is caught and recorded to the ledger as a failure.
    """
    try:
        return _process_file_inner(path, ledger, work_dir, dry_run, keep_temp_on_fail)
    except Exception as e:
        log.error("FAILED: %s (%s)", path, e)
        try:
            st = path.stat()
            ledger.record(path, "failed", st.st_size, st.st_mtime, detail=str(e))
        except OSError:
            ledger.record(path, "failed", detail=str(e))
        return "failed"


def _process_file_inner(path: Path, ledger: Ledger, work_dir: Path, dry_run: bool,
                         keep_temp_on_fail: bool) -> str:
    st = path.stat()
    size, mtime = st.st_size, st.st_mtime

    if ledger.is_known_good(path, size, mtime):
        log.info("SKIP  (ledger: known faststart, unchanged): %s", path)
        return "skipped"

    if is_faststart(path):
        log.info("SKIP  (already faststart): %s", path)
        if not dry_run:
            ledger.record(path, "already-faststart", size, mtime)
        return "skipped"

    if dry_run:
        log.info("WOULD PROCESS: %s", path)
        return "processed"

    log.info("PROCESSING: %s", path)
    src_probe = ffprobe_json(path)

    # Both the working remux and the recovery backup live entirely OUTSIDE
    # the audiobooks folder (in work_dir, typically the same mounted volume
    # as the ledger) — never in the library tree, even transiently. A
    # scanner that walks the library mid-run (Audiobookshelf, Plex, etc.)
    # never sees anything but the real book files. A short hash of the
    # full path keeps names collision-safe and short even though work_dir
    # is shared across every book, unlike the old per-folder temp file.
    safe_stem = re.sub(r"[^A-Za-z0-9._-]", "_", path.stem)[:60]
    path_hash = hashlib.sha256(str(path).encode()).hexdigest()[:12]
    work_dir.mkdir(parents=True, exist_ok=True)

    tmp_fd, tmp_name = tempfile.mkstemp(
        suffix=".m4b", prefix=f"{safe_stem}.{path_hash}.tmp.", dir=str(work_dir)
    )
    os.close(tmp_fd)
    tmp_path = Path(tmp_name)

    backup_path = work_dir / f"{safe_stem}.{path_hash}.bak.m4b"
    backup_made = False

    try:
        run_faststart_remux(path, tmp_path)
        verify(src_probe, tmp_path)

        # A plain recovery copy, made before touching the original at all.
        # Not part of the write path below — purely a manual fallback if
        # something goes wrong mid-overwrite.
        shutil.copy2(path, backup_path)
        backup_made = True

        overwrite_in_place(path, tmp_path)

        tmp_path.unlink(missing_ok=True)
        backup_path.unlink(missing_ok=True)
        new_st = path.stat()
        ledger.record(
            path, "processed", new_st.st_size, new_st.st_mtime,
            fingerprint=quick_fingerprint(path, new_st.st_size),
        )
        log.info("DONE: %s", path)
        return "processed"

    except Exception:
        if tmp_path.exists():
            if keep_temp_on_fail:
                log.error("  kept temp file for inspection: %s", tmp_path)
            else:
                tmp_path.unlink(missing_ok=True)
        if backup_made:
            log.error("  original preserved at: %s", backup_path)
        raise  # logged + recorded to the ledger by process_file()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def is_our_temp_file(path: Path) -> bool:
    """True for our own scratch files (see mkstemp prefix/suffix in
    _process_file_inner). Working files no longer land in the library
    folder at all (they live in work_dir), but this filter stays as a
    cheap defense-in-depth in case one was left behind by an older
    version of the script, or dropped into the library folder some
    other way."""
    return ".tmp." in path.name


def find_m4b_files(root: Path):
    yield from sorted(p for p in root.rglob("*.m4b") if not is_our_temp_file(p))
    yield from sorted(p for p in root.rglob("*.M4B") if not is_our_temp_file(p))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "directory", nargs="?", default="/audiobooks",
        help="Root directory to scan recursively (default: /audiobooks)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Report what would be done without changing any files",
    )
    parser.add_argument(
        "--keep-temp-on-fail", action="store_true",
        help="Keep the temp remux output when verification fails, for debugging",
    )
    parser.add_argument(
        "--log", default="/data/faststart-log.json",
        help="Path to the processed-file ledger (JSON). Set to empty string "
             "to disable. Default: /data/faststart-log.json",
    )
    parser.add_argument(
        "--work-dir", default="/data/work",
        help="Scratch directory for in-progress remuxes and recovery backups. "
             "Kept OUTSIDE the audiobooks folder on purpose — a library "
             "scanner (Audiobookshelf, Plex, etc.) walking the tree mid-run "
             "should never see a working file. Default: /data/work",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Debug logging",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = Path(args.directory)
    if not root.is_dir():
        log.error("Not a directory: %s", root)
        return 2

    for tool in ("ffmpeg", "ffprobe"):
        if shutil.which(tool) is None:
            log.error("%s not found on PATH", tool)
            return 2

    ledger = Ledger(Path(args.log) if args.log else None)
    if ledger.log_path:
        log.info("Using ledger: %s", ledger.log_path)

    work_dir = Path(args.work_dir)

    # Clean up any of our own scratch files left behind by a run that was
    # killed mid-job (container OOM'd, host rebooted, etc). These live in
    # work_dir, never in the library folder, so this is just tidying our
    # own scratch space — not a scan of the library.
    if not args.dry_run and work_dir.is_dir():
        for stray in work_dir.glob("*.tmp.*.m4b"):
            log.warning("Removing orphaned temp file from a prior run: %s", stray)
            stray.unlink(missing_ok=True)

    # Also sweep the library folder itself, in case a leftover from an
    # older version of this script (which used to work in-place there) is
    # still sitting around.
    if not args.dry_run:
        for pattern in ("*.tmp.*.m4b", "*.tmp.*.M4B"):
            for stray in root.rglob(pattern):
                log.warning("Removing orphaned temp file from a prior run: %s", stray)
                stray.unlink(missing_ok=True)

    counts = {"skipped": 0, "processed": 0, "failed": 0}
    files = list(find_m4b_files(root))
    log.info("Found %d .m4b file(s) under %s", len(files), root)

    for path in files:
        try:
            result = process_file(
                path, ledger, work_dir, dry_run=args.dry_run,
                keep_temp_on_fail=args.keep_temp_on_fail,
            )
        except Exception as e:
            log.error("UNEXPECTED ERROR on %s: %s", path, e)
            result = "failed"
        counts[result] += 1

    log.info(
        "Summary: %d processed, %d skipped, %d failed (of %d total)",
        counts["processed"], counts["skipped"], counts["failed"], len(files),
    )

    if not args.dry_run:
        ledger.record_run(str(root), counts, args.dry_run)
        ledger.save()

    return 1 if counts["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
