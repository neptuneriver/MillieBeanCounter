#!/bin/bash
# Check the export against the CSV Huckleberry emails you (in the Huckleberry app,
# use its data export to have the CSV sent to you, and save that file first).
source "$(dirname "$0")/scripts/common.sh"
need_docker
say "Drag the CSV file from Huckleberry into this window, then press Return:"
CSV_PATH=""
read -r -p "> " CSV_PATH || true
CSV_PATH="$(dragged_path "$CSV_PATH")"
[ -f "$CSV_PATH" ] || die "Couldn't find that file: $CSV_PATH"
DEST="$HOME/Documents/Millie Data"
TEMP="$(mktemp "${TMPDIR:-/tmp}/millie-export.XXXXXX")" || die "Couldn't make a temporary file."
compose run --rm -T sync export --tar --compare <"$CSV_PATH" >"$TEMP"
result=$?
if [ ! -s "$TEMP" ]; then
  rm -f "$TEMP"
  die "The check didn't finish. The reason is above."
fi
mkdir -p "$DEST" && tar -xf "$TEMP" -C "$DEST"
rm -f "$TEMP"
say ""
if [ "$result" -eq 0 ]; then
  say "Everything matches. The fresh export is in: $DEST"
else
  say "The full list of differences is in: $DEST/verify_report.txt"
fi
if [ "$(uname -s)" = "Darwin" ]; then open "$DEST"; fi
