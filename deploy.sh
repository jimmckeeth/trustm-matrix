#!/usr/bin/env bash
# SPDX-License-Identifier: AGPL-3.0-or-later
# Deploy the Trust M matrix code to the board's CIRCUITPY drive.
#
#   ./deploy.sh [options]
#     -w, --wait SECONDS   wait up to this long for the board (default 30)
#     -n, --dry-run        show what would be copied, change nothing
#     -k, --keep-mounted   leave the drive mounted afterwards (default: unmount)
#     -h, --help
#
# Finds the drive by its CIRCUITPY label (no hardcoded /dev/sdX or mount
# path), mounts it via udisks if the desktop hasn't already (no root needed),
# refuses to write to a read-only mount, copies the support modules first and
# code.py LAST (CircuitPython auto-reloads on every write, so this keeps it
# from running a half-copied set), syncs, verifies every file byte-for-byte,
# and unmounts so the board can safely re-read the drive.

set -euo pipefail

LABEL=CIRCUITPY
WAIT=30
DRY=0
KEEP=0

while [[ $# -gt 0 ]]; do
    case $1 in
        -w | --wait) WAIT=${2:?--wait needs a number}; shift 2 ;;
        -n | --dry-run) DRY=1; shift ;;
        -k | --keep-mounted) KEEP=1; shift ;;
        -h | --help) sed -n '2,/^$/s/^# \{0,1\}//p' "$0"; exit 0 ;;
        *) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")"

# Support modules first, code.py last (see header). Globs keep new viz_*.py
# files from being forgotten.
SUPPORT=(vizcore.py viz_*.py trustm.py trustm_crypto.py)
FILES=("${SUPPORT[@]}" code.py)
for f in "${FILES[@]}"; do
    [[ -f $f ]] || { echo "missing source file: $f" >&2; exit 1; }
done

die() { echo "error: $*" >&2; exit 1; }

# --- locate the device, waiting for it to show up --------------------------
dev=
deadline=$((SECONDS + WAIT))
while :; do
    if [[ -e /dev/disk/by-label/$LABEL ]]; then
        dev=$(readlink -f "/dev/disk/by-label/$LABEL")
        break
    fi
    ((SECONDS < deadline)) || die "no $LABEL drive found after ${WAIT}s -- is the board plugged in (data cable) and not in bootloader mode?"
    [[ -n ${waited:-} ]] || echo "waiting for $LABEL (up to ${WAIT}s)..."
    waited=1
    sleep 1
done
echo "device: $dev"

# --- find or create the mount ----------------------------------------------
mnt=$(findmnt -rno TARGET "$dev" | head -n1 || true)
if [[ -z $mnt ]]; then
    command -v udisksctl >/dev/null || die "$dev isn't mounted and udisksctl isn't installed; mount it manually"
    if ((DRY)); then
        echo "[dry-run] would mount $dev"
        mnt="(not mounted)"
    else
        udisksctl mount -b "$dev" --no-user-interaction >/dev/null || die "mount failed"
        mnt=$(findmnt -rno TARGET "$dev" | head -n1)
    fi
fi
echo "mount:  $mnt"

# --- refuse a read-only mount: that means filesystem damage, not our fault --
if ((!DRY)); then
    opts=$(findmnt -rno OPTIONS "$dev")
    if [[ ,$opts, == *,ro,* ]]; then
        cat >&2 <<EOF
error: $mnt is mounted READ-ONLY. Linux does this after FAT errors
(see: journalctl -k | grep FAT-fs). Repair it, then replug the board:
    sudo umount $mnt
    sudo fsck.vfat -a $dev
EOF
        exit 1
    fi
fi

# --- copy, sync, verify ----------------------------------------------------
if ((DRY)); then
    printf '[dry-run] would copy to %s:\n' "$LABEL"
    printf '  %s\n' "${FILES[@]}"
    exit 0
fi

for f in "${FILES[@]}"; do
    printf '  %-22s' "$f"
    cp -f "$f" "$mnt/$f"
    echo "copied"
done
echo "syncing..."
sync

bad=0
for f in "${FILES[@]}"; do
    cmp -s "$f" "$mnt/$f" || { echo "VERIFY FAILED: $f" >&2; bad=1; }
done
((!bad)) || die "verification failed -- do not unplug; check the drive (fsck) and retry"
echo "verified ${#FILES[@]} files OK"

# --- unmount so the board gets the drive back cleanly ----------------------
if ((!KEEP)); then
    udisksctl unmount -b "$dev" --no-user-interaction >/dev/null && echo "unmounted $dev"
fi
echo "done -- the board reloads code.py on its own."
