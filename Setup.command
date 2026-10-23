#!/bin/bash
# Set up or update the Millie daycare sync with your own accounts. Safe to run
# again any time; it keeps your saved settings unless you type new ones.
# (The questions themselves are in scripts/setup.sh.)
source "$(dirname "$0")/scripts/common.sh"
source "$MILLIE_DIR/scripts/setup.sh"
