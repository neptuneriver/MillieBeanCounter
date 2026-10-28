#!/bin/bash
# The setup steps behind Setup.command and My Setup.command. Sourced after
# scripts/common.sh. Nothing in this file is personal.
#
# Setup.command suggests no answers, so anyone can use it with their own accounts.
# A personal launcher can suggest first-time answers by setting any of these
# before it sources this file (My Setup.command does):
#   PRESET_GMAIL          the Gmail inbox the daycare emails go to
#   PRESET_YOUR_EMAIL     your own email (summaries go there; emails you forward from it are accepted)
#   PRESET_CHILD_NAME     your child's name as it should appear in photo names
#   PRESET_BIRTHDAY       your child's birthday, like 3/14/2026
#   PRESET_HB_EMAIL       the email you sign in to Huckleberry with
#   PRESET_HB_CHILD       your child's name in Huckleberry (if not PRESET_CHILD_NAME)
#   PRESET_DAYCARE_CHILD  your child's name on the daily sheet (if not PRESET_CHILD_NAME)
#   PRESET_TIMEZONE       the daycare's time zone, e.g. America/New_York
#   PRESET_DRIVE_PHOTOS   yes to suggest saving the daycare's photos to Google Drive
# Once settings are saved, the saved answers are suggested instead.

SETUP_NAME="${0##*/}"
LOGIN_PORT=8737

# The computer's own time zone: /etc/localtime points at .../zoneinfo/America/Chicago.
local_timezone() {
  local zone
  zone="$(readlink /etc/localtime 2>/dev/null | sed -n 's#^.*/zoneinfo/##p')"
  if [ -n "$zone" ]; then
    printf '%s' "$zone"
  else
    printf '%s' "America/New_York"
  fi
}

# 2026-03-14 (how it's saved) -> 3/14/2026 (how it's typed)
typed_date() {
  local year month day
  case "$1" in
    [0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9])
      year="${1%%-*}" && month="${1#*-}" && day="${month#*-}" && month="${month%%-*}"
      printf '%s/%s/%s' "${month#0}" "${day#0}" "$year"
      ;;
    *) printf '%s' "$1" ;;
  esac
}

# real_past_date MONTH DAY YEAR   Succeeds for a real calendar date that isn't in the future.
real_past_date() {
  local month=$((10#$1)) day=$((10#$2)) year=$((10#$3)) last=31
  [ "$month" -ge 1 ] && [ "$month" -le 12 ] && [ "$year" -ge 1900 ] || return 1
  case "$month" in
    4 | 6 | 9 | 11) last=30 ;;
    2)
      last=28
      if [ $((year % 4)) -eq 0 ] && { [ $((year % 100)) -ne 0 ] || [ $((year % 400)) -eq 0 ]; }; then last=29; fi
      ;;
  esac
  [ "$day" -ge 1 ] && [ "$day" -le "$last" ] || return 1
  [ $((year * 10000 + month * 100 + day)) -le "$(date +%Y%m%d)" ]
}

# ask_birthday VAR "Question" "suggestion"   Asks again until it's a real date like 3/14/2026.
ask_birthday() {
  local __var="$1" __question="$2" __suggested="${3:-}" __birthday="" __m __d __y
  local __us='^([0-9]{1,2})/([0-9]{1,2})/([0-9]{4})$' __iso='^([0-9]{4})-([0-9]{2})-([0-9]{2})$'
  while :; do
    ask_required __birthday "$__question" "$__suggested"
    __birthday="${__birthday// /}"
    __m="" __d="" __y=""
    if [[ $__birthday =~ $__us ]]; then
      __m="${BASH_REMATCH[1]}" __d="${BASH_REMATCH[2]}" __y="${BASH_REMATCH[3]}"
    elif [[ $__birthday =~ $__iso ]]; then
      __y="${BASH_REMATCH[1]}" __m="${BASH_REMATCH[2]}" __d="${BASH_REMATCH[3]}"
    fi
    if [ -n "$__m" ] && real_past_date "$__m" "$__d" "$__y"; then break; fi
    say "   Type a real date (not in the future) as month/day/year, like 3/14/2026."
    __suggested=""
  done
  printf -v "$__var" '%s' "$__birthday"
}

yes_or_no() {
  case "$1" in
    [yY] | [yY][eE][sS]) printf 'yes' ;;
    *) printf 'no' ;;
  esac
}

