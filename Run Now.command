#!/bin/bash
# Check Gmail right now instead of waiting for the next 15-minute check.
source "$(dirname "$0")/scripts/common.sh"
need_docker
say "Checking Gmail now..."
compose run --rm -T sync run
