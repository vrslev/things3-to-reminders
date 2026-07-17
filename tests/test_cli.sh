#!/bin/sh
set -eu
ROOT="$(CDPATH= cd -- "$(dirname "$0")/.." && pwd)"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
mkdir "$TMP/archive" "$TMP/main" "$TMP/legacy" "$TMP/manifest-authority"
printf '%s\n' '{"planKind":"verbatim_archive","runID":"archive"}' > "$TMP/archive/plan.json"
printf '%s\n' '{"planKind":"main_migration","runID":"main"}' > "$TMP/main/plan.json"
printf '%s\n' '{"runID":"legacy"}' > "$TMP/legacy/plan.json"
printf '%s\n' '{"planKind":"main_migration","runID":"changed-plan"}' > "$TMP/manifest-authority/plan.json"
printf '%s\n' '{"planKind":"verbatim_archive","runID":"authoritative-manifest"}' > "$TMP/manifest-authority/manifest.json"

assert_kind() {
  expected="$1"; run="$2"
  actual="$($ROOT/things-reminders run-kind "$run")"
  [ "$actual" = "$expected" ] || {
    echo "Expected $expected, got $actual for $run" >&2
    exit 1
  }
}

assert_kind verbatim_archive "$TMP/archive"
assert_kind main_migration "$TMP/main"
assert_kind main_migration "$TMP/legacy"
assert_kind verbatim_archive "$TMP/manifest-authority"
"$ROOT/things-reminders" archive --help | grep -q 'build-index'

echo 'CLI plan-kind and archive dispatch: OK'