# Shows the sign-in's output and opens its Google link in the browser as soon as it appears.
open_sign_in_link() {
  local line url opened=""
  while IFS= read -r line; do
    printf '%s\n' "$line"
    case "$line" in
      *https://accounts.google.com/*)
        [ -z "$opened" ] || continue
        opened="yes"
        url="https://${line#*https://}"
        if [ "$(uname -s)" = "Darwin" ]; then
          open "$url" >/dev/null 2>&1 || true
        elif command -v xdg-open >/dev/null 2>&1; then
          xdg-open "$url" >/dev/null 2>&1 || true
        fi
        say "   Your browser should open to Google now. If it doesn't, copy the long link above into it."
        ;;
    esac
  done
}

drive_sign_in() {
  say ""
  compose run --rm -T -p "127.0.0.1:$LOGIN_PORT:$LOGIN_PORT" sync drive-login --hint "$1" | open_sign_in_link
}

say "MillieBeanCounter: setup"
say "========================"
need_docker
stop_old_versions

say ""
say "Building the Docker image. The first time takes a few minutes..."
compose build sync || die "The Docker build stopped. The reason is a few lines above this."
adopt_old_data

current="$(compose run --rm -T sync show-settings 2>/dev/null || true)"
saved() { printf '%s\n' "$current" | sed -n "s/^$1=//p" | head -n 1; }

if [ -n "$(saved GMAIL_ADDRESS)" ]; then
  first_time=""
  say ""
  say "Your saved settings are in [brackets]. Press Return to keep each one."
  d_gmail="$(saved GMAIL_ADDRESS)"
  # Settings added in 2.0 aren't saved yet after an upgrade: fall back to the presets.
  d_child="$(saved CHILD_NAME)"
  [ -n "$d_child" ] || d_child="${PRESET_CHILD_NAME:-}"
  [ -n "$d_child" ] || d_child="$(saved HUCKLEBERRY_CHILD)"
  [ -n "$d_child" ] || d_child="$(saved DAYCARE_CHILD)"
  d_birthday="$(typed_date "$(saved CHILD_BIRTHDAY)")"
  [ -n "$d_birthday" ] || d_birthday="${PRESET_BIRTHDAY:-}"
  d_hb_email="$(saved HUCKLEBERRY_EMAIL)"
  d_hb_child="$(saved HUCKLEBERRY_CHILD)"
  d_daycare_child="$(saved DAYCARE_CHILD)"
  d_timezone="$(saved TIMEZONE)"
  [ -n "$d_timezone" ] || d_timezone="America/New_York" # what a blank setting means
  d_senders="$(saved ALLOWED_SENDERS)"
  d_notify="$(saved NOTIFY_EMAIL)"
  [ -n "$d_notify" ] || d_notify="none"
  d_photos="$(saved DRIVE_PHOTOS)"
  d_photos="$(yes_or_no "${d_photos:-${PRESET_DRIVE_PHOTOS:-no}}")"
  d_folder="$(saved DRIVE_FOLDER)"
  d_sheet="$(saved GOOGLE_SHEET)"
  [ -n "$d_sheet" ] || d_sheet="none"
else
  first_time="yes"
  say ""
  say "A few questions. Suggestions are in [brackets]; press Return to accept one."
  d_gmail="${PRESET_GMAIL:-}"
  d_child="${PRESET_CHILD_NAME:-}"
  d_birthday="${PRESET_BIRTHDAY:-}"
  d_hb_email="${PRESET_HB_EMAIL:-}"
  d_hb_child="${PRESET_HB_CHILD:-}"
  d_daycare_child="${PRESET_DAYCARE_CHILD:-}"
  d_timezone="${PRESET_TIMEZONE:-$(local_timezone)}"
  d_senders="" # suggested from your own email below
  d_notify=""
  d_photos="$(yes_or_no "${PRESET_DRIVE_PHOTOS:-no}")"
  d_folder=""
  d_sheet="none"
fi

say ""
say "1) The Gmail inbox the daycare's emails are sent or forwarded to"
ask_required GMAIL_ADDRESS "   Address" "$d_gmail"
ask_secret GMAIL_APP_PASSWORD "   App password for that inbox (16 letters)" "$(saved GMAIL_APP_PASSWORD_SAVED)"
say ""
say "2) Your child"
ask_required CHILD_NAME "   Name, as you'd like it in photo names" "$d_child"
ask_birthday CHILD_BIRTHDAY "   Birthday (month/day/year)" "$d_birthday"
ask DAYCARE_CHILD "   Name on the daycare's daily sheet" "${d_daycare_child:-$CHILD_NAME}"
say ""
say "3) Huckleberry"
ask_required HUCKLEBERRY_EMAIL "   Email you sign in to Huckleberry with" "$d_hb_email"
ask_secret HUCKLEBERRY_PASSWORD "   Huckleberry password" "$(saved HUCKLEBERRY_PASSWORD_SAVED)"
ask HUCKLEBERRY_CHILD "   Child's name in Huckleberry" "${d_hb_child:-$CHILD_NAME}"
ask TIMEZONE "   Time zone the daycare is in" "$d_timezone"
say ""
say "4) Email"
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
ask ALLOWED_SENDERS "   Accept daycare emails sent or forwarded by" "$d_senders"
ask NOTIFY_EMAIL "   Email a summary after each daily sheet to (or type none)" "$d_notify"
case "$NOTIFY_EMAIL" in
  none | None | NONE | no | No | off | Off) NOTIFY_EMAIL="" ;;
