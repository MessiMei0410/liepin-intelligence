#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

APP_NAME="ASA"
BUNDLE="$PWD/build/${APP_NAME}.app"
EXECUTABLE="asa-floating"
ICONSET="$PWD/build/AppIcon.iconset"
SIGN_IDENTITY="ASA Floating Local Code Signing"

rm -rf "$BUNDLE" build "$EXECUTABLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

swiftc \
  -o "$EXECUTABLE" \
  src/main.swift \
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
  <string>0.2.18</string>
  <key>CFBundleVersion</key>
  <string>41</string>
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

find "$BUNDLE" -name '*.cstemp' -delete

sign_with_timeout() {
  codesign --force --deep --sign "$SIGN_IDENTITY" "$BUNDLE" >/dev/null &
  local sign_pid=$!
  local attempts=0
  while kill -0 "$sign_pid" 2>/dev/null; do
    if (( attempts >= 100 )); then
      kill "$sign_pid" 2>/dev/null || true
      wait "$sign_pid" 2>/dev/null || true
      return 1
    fi
    sleep 0.1
    ((attempts += 1))
  done
  wait "$sign_pid"
}

# Preserve the stable local identity for TCC permissions, but never let a
# keychain approval prompt block an unattended build indefinitely.
if security find-identity -v -p codesigning | grep "$SIGN_IDENTITY" >/dev/null \
  && sign_with_timeout; then
  :
else
  find "$BUNDLE" -name '*.cstemp' -delete
  rm -rf "$BUNDLE/Contents/_CodeSignature"
  codesign --force --deep --sign - "$BUNDLE" >/dev/null
fi
codesign --verify --deep --strict "$BUNDLE"

echo "$BUNDLE"
