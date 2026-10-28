#!/bin/bash
# Export everything in Huckleberry into Documents/Millie Data on this Mac:
# an Excel workbook, one CSV per sheet (pandas), a SQLite database (SQL), and
# the entries in Huckleberry's own CSV layout. Also refreshes the Google Sheet.
source "$(dirname "$0")/scripts/common.sh"
need_docker
DEST="$HOME/Documents/Millie Data"
TEMP="$(mktemp "${TMPDIR:-/tmp}/millie-export.XXXXXX")" || die "Couldn't make a temporary file."
compose run --rm -T sync export --tar >"$TEMP"
result=$?
if [ "$result" -ne 0 ] || [ ! -s "$TEMP" ]; then
  rm -f "$TEMP"
  die "The export didn't finish. The reason is above."
fi
mkdir -p "$DEST" || die "Couldn't create $DEST."
tar -xf "$TEMP" -C "$DEST" || {
  rm -f "$TEMP"
  die "Couldn't unpack the export into $DEST."
}
rm -f "$TEMP"
say ""
say "Saved in: $DEST"
if [ "$(uname -s)" = "Darwin" ]; then open "$DEST"; fi