esac

say ""
say "5) The daycare's photos, saved to Google Drive by month (optional; README.md has the Google steps)"
ask DRIVE_PHOTOS "   Save the daycare's photos to Google Drive? (yes/no)" "$d_photos"
DRIVE_PHOTOS="$(yes_or_no "$DRIVE_PHOTOS")"
DRIVE_FOLDER="$d_folder"
if [ "$DRIVE_PHOTOS" = "yes" ]; then
  ask DRIVE_FOLDER "   Google Drive folder for them" "${d_folder:-$CHILD_NAME Daycare Photos}"
  client_saved="$(saved DRIVE_CLIENT_SAVED)"
  drive_account="$(saved DRIVE_ACCOUNT)"
  if [ "$client_saved" = "yes" ]; then
    say "   Drag in a new OAuth client file (.json) to replace the saved one, or just press Return."
  else
    say "   Drag the OAuth client file (.json) from Google Cloud into this window, then press Return."
  fi
  CLIENT_PATH=""
  read -r -p "   > " CLIENT_PATH || true
  CLIENT_PATH="$(dragged_path "$CLIENT_PATH")"
  if [ -n "$CLIENT_PATH" ]; then
    [ -f "$CLIENT_PATH" ] || die "Couldn't find that file: $CLIENT_PATH"
    compose run --rm -T sync save-drive-client <"$CLIENT_PATH" || die "Run $SETUP_NAME again with the right file."
    client_saved="yes"
    drive_account="" # a new client needs a new sign-in
  fi
  if [ "$client_saved" != "yes" ]; then
    say "   No OAuth client file, so saving photos stays off for now."
    DRIVE_PHOTOS="no"
  else
    sign_in="yes"
    if [ -n "$drive_account" ]; then
      ask again "   Signed in to Google Drive as $drive_account. Sign in again? (yes/no)" "no"
      [ "$(yes_or_no "$again")" = "yes" ] || sign_in=""
    fi
    if [ -n "$sign_in" ] && ! drive_sign_in "$GMAIL_ADDRESS"; then
      if [ -n "$drive_account" ]; then
        say "   Kept the earlier Google Drive sign-in ($drive_account)."
      else
        say "   Saving photos stays off until the Google Drive sign-in works. Run $SETUP_NAME again to retry."
        DRIVE_PHOTOS="no"
      fi
    fi
  fi
fi

say ""
say "6) Google Sheet copy of all the data (optional; README.md has the Google steps)"
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
  printf 'CHILD_NAME=%s\n' "$CHILD_NAME"
  printf 'CHILD_BIRTHDAY=%s\n' "$CHILD_BIRTHDAY"
  printf 'HUCKLEBERRY_EMAIL=%s\n' "$HUCKLEBERRY_EMAIL"
  printf 'HUCKLEBERRY_PASSWORD=%s\n' "$HUCKLEBERRY_PASSWORD"
  printf 'HUCKLEBERRY_CHILD=%s\n' "$HUCKLEBERRY_CHILD"
  printf 'DAYCARE_CHILD=%s\n' "$DAYCARE_CHILD"
  printf 'TIMEZONE=%s\n' "$TIMEZONE"
  printf 'ALLOWED_SENDERS=%s\n' "$ALLOWED_SENDERS"
  printf 'NOTIFY_EMAIL=%s\n' "$NOTIFY_EMAIL"
  printf 'DRIVE_PHOTOS=%s\n' "$DRIVE_PHOTOS"
  printf 'DRIVE_FOLDER=%s\n' "$DRIVE_FOLDER"
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
say "Here's what the sync would do right now. Nothing has been written yet."
say "-----------------------------------------------------------------------"
compose run --rm -T sync preview
say "-----------------------------------------------------------------------"
say ""
say "What now?"
say "  Y = start the sync and add the daily sheets listed above"
say "  s = start the sync but skip those sheets (only newer ones get added)"
say "  n = don't start yet"
if [ "$DRIVE_PHOTOS" = "yes" ]; then
  say "Either way, the photos go to Google Drive once it's started."
fi
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
say "Get Data.command saves all the data to Documents/MillieBeanCounter Data (Excel, CSV and SQLite)."
