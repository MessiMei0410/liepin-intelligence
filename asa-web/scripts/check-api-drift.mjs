#!/usr/bin/env node
// R5 契约漂移检查：Core (8765) 的 openapi.json 重新生成后与 src/generated/api.d.ts 逐字节比对。
// 不一致 → 退出码 1（提示运行 npm run generate:api）；Core 不可达 → 打印警告并退出码 0，
// 因为本仓库无托管 CI，本地起不起 Core 都不应误伤 lint/test/build 链路。
import { execFileSync } from 'node:child_process'
import { mkdtempSync, readFileSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join } from 'node:path'
import { createRequire } from 'node:module'

const CORE_URL = process.env.ASA_CORE_URL || 'http://127.0.0.1:8765'

let spec
try {
  const response = await fetch(`${CORE_URL}/openapi.json`, { signal: AbortSignal.timeout(5000) })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  spec = await response.text()
} catch (error) {
  console.warn(`[check-api-drift] Core 不可达（${CORE_URL}），跳过漂移检查：${error instanceof Error ? error.message : String(error)}`)
  process.exit(0)
}

const dir = mkdtempSync(join(tmpdir(), 'asa-api-drift-'))
const specPath = join(dir, 'openapi.json')
const regeneratedPath = join(dir, 'api.d.ts')
writeFileSync(specPath, spec)

const require = createRequire(import.meta.url)
const packageJsonPath = require.resolve('openapi-typescript/package.json')
const { bin } = require('openapi-typescript/package.json')
const cliPath = join(dirname(packageJsonPath), typeof bin === 'string' ? bin : bin['openapi-typescript'])
// 与 npm run generate:api 同一 CLI 二进制，仅从临时文件读 spec，保证输出可比。
execFileSync(process.execPath, [cliPath, specPath, '-o', regeneratedPath], { stdio: 'pipe' })

const regenerated = readFileSync(regeneratedPath, 'utf8')
const current = readFileSync(new URL('../src/generated/api.d.ts', import.meta.url), 'utf8')
if (regenerated !== current) {
  console.error('[check-api-drift] src/generated/api.d.ts 与 Core openapi.json 已漂移。请运行 npm run generate:api 重新生成并提交。')
  process.exit(1)
}
console.log('[check-api-drift] src/generated/api.d.ts 与 Core 契约一致。')
