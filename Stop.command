#!/bin/bash
# Pause the background sync. Settings and history are kept.
source "$(dirname "$0")/scripts/common.sh"
need_docker
compose stop sync || die "Docker couldn't stop the sync."
say "Stopped. Start.command turns it back on; nothing is lost."
