# Resume Scraper Chrome Extensions

Analysis of existing Chrome extensions that scrape resumes from recruitment sites.
Useful reference for understanding how professional tools handle resume capture.

## E简历 (Puxin Recruiting System Plugin)

- **Name**: E简历 v2.0.4
- **Publisher**: 璞心招聘系统
- **Type**: Chrome Extension Manifest V3
- **Purpose**: One-click resume import from recruitment/social sites into Puxin ATS

### Architecture

```
Recruitment site page → content.js (UI overlay + HTML capture)
                              ↓
                     background.js (Service Worker)
                              ↓
                     {HOST}/handler.aspx (Puxin backend)
```

### Key Capabilities

1. **URL pattern matching**: Fetches supported site regex list from `cdn.fplusats.com/plugin/resume_url.json`
   dynamically. Covers Liepin, BOSS直聘, and other major Chinese recruitment platforms.

2. **HTML full capture**: `content.js` captures entire page HTML, strips `<script>` tags,
   submits to backend for parsing. Uses `chrome.storage` for config persistence.

3. **Screenshot capture**: Supports both `chrome.tabs.captureVisibleTab` (viewport PNG) 
   and `html2canvas` (full-page rendering). Canvas cropping for targeted areas.

4. **Token auth**: 2-hour expiry token pool with auto-refresh. Uses FormData POST with
   `token` + `certificate` fields.

5. **Duplicate detection**: Pre-submission check via `/handler.aspx` (mode=0, action=-42).

### API Endpoints

| Use | Endpoint | Method | Params |
|-----|----------|--------|--------|
| URL rules | `cdn.fplusats.com/plugin/resume_url.json` | GET | — |
| Login | `{HOST}/handler.aspx` | POST | mode=0, action=41 |
| Dup check | `{HOST}/handler.aspx` | POST | mode=0, action=-42 |
| Create resume | `{HOST}/handler.aspx` | POST | mode=35, action=-42, Person JSON + HTML |
| Auto grab | `{oSystem.resumeapi}/api/resume/browseresume` | POST | sHTML + sWebUrl |

### Relevance to Hermes Workflow

- **Proof that HTML-level resume capture from Liepin is viable** — the plugin successfully
  captures and structures resume data from the same sites we target.
- **URL pattern list** (`resume_url.json`) could be fetched to understand Liepin's page
  URL structure for better screenshot targeting.
- **html2canvas approach** is an alternative to `screencapture -x` for full-page captures
  when viewport screenshots miss data below the fold.
- **Limitation**: Requires Puxin backend to parse HTML into structured data. Cannot be
  used standalone without the Puxin system.
