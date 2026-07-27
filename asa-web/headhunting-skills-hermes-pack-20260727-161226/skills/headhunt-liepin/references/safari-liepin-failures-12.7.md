# macOS 12.7 Safari + 猎聘 Interaction Failures

Verified on 2026-06-02, macOS 12.7.6, Safari.

## Failure 1: pbcopy + Cmd+V paste not triggering search

**Symptom**: After `pbcopy` + `Cmd+V` + `Return`, the URL stays at `h.liepin.com/search/getConditionItem`. Search never triggers.

**Attempted fixes (all failed)**:
- Added `delay 0.3` between paste and return
- Removed `Cmd+A` before `Cmd+V` (box should be empty)
- Tried `key code 36` instead of `keystroke return`
- Tried navigating to `h.liepin.com` homepage first (vs. `getConditionItem`)
- User re-clicked search box between attempts

**Root cause hypothesis**: The Liepin search box on the condition search page is likely a custom React component, not a native `<input>`. AppleScript `keystroke` may not trigger the React synthetic event handler, or `Cmd+V` doesn't fire the `onChange`/`onInput` handler that the search component relies on. Focus timing between user click and osascript execution may also play a role.

## Failure 2: URL parameter search returns 404

**Command**: 
```bash
osascript -e 'tell application "Safari" to set URL of current tab of window 1 to "https://h.liepin.com/search?key=%E5%85%89%E5%AD%A6%E4%BA%A7%E5%93%81%E7%BB%8F%E7%90%86"'
```

**Result**: 404 page. Liepin no longer supports direct URL parameter search (`?key=`).

## Working Path

Only `computer_use` tool (with cua-driver + AX accessibility) can reliably interact with Liepin's search components. When `computer_use` is not available in the current session, **do not attempt osascript workarounds** — go directly to delegation (send strategy to 二号机 in Feishu group).
