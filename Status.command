#!/bin/bash
# What has the sync been doing?
source "$(dirname "$0")/scripts/common.sh"
need_docker
compose ps sync
say ""
compose run --rm -T sync status
say ""
say "Latest activity:"
compose logs --no-log-prefix --tail 40 sync
