#!/bin/zsh
set -euo pipefail

ROOT="${0:A:h}"
SRC="$ROOT/src/EventKitBridge.swift"
PLIST="$ROOT/src/Info.plist"
OUT="$ROOT/.build/EventKitBridge"
mkdir -p "$ROOT/.build"

if [[ "$(uname -s)" != "Darwin" ]]; then
  print -u2 "This bridge can only be built on macOS."
  exit 1
fi
if ! xcrun --find swiftc >/dev/null 2>&1; then
  print -u2 "Swift compiler not found. Install Xcode Command Line Tools: xcode-select --install"
  exit 1
fi

print "Building EventKit bridge…"
xcrun swiftc \
  -O \
  -framework Foundation \
  -framework EventKit \
  -framework CryptoKit \
  "$SRC" \
  -o "$OUT" \
  -Xlinker -sectcreate \
  -Xlinker __TEXT \
  -Xlinker __info_plist \
  -Xlinker "$PLIST"

codesign --force --sign - --identifier dev.vrslev.things-reminders-migrator "$OUT" >/dev/null
print "Built: $OUT"
