#!/bin/sh
set -eu
cd "$(dirname "$0")"
python3 -m unittest -v tests/test_planner.py
swiftc -frontend -parse src/EventKitBridge.swift
