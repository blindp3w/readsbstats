// Audit-13 Phase 6: Pagination off-by-one regression locks.
//
// The component does the arithmetic for "Showing X–Y of Z" and the
// disabled state of Prev/Next. The audit flagged off-by-one risk
// because the math is hand-rolled (`offset + 1` for X, `offset + limit`
// clamped to total for Y, `page = floor(offset/limit) + 1`, etc.).
// These tests pin the boundary conditions: zero results, exact-page
// boundaries, last partial page.

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { Pagination } from '@/components/Pagination';

function renderPagination(props: {
  total: number;
  limit: number;
  offset: number;
}) {
  return render(
    <Pagination total={props.total} limit={props.limit} offset={props.offset} onOffsetChange={() => {}} />,
  );
}

describe('Pagination — off-by-one boundaries', () => {
  it('zero results → "Showing 0–0 of 0", Prev/Next both disabled', () => {
    renderPagination({ total: 0, limit: 25, offset: 0 });
    // Math.min(offset + 1, total) = Math.min(1, 0) = 0.
    // Math.min(offset + limit, total) = Math.min(25, 0) = 0.
    expect(screen.getByTestId('pagination').textContent).toMatch(/Showing 0[–-]0 of 0/);
    expect(screen.getByTestId('pagination-prev')).toBeDisabled();
    expect(screen.getByTestId('pagination-next')).toBeDisabled();
  });

  it('first page exactly full → "Showing 1–25 of 100", page 1/4', () => {
    renderPagination({ total: 100, limit: 25, offset: 0 });
    expect(screen.getByTestId('pagination').textContent).toMatch(/Showing 1[–-]25 of 100/);
    expect(screen.getByTestId('pagination').textContent).toMatch(/1 \/ 4/);
    expect(screen.getByTestId('pagination-prev')).toBeDisabled();
    expect(screen.getByTestId('pagination-next')).not.toBeDisabled();
  });

  it('middle page exactly aligned → "Showing 26–50 of 100", page 2/4', () => {
    renderPagination({ total: 100, limit: 25, offset: 25 });
    expect(screen.getByTestId('pagination').textContent).toMatch(/Showing 26[–-]50 of 100/);
    expect(screen.getByTestId('pagination').textContent).toMatch(/2 \/ 4/);
    expect(screen.getByTestId('pagination-prev')).not.toBeDisabled();
    expect(screen.getByTestId('pagination-next')).not.toBeDisabled();
  });

  it('last page exactly full → Next disabled, no off-by-one', () => {
    renderPagination({ total: 100, limit: 25, offset: 75 });
    expect(screen.getByTestId('pagination').textContent).toMatch(/Showing 76[–-]100 of 100/);
    expect(screen.getByTestId('pagination').textContent).toMatch(/4 \/ 4/);
    expect(screen.getByTestId('pagination-next')).toBeDisabled();
  });

  it('last page partial → "Showing 26–37 of 37", Next disabled', () => {
    // 37 total, limit 25, page 2 has 12 rows (offset 25..37).
    renderPagination({ total: 37, limit: 25, offset: 25 });
    expect(screen.getByTestId('pagination').textContent).toMatch(/Showing 26[–-]37 of 37/);
    expect(screen.getByTestId('pagination').textContent).toMatch(/2 \/ 2/);
    expect(screen.getByTestId('pagination-next')).toBeDisabled();
  });

  it('single page partial → page 1/1, both buttons disabled', () => {
    renderPagination({ total: 12, limit: 25, offset: 0 });
    expect(screen.getByTestId('pagination').textContent).toMatch(/Showing 1[–-]12 of 12/);
    expect(screen.getByTestId('pagination').textContent).toMatch(/1 \/ 1/);
    expect(screen.getByTestId('pagination-prev')).toBeDisabled();
    expect(screen.getByTestId('pagination-next')).toBeDisabled();
  });

  it('total == limit → exactly 1 page, no Next', () => {
    renderPagination({ total: 25, limit: 25, offset: 0 });
    expect(screen.getByTestId('pagination').textContent).toMatch(/Showing 1[–-]25 of 25/);
    expect(screen.getByTestId('pagination').textContent).toMatch(/1 \/ 1/);
    expect(screen.getByTestId('pagination-next')).toBeDisabled();
  });

  it('total == limit + 1 → 2 pages, second has 1 row', () => {
    renderPagination({ total: 26, limit: 25, offset: 25 });
    expect(screen.getByTestId('pagination').textContent).toMatch(/Showing 26[–-]26 of 26/);
    expect(screen.getByTestId('pagination').textContent).toMatch(/2 \/ 2/);
    expect(screen.getByTestId('pagination-next')).toBeDisabled();
  });

  it('pageCount floors to 1 even for total=0 (avoids "1/0")', () => {
    renderPagination({ total: 0, limit: 25, offset: 0 });
    // pageCount = Math.max(1, Math.ceil(0/25)) = Math.max(1, 0) = 1.
    expect(screen.getByTestId('pagination').textContent).toMatch(/1 \/ 1/);
  });
});

describe('Pagination — onOffsetChange handler math', () => {
  it('Prev offsets back by limit, clamped at 0', () => {
    let received = -1;
    const { rerender } = render(
      <Pagination total={100} limit={25} offset={25} onOffsetChange={(n) => (received = n)} />,
    );
    screen.getByTestId('pagination-prev').click();
    expect(received).toBe(0);

    // From offset 5 (smaller than limit), Prev should clamp to 0 not go negative.
    rerender(
      <Pagination total={100} limit={25} offset={5} onOffsetChange={(n) => (received = n)} />,
    );
    screen.getByTestId('pagination-prev').click();
    expect(received).toBe(0);
  });

  it('Next offsets forward by limit', () => {
    let received = -1;
    render(
      <Pagination total={100} limit={25} offset={0} onOffsetChange={(n) => (received = n)} />,
    );
    screen.getByTestId('pagination-next').click();
    expect(received).toBe(25);
  });
});
