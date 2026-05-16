import { Button } from '@/components/ui/Button';

interface Props {
  total: number;
  limit: number;
  offset: number;
  onOffsetChange: (next: number) => void;
}

// Same shape as the v1 renderPagination() helper in table-utils.js.
// Touch targets are 40px+ via Button size='sm'; "Prev"/"Next" mean
// previous/next page of `limit`.
export function Pagination({ total, limit, offset, onOffsetChange }: Props) {
  const page = Math.floor(offset / limit) + 1;
  const pageCount = Math.max(1, Math.ceil(total / limit));
  const canPrev = offset > 0;
  const canNext = offset + limit < total;

  return (
    <div
      className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-[var(--color-text-dim)]"
      data-testid="pagination"
    >
      <div className="tabnum">
        Showing {Math.min(offset + 1, total)}–{Math.min(offset + limit, total)} of{' '}
        {total.toLocaleString()}
      </div>
      <div className="flex items-center gap-1.5">
        <Button
          size="sm"
          variant="secondary"
          disabled={!canPrev}
          onClick={() => onOffsetChange(Math.max(0, offset - limit))}
          data-testid="pagination-prev"
        >
          ‹ Prev
        </Button>
        <span className="tabnum">
          {page} / {pageCount}
        </span>
        <Button
          size="sm"
          variant="secondary"
          disabled={!canNext}
          onClick={() => onOffsetChange(offset + limit)}
          data-testid="pagination-next"
        >
          Next ›
        </Button>
      </div>
    </div>
  );
}
