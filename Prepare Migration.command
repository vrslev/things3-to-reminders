#!/bin/zsh
set -euo pipefail
ROOT="${0:A:h}"
cd "$ROOT"
./things-reminders doctor
./things-reminders inspect
RUN_ID="$(date +%Y%m%d-%H%M%S)-$(openssl rand -hex 3)"
RUN_DIR="$HOME/Documents/Things Reminders Migration/$RUN_ID"
./things-reminders plan --run-id "$RUN_ID" --run-dir "$RUN_DIR" --unsupported=manual
open "$RUN_DIR"
print "\nPlan created and opened. Recurring projects are represented as recurring summary reminders with their child-task blueprint in notes."
print "Review report.txt, plan.csv, and MANUAL_REVIEW.md."
print "Then run:"
print "  $ROOT/things-reminders apply '$RUN_DIR'"
read -k 1 '?Press any key to close.'
