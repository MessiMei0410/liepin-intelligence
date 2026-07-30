#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
ARCH="$(uname -m)"
TEST_DIR="$(mktemp -d "${TMPDIR:-/tmp}/asa-native-tests.XXXXXX")"
trap 'rm -rf "$TEST_DIR"' EXIT

swiftc \
  -target "${ARCH}-apple-macos${DEPLOYMENT_TARGET}" \
  -o "$TEST_DIR/native-boundary-tests" \
  src/WebSecurityPolicy.swift \
  src/NativeContextPrivacy.swift \
  tests/NativeBoundaryTests.swift

"$TEST_DIR/native-boundary-tests"
echo "ASA native boundary tests passed"
