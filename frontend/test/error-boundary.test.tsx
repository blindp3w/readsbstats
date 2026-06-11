// components/ErrorBoundary.tsx + RouteError.tsx — the two error surfaces.
//
// Contracts pinned here:
//  - a render exception in a child shows the fallback UI (never a blank page);
//  - "Try again" resets the boundary and re-renders the children;
//  - a custom fallback render-prop receives (error, reset);
//  - RouteError renders a thrown route error's message, and the status line
//    for a Response-style error, with the back link present.

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router-dom';
import { ErrorBoundary } from '@/components/ErrorBoundary';
import { RouteError } from '@/components/RouteError';

// React logs caught render errors via console.error — expected noise here.
beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
});
afterEach(() => {
  vi.restoreAllMocks();
});

function Bomb({ when }: { when: boolean }) {
  if (when) throw new Error('chart exploded');
  return <div data-testid="healthy-child">ok</div>;
}

describe('ErrorBoundary', () => {
  it('renders children while nothing throws', () => {
    render(
      <ErrorBoundary>
        <Bomb when={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId('healthy-child')).toBeInTheDocument();
  });

  it('shows the fallback with the error message when a child throws', () => {
    render(
      <ErrorBoundary>
        <Bomb when />
      </ErrorBoundary>,
    );
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('Something went wrong');
    expect(alert).toHaveTextContent('chart exploded');
  });

  it('"Try again" resets the boundary and re-renders the children', () => {
    const { rerender } = render(
      <ErrorBoundary>
        <Bomb when />
      </ErrorBoundary>,
    );
    expect(screen.getByRole('alert')).toHaveTextContent('chart exploded');

    // New children alone must NOT clear the boundary — the fallback stays
    // until the user explicitly resets.
    rerender(
      <ErrorBoundary>
        <Bomb when={false} />
      </ErrorBoundary>,
    );
    expect(screen.getByRole('alert')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Try again' }));
    expect(screen.getByTestId('healthy-child')).toBeInTheDocument();
  });

  it('uses a custom fallback render-prop when provided', () => {
    render(
      <ErrorBoundary
        fallback={(error, reset) => (
          <div data-testid="custom-fallback">
            {error.message}
            <button type="button" onClick={reset}>
              go
            </button>
          </div>
        )}
      >
        <Bomb when />
      </ErrorBoundary>,
    );
    expect(screen.getByTestId('custom-fallback')).toHaveTextContent('chart exploded');
  });
});

describe('RouteError', () => {
  function renderRoute(thrown: unknown) {
    const router = createMemoryRouter(
      [
        {
          path: '/',
          element: <div />,
          errorElement: <RouteError />,
          loader: () => {
            throw thrown;
          },
        },
      ],
      { initialEntries: ['/'] },
    );
    return render(<RouterProvider router={router} />);
  }

  it('shows the message of a thrown Error with the default title', async () => {
    renderRoute(new Error('loader blew up'));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('Page failed to load');
    expect(alert).toHaveTextContent('loader blew up');
    expect(screen.getByRole('link', { name: 'Back to statistics' })).toBeInTheDocument();
  });

  it('shows status + statusText for a Response-style route error', async () => {
    renderRoute(
      new Response('flight not found', { status: 404, statusText: 'Not Found' }),
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('404 Not Found');
  });
});
