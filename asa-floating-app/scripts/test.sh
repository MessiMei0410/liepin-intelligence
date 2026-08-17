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
  src/HotKeyRouting.swift \
  src/ExternalLinkRouting.swift \
  src/DiagnosticsPage.swift \
  tests/NativeBoundaryTests.swift

"$TEST_DIR/native-boundary-tests"

# Full typecheck so AppDelegate/main.swift stay under compile-time guard
# even though they are not linked into the test binary.
swiftc -typecheck -target "${ARCH}-apple-macos${DEPLOYMENT_TARGET}" src/*.swift

echo "ASA native boundary tests passed"
