import eslint from '@eslint/js'
import prettier from 'eslint-config-prettier'
import globals from 'globals'
import tseslint from 'typescript-eslint'

// 本仓库风格约定:格式交给 Prettier(.prettierrc),ESLint 负责代码质量并统一大括号风格;
// eslint-config-prettier 需在 curly 之前声明,否则 curly 会被其静默关闭。
export default tseslint.config(
  {
    ignores: ['node_modules/', '.web-build/', '.venv/', 'data/', 'apps/web/vendor/', 'docs/'],
  },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  prettier,
  {
    files: ['**/*.{js,mjs,cjs,ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      sourceType: 'module',
      globals: {
        ...globals.node,
      },
    },
    rules: {
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', caughtErrorsIgnorePattern: '^_' },
      ],
      curly: ['error', 'all'],
    },
  },
  {
    files: ['apps/web/**/*.{js,ts,tsx}'],
    languageOptions: {
      globals: {
        ...globals.browser,
      },
    },
    rules: {
      'no-restricted-imports': [
        'error',
        {
          patterns: [
            {
              group: ['konva', 'konva/*', 'react-konva'],
              message: 'Konva 只能由 canvas/renderer-konva 渲染适配器导入。',
            },
          ],
        },
      ],
    },
  },
  {
    files: ['apps/web/canvas/renderer-konva/**/*.{ts,tsx}'],
    rules: {
      'no-restricted-imports': 'off',
    },
  },
)
