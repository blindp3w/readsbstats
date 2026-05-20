import { describe, it, expect } from 'vitest';

// Audit-12 #174 — `react/no-danger` is declared in eslint.config.mjs but
// `eslint-plugin-react` isn't loaded, so the lint rule silently does nothing.
// This Vitest grep is the actual guardrail: it fails the test run if any
// source file contains `dangerouslySetInnerHTML`, which is the XSS surface
// the rule was meant to block.
//
// Audit-13 A13-078: `api.types.ts` was deleted (zero consumers across the
// SPA); the exclusion lives on as defence-in-depth in case anyone restores
// a generated-types pipeline in the future.

// Vite's import.meta.glob loads every matched file as a raw string at build
// time — no Node fs API needed, so this works inside the TS project tree
// without pulling in @types/node.
const sources = import.meta.glob('../src/**/*.{ts,tsx}', {
  query: '?raw',
  import: 'default',
  eager: true,
}) as Record<string, string>;

describe('no dangerouslySetInnerHTML in src/', () => {
  it('zero matches across all .ts/.tsx files', () => {
    const offenders: string[] = [];
    for (const [path, content] of Object.entries(sources)) {
      if (content.includes('dangerouslySetInnerHTML')) {
        offenders.push(path);
      }
    }
    expect(offenders, `dangerouslySetInnerHTML found in:\n${offenders.join('\n')}`).toEqual([]);
  });
});
