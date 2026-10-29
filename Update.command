#!/bin/bash
# Install the version in this folder (after pulling it from GitHub, or unzipping a
# new one) without putting the running sync at risk:
#   1. build the new version; every test runs during the build
#   2. check that the new version can still sign in to everything
#   3. only then switch the background sync over to it (settings and history are kept)
# If step 1 or 2 fails, nothing is changed and the sync you have running keeps going.
source "$(dirname "$0")/scripts/common.sh"
need_docker

image="$(sed -n 's/^ *image: *//p' "$MILLIE_DIR/compose.yaml" | head -n 1)"
say "MillieBeanCounter: update to version ${image##*:}"
say "==========================================="
say ""
say "1) Building and testing the new version. This takes a few minutes, and the"
say "   sync you have running keeps running meanwhile."
compose build --pull sync ||
  die "The new version didn't build, or one of its tests failed (the reason is above). Nothing was changed: the sync you have running keeps running."

say ""
say "2) Checking that the new version can still sign in to everything..."
if ! compose run --rm -T sync check; then
  say ""
  say "Something above is marked ✗. If it already said that before this update (for"
  say "example, a sign-in you haven't fixed yet), it's safe to go ahead anyway."
  answer=""
  read -r -p "Switch to the new version anyway? [y/N]: " answer || true
  case "$answer" in
    [yY]*) ;;
    *) die "Nothing was changed: the sync you have running keeps running." ;;
  esac
fi

say ""
say "3) Switching the background sync over to the new version..."
stop_old_versions
compose up -d sync || die "Docker couldn't start the new version. Double-click Start.command to try again."

say ""
say "Now running, with these library versions:"
compose run --rm -T --entrypoint python sync -c '
from importlib.metadata import version
for name in ("aiohttp", "certifi", "idna", "multidict", "pyasn1", "requests", "urllib3", "yarl"):
    print(f"   {name} {version(name)}")
' || true

# Older images of this project aren't used any more; removing them also clears
# them from Docker Desktop's vulnerability list. Docker refuses to remove any
# image a container still uses, so nothing that's needed can go.
for old in $("$DOCKER_BIN" images millie_bean_counter --format '{{.Repository}}:{{.Tag}}' 2>/dev/null); do
  if [ "$old" != "$image" ] && "$DOCKER_BIN" image rm "$old" >/dev/null 2>&1; then
    say "Removed the old image $old."
  fi
done

say ""
say "Done. Status.command shows what it's doing; your settings and history are unchanged."
