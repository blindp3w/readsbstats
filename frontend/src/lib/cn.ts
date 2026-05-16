// Minimal class-name concat — drops falsy values, dedupes whitespace.
// Avoid pulling clsx/tailwind-merge here; the surface is small.
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ').trim();
}
