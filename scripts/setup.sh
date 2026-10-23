#!/bin/bash
# The setup steps behind Setup.command and My Setup.command. Sourced after
# scripts/common.sh. Nothing in this file is personal.
#
# Setup.command suggests no answers, so anyone can use it with their own accounts.
# A personal launcher can suggest first-time answers by setting any of these
# before it sources this file (My Setup.command does):
#   PRESET_GMAIL          the Gmail inbox the daily sheets go to
#   PRESET_YOUR_EMAIL     your own email (summaries go there; sheets you forward from it are accepted)
#   PRESET_HB_EMAIL       the email you sign in to Huckleberry with
#   PRESET_HB_CHILD       your child's name in Huckleberry
#   PRESET_DAYCARE_CHILD  your child's name on the daily sheet
#   PRESET_TIMEZONE       the daycare's time zone, e.g. America/New_York
# Once settings are saved, the saved answers are suggested instead.

SETUP_NAME="${0##*/}"

# The Mac's own time zone: /etc/localtime points at .../zoneinfo/America/Chicago.
mac_timezone() {
  local zone
  zone="$(readlink /etc/localtime 2>/dev/null | sed -n 's#^.*/zoneinfo/##p')"
  if [ -n "$zone" ]; then
    printf '%s' "$zone"
  else
    printf '%s' "America/New_York"
  fi
}

say "Millie daycare sync: setup"
say "=========================="
need_docker
stop_old_versions

say ""
say "Building the Docker image. The first time takes a few minutes..."
compose build sync || die "The Docker build stopped. The reason is a few lines above this."

current="$(compose run --rm -T sync show-settings 2>/dev/null || true)"
saved() { printf '%s\n' "$current" | sed -n "s/^$1=//p" | head -n 1; }

if [ -n "$(saved GMAIL_ADDRESS)" ]; then
  first_time=""
  say ""
  say "Your saved settings are in [brackets]. Press Return to keep each one."
  d_gmail="$(saved GMAIL_ADDRESS)"
  d_hb_email="$(saved HUCKLEBERRY_EMAIL)"
  d_hb_child="$(saved HUCKLEBERRY_CHILD)"
  d_daycare_child="$(saved DAYCARE_CHILD)"
  d_timezone="$(saved TIMEZONE)"
  [ -n "$d_timezone" ] || d_timezone="America/New_York" # what a blank setting means
  d_senders="$(saved ALLOWED_SENDERS)"
  d_notify="$(saved NOTIFY_EMAIL)"
  [ -n "$d_notify" ] || d_notify="none"
  d_sheet="$(saved GOOGLE_SHEET)"
  [ -n "$d_sheet" ] || d_sheet="none"
else
  first_time="yes"
  say ""
  say "A few questions. Suggestions are in [brackets]; press Return to accept one."
  d_gmail="${PRESET_GMAIL:-}"
  d_hb_email="${PRESET_HB_EMAIL:-}"
  d_hb_child="${PRESET_HB_CHILD:-}"
  d_daycare_child="${PRESET_DAYCARE_CHILD:-}"
  d_timezone="${PRESET_TIMEZONE:-$(mac_timezone)}"
  d_senders="" # suggested from your own email below
  d_notify=""
  d_sheet="none"
fi

say ""
say "1) The Gmail inbox the daily sheets are sent or forwarded to"
ask_required GMAIL_ADDRESS "   Address" "$d_gmail"
ask_secret GMAIL_APP_PASSWORD "   App password for that inbox (16 letters)" "$(saved GMAIL_APP_PASSWORD_SAVED)"
say ""
say "2) Huckleberry"
ask_required HUCKLEBERRY_EMAIL "   Email you sign in to Huckleberry with" "$d_hb_email"
ask_secret HUCKLEBERRY_PASSWORD "   Huckleberry password" "$(saved HUCKLEBERRY_PASSWORD_SAVED)"
ask HUCKLEBERRY_CHILD "   Child's name in Huckleberry" "$d_hb_child"
ask DAYCARE_CHILD "   Child's name on the daycare sheet" "$d_daycare_child"
ask TIMEZONE "   Time zone the daycare is in" "$d_timezone"
say ""
say "3) Email"
if [ -n "$first_time" ]; then
  ask YOUR_EMAIL "   Your own email address" "${PRESET_YOUR_EMAIL:-}"
  if [ -n "$YOUR_EMAIL" ]; then
    d_senders="$YOUR_EMAIL, kaymbu.com"
    d_notify="$YOUR_EMAIL"
  else
    d_senders="kaymbu.com"
    d_notify="none"
  fi
