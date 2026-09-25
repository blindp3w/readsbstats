// MapLibre 6 is ESM-only and resolves its tile-decoder worker at runtime via
// `new URL('./maplibre-gl-worker.mjs', import.meta.url)`. Inside a Vite bundle
// that points at a file Vite never emits (404 → blank map), so the worker must
// be routed through Vite's worker pipeline and registered once, before the
// first Map is constructed. Side-effect module: import it from every map
// component. See maplibre-gl docs/index.md "Installation → Vite".
import { setWorkerUrl } from 'maplibre-gl';
import workerUrl from 'maplibre-gl/dist/maplibre-gl-worker.mjs?worker&url';

setWorkerUrl(workerUrl);
