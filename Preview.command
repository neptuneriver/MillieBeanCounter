#!/bin/bash
# Show what the sync would add right now, without writing anything to Huckleberry.
source "$(dirname "$0")/scripts/common.sh"
need_docker
compose run --rm -T sync preview