fi
ask ALLOWED_SENDERS "   Accept daily sheets sent or forwarded by" "$d_senders"
ask NOTIFY_EMAIL "   Email a summary after each import to (or type none)" "$d_notify"
case "$NOTIFY_EMAIL" in
  none | None | NONE | no | No | off | Off) NOTIFY_EMAIL="" ;;
esac
say ""
say "4) Google Sheet copy of all the data (optional; README.md has the Google steps)"
ask GOOGLE_SHEET "   Link to the Google Sheet (or type none)" "$d_sheet"
case "$GOOGLE_SHEET" in
  none | None | NONE | no | No | off | Off) GOOGLE_SHEET="" ;;
esac
if [ -n "$GOOGLE_SHEET" ]; then
  if [ "$(saved GOOGLE_KEY_SAVED)" = "yes" ]; then
    say "   Drag in a new service account key (.json) to replace the saved one, or just press Return."
  else
    say "   Drag the service account key file (.json) into this window, then press Return."
  fi
  KEY_PATH=""
  read -r -p "   > " KEY_PATH || true
  KEY_PATH="$(dragged_path "$KEY_PATH")"
  if [ -n "$KEY_PATH" ]; then
    [ -f "$KEY_PATH" ] || die "Couldn't find that file: $KEY_PATH"
    compose run --rm -T sync save-google-key <"$KEY_PATH" || die "Run $SETUP_NAME again with the right key file."
  elif [ "$(saved GOOGLE_KEY_SAVED)" != "yes" ]; then
    say "   No key given, so the Google Sheet copy stays off for now."
    GOOGLE_SHEET=""
  fi
fi

say ""
{
  printf 'GMAIL_ADDRESS=%s\n' "$GMAIL_ADDRESS"
  printf 'GMAIL_APP_PASSWORD=%s\n' "$GMAIL_APP_PASSWORD"
  printf 'HUCKLEBERRY_EMAIL=%s\n' "$HUCKLEBERRY_EMAIL"
  printf 'HUCKLEBERRY_PASSWORD=%s\n' "$HUCKLEBERRY_PASSWORD"
  printf 'HUCKLEBERRY_CHILD=%s\n' "$HUCKLEBERRY_CHILD"
  printf 'DAYCARE_CHILD=%s\n' "$DAYCARE_CHILD"
  printf 'TIMEZONE=%s\n' "$TIMEZONE"
  printf 'ALLOWED_SENDERS=%s\n' "$ALLOWED_SENDERS"
  printf 'NOTIFY_EMAIL=%s\n' "$NOTIFY_EMAIL"
  printf 'GOOGLE_SHEET=%s\n' "$GOOGLE_SHEET"
} | compose run --rm -T sync save-settings
saved_ok=$?
GMAIL_APP_PASSWORD=""
HUCKLEBERRY_PASSWORD=""
[ "$saved_ok" -eq 0 ] || die "Run $SETUP_NAME again to fix the settings above."

say ""
say "Checking the connections..."
compose run --rm -T sync check || die "Fix whatever is marked ✗ above, then run $SETUP_NAME again."

if [ -n "$NOTIFY_EMAIL" ]; then
  compose run --rm -T sync test-email || say "(The sync still works without summary emails.)"
fi

say ""
say "Here's what the sync would add right now. Nothing has been written yet."
say "-----------------------------------------------------------------------"
compose run --rm -T sync preview
say "-----------------------------------------------------------------------"
say ""
say "What now?"
say "  Y = start the sync and add what's listed above"
say "  s = start the sync but skip the sheets above (only newer ones get added)"
say "  n = don't start yet"
choice=""
read -r -p "Your choice [Y/s/n]: " choice || true
case "$choice" in
  [nN]*)
    compose stop sync >/dev/null 2>&1 || true
    say "Not started. Double-click Start.command when you're ready."
    exit 0
    ;;
  [sS]*)
    compose run --rm -T sync skip-new || die "Couldn't mark those sheets as skipped; nothing was started."
    ;;
esac
compose up -d sync || die "Docker couldn't start the sync."
say ""
say "Running. It checks Gmail every 15 minutes whenever Docker Desktop is open."
say "Status.command shows what it has done; Stop.command pauses it."
say "Get Data.command saves all the data to Documents/Millie Data (Excel, CSV and SQLite)."
