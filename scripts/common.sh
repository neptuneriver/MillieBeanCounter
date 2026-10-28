#!/bin/bash
# Shared by the .command files. They only run Docker; nothing gets installed on the Mac.
# Written for the bash 3.2 that ships with macOS.
set -u -o pipefail
umask 077

MILLIE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$MILLIE_DIR" || exit 1
DOCKER_BIN=""
COMPOSE_BIN=""

say() { printf '%s\n' "$*"; }
die() {
  printf '\n%s\n' "$*" >&2
  exit 1
}

finish() {
  local code=$?
  if [ -t 0 ]; then
    echo
    read -r -p "Press Return to close this window. " _ || true
  fi
  exit "$code"
}
trap finish EXIT

compose() {
  if [ -n "$COMPOSE_BIN" ]; then
    "$COMPOSE_BIN" --project-directory "$MILLIE_DIR" -f "$MILLIE_DIR/compose.yaml" "$@"
  else
    "$DOCKER_BIN" compose --project-directory "$MILLIE_DIR" -f "$MILLIE_DIR/compose.yaml" "$@"
  fi
}

find_docker() {
  local dir
  DOCKER_BIN="$(command -v docker 2>/dev/null || true)"
  if [ -z "$DOCKER_BIN" ] || [ ! -x "$DOCKER_BIN" ]; then
    DOCKER_BIN=""
    for dir in "$@"; do
      if [ -x "$dir/docker" ]; then
        DOCKER_BIN="$dir/docker"
        break
      fi
    done
  fi
  [ -n "$DOCKER_BIN" ] || return 1
  # Docker Desktop's credential helpers live next to its CLI; Finder-launched
  # Terminal windows don't always have them on PATH.
  for dir in "$@"; do
    if [ -d "$dir" ]; then PATH="$PATH:$dir"; fi
  done
  PATH="$(dirname "$DOCKER_BIN"):$PATH"
  export PATH
}

find_compose() {
  local candidate version
  COMPOSE_BIN=""
  if "$DOCKER_BIN" compose version >/dev/null 2>&1; then return 0; fi
  for candidate in "$@"; do
    [ -x "$candidate" ] || continue
    version="$("$candidate" version --short 2>/dev/null)" || continue
    case "$version" in
      v1.* | 1.*) continue ;;
    esac
    COMPOSE_BIN="$candidate"
    return 0
  done
  return 1
}

need_docker() {
  if ! find_docker "/Applications/Docker.app/Contents/Resources/bin" "$HOME/.docker/bin" \
    "$HOME/Applications/Docker.app/Contents/Resources/bin" /usr/local/bin /opt/homebrew/bin; then
    die "Couldn't find Docker. Install Docker Desktop (or open it once), then try again."
  fi
  if ! "$DOCKER_BIN" info >/dev/null 2>&1; then
    die "Docker Desktop isn't running yet. Open it, wait until it says the engine is running, then try again."
  fi
  if ! find_compose "/Applications/Docker.app/Contents/Resources/cli-plugins/docker-compose" \
    "$HOME/.docker/cli-plugins/docker-compose" "/usr/local/lib/docker/cli-plugins/docker-compose" \
    "/opt/homebrew/lib/docker/cli-plugins/docker-compose"; then
    die "Docker Compose is missing. Update Docker Desktop, then try again."
  fi
}

stop_old_versions() {
  # The earlier Android-emulator version ran as Compose project "millie-daycare".
  # Stop it so two importers never run. Its data volume is left alone.
  local ids
  ids="$("$DOCKER_BIN" ps -q --filter "label=com.docker.compose.project=millie-daycare" 2>/dev/null || true)"
  if [ -n "$ids" ]; then
    say "Stopping the earlier emulator-based version (its data is kept)..."
    # shellcheck disable=SC2086
    "$DOCKER_BIN" update --restart=no $ids >/dev/null 2>&1 || true
    # shellcheck disable=SC2086
    "$DOCKER_BIN" stop $ids >/dev/null 2>&1 || true
  fi
  # The version before that used a Mac scheduled job. Turn it off if it's still there.
  [ "$(uname -s)" = "Darwin" ] || return 0
  local label="local.millie.daycare-import"
  local job="$HOME/Library/LaunchAgents/$label.plist"
  if launchctl print "gui/$(id -u)/$label" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)/$label" >/dev/null 2>&1 || true
  fi
  if [ -f "$job" ]; then
    mv "$job" "$job.disabled-$(date +%Y%m%d%H%M%S)" && say "Turned off the earlier Mac scheduled job."
  fi
}

# ask VAR "Question" "suggestion"
ask() {
  local __name="$1" __question="$2" __default="${3:-}" __reply=""
  if [ -n "$__default" ]; then
    read -r -p "$__question [$__default]: " __reply || true
  else
    read -r -p "$__question: " __reply || true
  fi
  [ -n "$__reply" ] || __reply="$__default"
  printf -v "$__name" '%s' "$__reply"
}

# dragged_path "text"   Turns what Terminal types when you drop a file into a plain path
# (it escapes spaces with backslashes and adds a trailing space).
dragged_path() {
  local p="$1"
  p="${p#"${p%%[![:space:]]*}"}"
  p="${p%"${p##*[![:space:]]}"}"
  case "$p" in
    \'*\') p="${p#\'}" && p="${p%\'}" ;;
    \"*\") p="${p#\"}" && p="${p%\"}" ;;
    *) p="$(printf '%s' "$p" | sed 's/\\\(.\)/\1/g')" ;;
  esac
  printf '%s' "$p"
}

# ask_secret VAR "Question" saved   (saved is yes/no; typing is hidden)
ask_secret() {
  local __name="$1" __question="$2" __saved="${3:-no}" __reply="" __hint="hidden as you type"
  [ "$__saved" = "yes" ] && __hint="hidden; press Return to keep the saved one"
  while :; do
    if ! read -r -s -p "$__question ($__hint): " __reply; then
      echo
      die "No answer was typed. Run Setup.command again."
    fi
    echo
    if [ -n "$__reply" ] || [ "$__saved" = "yes" ]; then break; fi
    say "   This one is required."
  done
  printf -v "$__name" '%s' "$__reply"
}
