#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m unittest discover -s tests -p 'test_*.py' -v
sh tests/test_cli.sh
swiftc -frontend -parse src/EventKitBridge.swift
