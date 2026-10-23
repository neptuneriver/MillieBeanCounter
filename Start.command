#!/bin/bash
# Turn the background sync on (after Setup.command has been run once).
source "$(dirname "$0")/scripts/common.sh"
need_docker
compose up -d sync || die "Docker couldn't start the sync. Run Setup.command if you haven't yet."
say "Started. It checks Gmail every 15 minutes while Docker Desktop is open."
say "Status.command shows what it has done."
