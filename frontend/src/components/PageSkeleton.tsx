// Placeholder shown while a lazy-loaded route chunk is fetching. Real per-page
// skeletons (shadcn/ui Skeleton) replace this once the page component renders.
export function PageSkeleton() {
  return (
    <div
      role="status"
      aria-live="polite"
      className="m-6 animate-pulse text-sm text-[var(--color-text-dim)]"
    >
      Loading…
    </div>
  );
}
