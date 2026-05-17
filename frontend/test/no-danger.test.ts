import { describe, it, expect } from 'vitest';

// Audit-12 #174 — `react/no-danger` is declared in eslint.config.mjs but
// `eslint-plugin-react` isn't loaded, so the lint rule silently does nothing.
// This Vitest grep is the actual guardrail: it fails the test run if any
// source file (excluding api.types.ts generated types) contains
// `dangerouslySetInnerHTML`, which is the XSS surface the rule was meant
// to block.

// Vite's import.meta.glob loads every matched file as a raw string at build
// time — no Node fs API needed, so this works inside the TS project tree
// without pulling in @types/node.
const sources = import.meta.glob('../src/**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

// api.types.ts is generated from the OpenAPI spec — exclude it so future
// schema changes can't trip the grep on an unrelated string match.
const EXCLUDE = /\/api\.types\.ts$/;

describe('no dangerouslySetInnerHTML in src/', () => {
  it('zero matches across all .ts/.tsx files', () => {
    const offenders: string[] = [];
    for (const [path, content] of Object.entries(sources)) {
      if (EXCLUDE.test(path)) continue;
      if (content.includes('dangerouslySetInnerHTML')) {
        offenders.push(path);
      }
    }
    expect(offenders, `dangerouslySetInnerHTML found in:\n${offenders.join('\n')}`).toEqual([]);
  });
});
