import path from 'node:path'
import { fileURLToPath } from 'node:url'

// E2E 环境常量：隔离 Core 一律打 127.0.0.1:8876 + /tmp 下的新鲜 DB 副本，
// 正式 Core（8765）与正式 v3 库绝不作为目标，正式库仅作只读复制来源。
const here = path.dirname(fileURLToPath(import.meta.url))

export const REPO_ROOT = path.resolve(here, '..', '..')
export const REPO_DIST = path.join(REPO_ROOT, 'dist')

export const BACKEND_SCRIPTS = '/Users/messi/Documents/Codex/2026-06-18/liepin-intelligence/scripts'
export const BACKEND_APP = path.join(BACKEND_SCRIPTS, 'asa_core', 'app.py')
export const PRODUCTION_DB = '/Users/messi/Documents/Codex/2026-06-26/re/outputs/talent_system_v3_20260629.db'
export const PYTHON = '/usr/local/bin/python3'

export const E2E_PORT = 8876
export const E2E_BASE_URL = `http://127.0.0.1:${E2E_PORT}`
export const TMP_PREFIX = 'asa-e2e-'
export const STATE_FILE = '/tmp/asa-e2e-runtime.json'
