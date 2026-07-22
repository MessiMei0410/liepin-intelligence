import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import prettier from 'eslint-config-prettier';

// R1 基线：最小规则集，存量违例一律 warn 不阻断 CI；
// R5 已落地：src/** 新增代码 no-explicit-any=error，main.tsx 存量 any 保持 warn 至 R4 拆分收口。
export default tseslint.config(
  {
    ignores: ['dist/', 'node_modules/', 'opencli/', 'work/', 'experiments/', 'src/generated/'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['src/**/*.{ts,tsx}'],
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      '@typescript-eslint/no-explicit-any': 'error',
      // 存量巨石组件的架构性违例，R4 拆分后逐步收紧回 error
      'react-hooks/immutability': 'warn',
      'react-hooks/purity': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
    },
  },
  {
    // main.tsx 存量 any（recordValue 逃逸舱）在 R4 拆分收口前保持 warn
    files: ['src/main.tsx'],
    rules: { '@typescript-eslint/no-explicit-any': 'warn' },
  },
  {
    // R4 verbatim 搬运豁免，拆分收口后收紧：搬运代码含存量 any 与 hooks 存量违例，保持 warn
    files: ['src/shared/*.{ts,tsx}', 'src/workflows/utils.ts', 'src/pages/*.tsx', 'src/panels/*.tsx', 'src/copilot/bridge.ts'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'warn',
      'react-hooks/immutability': 'warn',
      'react-hooks/purity': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
    },
  },
  {
    // Node 脚本（CI 工具）的最小全局声明，避免 no-undef 误报
    files: ['scripts/**/*.mjs'],
    languageOptions: {
      globals: { process: 'readonly', console: 'readonly', fetch: 'readonly', AbortSignal: 'readonly', URL: 'readonly' },
    },
  },
  prettier,
);
