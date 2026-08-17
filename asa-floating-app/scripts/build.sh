#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="ASA"
BUILD_DIR="$PWD/build"
BUNDLE="$BUILD_DIR/${APP_NAME}.app.staging"
EXECUTABLE="asa-floating"
ICONSET="$BUILD_DIR/AppIcon.iconset"
INSTALL_DIR="${ASA_INSTALL_DIR:-${HOME}/Applications}"
INSTALL_BUNDLE="$INSTALL_DIR/${APP_NAME}.app"
INSTALL_STAGING="$INSTALL_DIR/.${APP_NAME}.app.installing"
PREVIOUS_BUNDLE="$BUILD_DIR/${APP_NAME}.app.previous"
SIGN_IDENTITY="ASA Floating Local Code Signing"
DEPLOYMENT_TARGET="${MACOSX_DEPLOYMENT_TARGET:-13.0}"
SIGNING_TIMEOUT_SECONDS="${ASA_SIGNING_TIMEOUT_SECONDS:-60}"
ARCH="$(uname -m)"
SIGNING_MODE="${ASA_SIGNING_MODE:-stable}"

rm -rf "$BUILD_DIR" "$EXECUTABLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

swiftc \
  -target "${ARCH}-apple-macos${DEPLOYMENT_TARGET}" \
  -o "$EXECUTABLE" \
  src/main.swift \
  src/WebSecurityPolicy.swift \
  src/NativeContextPrivacy.swift \
  src/HotKeyRouting.swift \
  src/ExternalLinkRouting.swift \
  src/DiagnosticsPage.swift \
  src/DetachedCandidateList.swift \
  src/AppDelegate.swift \
  -framework Cocoa \
  -framework Vision \
  -framework WebKit

mv "$EXECUTABLE" "$BUNDLE/Contents/MacOS/$EXECUTABLE"

mkdir -p "$ICONSET"
python3 scripts/render_app_icon.py --size 1024 --out "$ICONSET/icon_512x512@2x.png"
sips -z 16 16 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_16x16.png" >/dev/null
sips -z 32 32 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_16x16@2x.png" >/dev/null
sips -z 32 32 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_32x32.png" >/dev/null
sips -z 64 64 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_32x32@2x.png" >/dev/null
sips -z 128 128 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_128x128.png" >/dev/null
sips -z 256 256 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_128x128@2x.png" >/dev/null
sips -z 256 256 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_256x256.png" >/dev/null
sips -z 512 512 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_256x256@2x.png" >/dev/null
sips -z 512 512 "$ICONSET/icon_512x512@2x.png" --out "$ICONSET/icon_512x512.png" >/dev/null
iconutil -c icns "$ICONSET" -o "$BUNDLE/Contents/Resources/AppIcon.icns"

cat > "$BUNDLE/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleDevelopmentRegion</key>
  <string>zh_CN</string>
  <key>CFBundleIconFile</key>
  <string>AppIcon</string>
  <key>CFBundleExecutable</key>
  <string>asa-floating</string>
  <key>CFBundleIdentifier</key>
  <string>local.asa.floating</string>
  <key>CFBundleInfoDictionaryVersion</key>
  <string>6.0</string>
  <key>CFBundleName</key>
  <string>ASA</string>
  <key>CFBundlePackageType</key>
  <string>APPL</string>
  <key>CFBundleShortVersionString</key>
  <string>0.2.31</string>
  <key>CFBundleVersion</key>
  <string>53</string>
  <key>LSMinimumSystemVersion</key>
  <string>__DEPLOYMENT_TARGET__</string>
  <key>LSUIElement</key>
  <false/>
  <key>NSAppleEventsUsageDescription</key>
  <string>ASA 需要读取当前前台应用和窗口上下文，用于在用户唤起时辅助完成本机自动化任务。</string>
  <key>NSScreenCaptureUsageDescription</key>
  <string>ASA 需要在用户唤起时读取当前可见窗口截图，用于 OCR 识别当前聊天窗口文本。</string>
  <key>NSHighResolutionCapable</key>
  <true/>
</dict>
</plist>
PLIST
sed -i '' "s/__DEPLOYMENT_TARGET__/${DEPLOYMENT_TARGET}/g" "$BUNDLE/Contents/Info.plist"

find "$BUNDLE" -name '*.cstemp' -delete

sign_with_timeout() {
  codesign --force --deep --sign "$SIGN_IDENTITY" "$BUNDLE" >/dev/null &
  local sign_pid=$!
  local attempts=0
  local max_attempts=$((SIGNING_TIMEOUT_SECONDS * 10))
  while kill -0 "$sign_pid" 2>/dev/null; do
    if (( attempts >= max_attempts )); then
      kill "$sign_pid" 2>/dev/null || true
      wait "$sign_pid" 2>/dev/null || true
      return 1
    fi
    sleep 0.1
    ((attempts += 1))
  done
  wait "$sign_pid"
}

discard_unsigned_bundle() {
  find "$BUNDLE" -name '*.cstemp' -delete 2>/dev/null || true
  rm -rf "$BUNDLE/Contents/_CodeSignature" "$BUNDLE"
}

case "$SIGNING_MODE" in
  stable)
    if ! security find-identity -v -p codesigning | grep "$SIGN_IDENTITY" >/dev/null; then
      echo "Missing stable signing identity: $SIGN_IDENTITY" >&2
      discard_unsigned_bundle
      exit 1
    fi
    if ! sign_with_timeout; then
      echo "Stable signing failed or timed out; unlock the signing key and rerun." >&2
      discard_unsigned_bundle
      exit 1
    fi
    ;;
  adhoc)
    echo "Warning: explicit ad-hoc signing; TCC permissions may not persist." >&2
    codesign --force --deep --sign - "$BUNDLE" >/dev/null
    ;;
  *)
    echo "ASA_SIGNING_MODE must be stable or adhoc" >&2
    exit 2
    ;;
esac
codesign --verify --deep --strict "$BUNDLE"

mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_STAGING" "$PREVIOUS_BUNDLE"
ditto "$BUNDLE" "$INSTALL_STAGING"
codesign --verify --deep --strict "$INSTALL_STAGING"

if [[ -e "$INSTALL_BUNDLE" ]]; then
  mv "$INSTALL_BUNDLE" "$PREVIOUS_BUNDLE"
fi
if ! mv "$INSTALL_STAGING" "$INSTALL_BUNDLE"; then
  if [[ -e "$PREVIOUS_BUNDLE" ]]; then
    mv "$PREVIOUS_BUNDLE" "$INSTALL_BUNDLE"
  fi
  exit 1
fi

# Keep build and rollback directories from being discovered as extra applications.
rm -rf "$BUNDLE"
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -u "$PREVIOUS_BUNDLE" >/dev/null 2>&1 || true
/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister \
  -f "$INSTALL_BUNDLE" >/dev/null 2>&1 || true

echo "$INSTALL_BUNDLE"
