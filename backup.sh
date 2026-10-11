#!/bin/bash
# Stash nightly backup: local-first (sqlite .backup + rsync of uploads), then a time-capped iCloud mirror.
# Never writes into iCloud Drive directly first: FileProvider has wedged the mini before.
set -u
umask 077   # snapshots hold the inventory + photos; owner-only (security review 2026-10-07)
APP="$HOME/Stash"; DST="$HOME/Backups/stash-nightly"; mkdir -p "$DST/uploads"; chmod 700 "$DST"
STAMP=$(date +%F)
/usr/bin/sqlite3 "$APP/stash.db" ".backup '$DST/stash-$STAMP.db'" && echo "$(date '+%F %T') db snapshot ok"
# Orphan sweep first: files no attachment points at go to ~/Stash/orphans (purged after 30 days).
"$APP/venv/bin/python" "$APP/app.py" --sweep
# Daily hardlinked snapshot of uploads before the mirror runs --delete: a photo deleted in the app
# survives in the dated snapshots for 30 days. Unchanged files cost no extra disk (link-dest).
/usr/bin/rsync -a --link-dest="$DST/uploads" "$APP/uploads/" "$DST/uploads-$STAMP/" && echo "$(date '+%F %T') uploads snapshot ok"
ls -1dt "$DST"/uploads-20* 2>/dev/null | tail -n +31 | while read -r d; do rm -rf "$d"; done
/usr/bin/rsync -a --delete "$APP/uploads/" "$DST/uploads/" && echo "$(date '+%F %T') uploads mirror ok ($(du -sh "$DST/uploads" | cut -f1))"
ls -1t "$DST"/stash-*.db | tail -n +31 | xargs -I{} rm -f {}
ICLOUD="$HOME/Library/Mobile Documents/com~apple~CloudDocs/Stash Backups"
# cp, not rsync: under launchd rsync gets "Operation not permitted" writing into iCloud Drive (TCC), cp does not.
( mkdir -p "$ICLOUD/uploads" 2>/dev/null && cp "$DST/stash-$STAMP.db" "$ICLOUD/" && cp -R "$DST/uploads/." "$ICLOUD/uploads/" ) &
P=$!; ( sleep 180; kill $P 2>/dev/null && echo "$(date '+%F %T') iCloud mirror TIMED OUT (wedged?)" ) & W=$!
wait $P 2>/dev/null && { kill $W 2>/dev/null; wait $W 2>/dev/null; echo "$(date '+%F %T') iCloud mirror ok"; }
