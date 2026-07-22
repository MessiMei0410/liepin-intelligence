import js from '@eslint/js';
import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';
import prettier from 'eslint-config-prettier';

// R1 基线：最小规则集，现有违例一律 warn 不阻断 CI；
// R5 阶段将把 no-explicit-any 对新增代码提为 error（存量豁免）。
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
      '@typescript-eslint/no-explicit-any': 'warn',
      // 存量巨石组件的架构性违例，R4 拆分后逐步收紧回 error
      'react-hooks/immutability': 'warn',
      'react-hooks/purity': 'warn',
      'react-hooks/set-state-in-effect': 'warn',
    },
  },
  prettier,
);
