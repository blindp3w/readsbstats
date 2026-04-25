/* Shared loader: reads a JS file from static/js/ and evaluates it inside a
   sandboxed VM context so its top-level functions become callable from tests.

   The source files are loaded as plain <script> tags in the browser and rely
   on browser globals (localStorage, document, fetch). We stub the ones we need
   per test; functions that touch unstubbed globals will throw if invoked. */

import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import vm from "node:vm";

const HERE = dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = resolve(HERE, "..", "..");

export function loadJs(relPath, contextStubs = {}) {
  const fullPath = resolve(REPO_ROOT, relPath);
  const source = readFileSync(fullPath, "utf8");
  const ctx = vm.createContext(contextStubs);
  vm.runInContext(source, ctx, { filename: fullPath });
  return ctx;
}

/* In-memory localStorage shim: enough for getUnits()/setUnits() without a
   real browser. */
export function makeLocalStorage(initial = {}) {
  const store = { ...initial };
  return {
    getItem: (k) => (k in store ? store[k] : null),
    setItem: (k, v) => { store[k] = String(v); },
    removeItem: (k) => { delete store[k]; },
    clear: () => { for (const k of Object.keys(store)) delete store[k]; },
  };
}
