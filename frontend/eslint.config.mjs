import js from '@eslint/js';
import globals from 'globals';
import reactHooks from 'eslint-plugin-react-hooks';
import reactRefresh from 'eslint-plugin-react-refresh';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'node_modules'] },
  {
    extends: [js.configs.recommended, ...tseslint.configs.recommended],
    files: ['**/*.{ts,tsx}'],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react-refresh': reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      'react-refresh/only-export-components': ['warn', { allowConstantExport: true }],
      // Note: `react/no-danger` lives in `eslint-plugin-react` which we
      // deliberately don't load here (keeps the eslint dep surface tight).
      // The actual guardrail against `dangerouslySetInnerHTML` is the Vitest
      // test in `frontend/test/no-danger.test.ts` (audit-12 #174).
      '@typescript-eslint/no-unused-vars': ['warn', { argsIgnorePattern: '^_' }],
    },
  },
  {
    // Audit-15: test fixtures override ECharts and other library internals
    // whose shapes are too deeply nested to model usefully in test code.
    // Allowing `any` here keeps the production rule strict (zero `any` in
    // src/) while the test suite stays readable. If you find yourself
    // reaching for `any` in src/, find a real type or define a local one
    // — don't widen this allowlist.
    files: ['test/**/*.{ts,tsx}'],
    rules: {
      '@typescript-eslint/no-explicit-any': 'off',
    },
  },
  {
    // Audit-15: the shadcn-style Radix-wrapper UI primitives intentionally
    // re-export multiple parts of a single component family from one file
    // (`Popover` + `PopoverTrigger` + `PopoverContent` + `PopoverClose` etc.).
    // The `react-refresh/only-export-components` rule flags the `export
    // const X = RadixPart.Y` re-exports as non-component constants because
    // TypeScript can't statically prove they are React components.
    // Fast-refresh on these primitives works in practice (Vite recognises
    // the re-exports at module-graph time) and the project's
    // frontend/CLAUDE.md documents this as the canonical UI-primitive
    // pattern. Disabling the rule here is consistent; do not extend this
    // override to page or feature components — those keep the rule.
    files: ['src/components/ui/**/*.{ts,tsx}'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
  {
    // Entry file — there's nothing to fast-refresh; the bootstrap creates
    // the root and never re-renders itself.
    files: ['src/main.tsx'],
    rules: {
      'react-refresh/only-export-components': 'off',
    },
  },
);
