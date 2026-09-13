# m4b-faststart

A tiny, one-shot Docker job that recursively scans a directory of `.m4b`
audiobooks and moves the `moov` atom to the front of each file
(`-movflags +faststart`) so players can start playback before the whole
file downloads/loads.

No database, no web UI, no daemon. It runs, processes what needs
processing, and exits — meant to be triggered by your NAS's own
scheduler (cron, Synology Task Scheduler, etc).

Pre-built images (`linux/amd64` + `linux/arm64`) are published to GHCR
on every push to `main` and on tagged releases — see
[Pull the image](#pull-the-image) below if you'd rather not build it
yourself.

## How it works

For each `*.m4b` file under the mounted directory:

1. **Detect** — read the file's top-level box (atom) order directly. If
   `moov` appears before `mdat`, the file is already faststart and is
   skipped.
2. **Remux** — otherwise, run:
   ```
   ffmpeg -i input.m4b -map 0 -map_metadata 0 -c copy -movflags +faststart output.tmp
   ```
   This is a stream copy — no re-encoding, so it's fast and lossless.
3. **Verify** — probe the output with `ffprobe` and compare against the
   original:
   - duration (within 1s tolerance)
   - chapter count
   - stream count, codec type, and codec name per stream (this also
     covers embedded cover art, which shows up as an `attached_pic`
     video stream)
   - confirm the output is actually faststart now
4. **Replace** — only if verification passes: the original file is
   overwritten **in place** (same inode, contents replaced) rather than
   deleted-and-replaced. This matters for library managers like
   Audiobookshelf that track files by inode — a delete-and-recreate
   (even at the identical path) can get seen as a *new* file, leaving
   you with a duplicate library item pointing at the same book. See
   [In-place updates](#in-place-updates-and-why) below for the full
   reasoning and the one trade-off this involves.

Errors and skips are logged; the run ends with a summary line and a
non-zero exit code if anything failed, so your scheduler can alert on
it.

## In-place updates, and why

Early versions of this tool wrote the remuxed output to a temp file and
swapped it in with a rename (`os.replace`). That's the normal safe
pattern for atomic file updates — but it has one consequence that
matters specifically for audiobook library managers: a rename creates a
**new inode** at that path. Audiobookshelf's scanner tracks library
files by inode, not just path, so a renamed-in replacement — even with
byte-identical content at the identical path — can be seen as a
different file, and the book shows up with a duplicate `Library Files`
entry pointing at the same folder.

To avoid that, the file's contents are overwritten **in place**: the
original file is opened and its bytes are replaced, then truncated to
the new (usually slightly smaller) length — the inode never changes.
Sequence, in order:

1. Remux to a scratch file in `work_dir` (default `/data/work`, outside
   the library folder entirely — see below).
2. Verify the scratch file fully (chapters, streams, duration,
   faststart).
3. Copy the *original* to a recovery backup, also in `work_dir`.
4. Write the new bytes into the original file, `fsync`, **then**
   truncate to the final length — write-then-truncate, never the
   reverse, so a crash mid-write leaves harmless trailing old bytes past
   the new EOF rather than a corrupted file.
5. Delete the scratch file and the recovery backup.

**Trade-off:** unlike a rename-swap, this is not atomic to a concurrent
reader. If something has the file open and is actively reading through
the exact moment it's being overwritten, it could see a torn mix of old
and new bytes rather than cleanly one version or the other. In practice
this only matters if a book is being actively streamed at the exact
moment the job processes that exact file — for a home/NAS setup this is
a narrow window, and running the job during low-usage hours (e.g. the
weekly cron examples below) avoids it in practice. If a run fails
partway, the recovery backup in `work_dir` (named
`<title>.<hash>.bak.m4b`) has the untouched original.

Nothing related to processing — the scratch remux, the recovery backup,
or anything else — is ever written into the audiobooks folder itself,
even transiently. A library scanner that happens to walk the tree
mid-run sees only real book files, never a stray temp file.

## Processed-file ledger

Every run reads/writes a small JSON ledger (default
`/data/faststart-log.json`) recording, per file: result
(`processed` / `already-faststart` / `failed`), size, mtime, a cheap
content fingerprint, and when it was last checked. Each run also appends
a summary row (timestamp, counts) so you have history across runs.

This does two things:

- **Speeds up repeat runs.** If a file's path + size + mtime match a
  ledger entry already confirmed faststart, the run skips it with just a
  `stat()` call — no file open, no box-order scan. On a library that's
  already fully optimized, a scheduled run over thousands of books
  finishes almost instantly.
- **Gives you an audit trail.** What was touched, when, and whether it
  succeeded — useful for confirming a scheduled job is actually doing
  something (or nothing, because there's nothing left to do) without
  digging through container logs.

The ledger is deliberately **not** the sole source of truth for
"already faststart" — a file whose size/mtime changed since it was
logged (edited, replaced, restored from backup) is always re-verified
with the real box-order check, never trusted blindly.

Keep the ledger's directory **outside** the audiobooks mount (e.g. a
small NAS appdata folder) so the library directory stays free of tool
bookkeeping:

```sh
docker run --rm \
  -v /path/to/audiobooks:/audiobooks \
  -v /path/to/appdata/m4b-faststart:/data \
  m4b-faststart
```

Pass `--log ""` to disable the ledger entirely (every run then falls
back to a full box-order scan of every file, which is still cheap —
just a header read per file, not a full read).

## Usage

### Pull the image

```sh
docker pull ghcr.io/hypnotoad08/m4b-faststart:latest
```

Also available pinned to a version (`:1.2.0`), a major.minor track
(`:1.2`), or a specific commit (`:<short-sha>`) — see the
[Packages page](https://github.com/hypnotoad08/m4b-faststart/pkgs/container/m4b-faststart)
for what's published. `:latest` always tracks `main`.

### ...or build it yourself

```sh
docker build -t m4b-faststart .
```

(Substitute `m4b-faststart` for `ghcr.io/hypnotoad08/m4b-faststart:latest`
in the examples below if you built locally instead of pulling.)

### Run once

```sh
docker run --rm \
  -v /path/to/audiobooks:/audiobooks \
  -v /path/to/appdata/m4b-faststart:/data \
  ghcr.io/hypnotoad08/m4b-faststart:latest
```

### Dry run (no files changed)

```sh
docker run --rm -v /path/to/audiobooks:/audiobooks m4b-faststart /audiobooks --dry-run
```

### Verbose logging

```sh
docker run --rm -v /path/to/audiobooks:/audiobooks m4b-faststart /audiobooks -v
```

## Scheduling on a NAS

Point your NAS's task scheduler (Synology Task Scheduler, TrueNAS cron
job, etc.) at a command like:

```sh
docker run --rm -v /volume1/audiobooks:/audiobooks ghcr.io/hypnotoad08/m4b-faststart:latest
```

Run it nightly or weekly — whatever matches how often new books are
added. The container does nothing but exit(0) when the library is
already fully optimized, so there's no harm in running it often.

## Scheduling on a plain Linux server (cron)

No NAS-specific UI needed — the host's own crontab is enough. Weekly is
a good default for a library that mostly gets new books added rather
than re-edited:

```sh
crontab -e
```

```cron
# m4b-faststart: weekly audiobook faststart scan, Sunday 3:15am
15 3 * * 0 /usr/bin/docker run --rm \
  -v /srv/audiobooks:/audiobooks \
  -v /srv/appdata/m4b-faststart:/data \
  ghcr.io/hypnotoad08/m4b-faststart:latest >> /var/log/m4b-faststart.log 2>&1
```

Notes:

- Use the absolute path to `docker` (`which docker`) — cron doesn't
  load your shell's `$PATH`.
- Mount `/data` (the ledger) so only new books cost real time; the
  first run scans the whole library, every run after only touches
  what's changed since.
- Add log rotation for `/var/log/m4b-faststart.log` (e.g. a file in
  `/etc/logrotate.d/`) so it doesn't grow unbounded.
- The script exits non-zero on any failure, so you can chain an alert:
  `... || echo "m4b-faststart FAILED $(date)" >> /var/log/m4b-faststart-errors.log`

## Running without Docker

The script has no dependencies beyond `ffmpeg`/`ffprobe` on `PATH` and
Python 3.8+:

```sh
python3 faststart.py /path/to/audiobooks
```

## Notes

- Only `.m4b` / `.M4B` files are touched.
- Processing is one file at a time, sequentially — this is an I/O-bound
  stream copy, not a CPU-bound transcode, so parallelism isn't worth the
  added complexity/risk here.
- Nothing appears in the audiobooks folder during processing — no temp
  file, no `.bak`. All scratch/recovery files live in `work_dir`
  (default `/data/work`) instead; see
  [In-place updates](#in-place-updates-and-why) above. If a run is
  killed mid-job, the next run cleans up any leftover scratch file in
  `work_dir` automatically.
- The original file's own permissions/ownership are never touched,
  since its inode is never replaced — only its content.
