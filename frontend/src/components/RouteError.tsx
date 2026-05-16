import { isRouteErrorResponse, useRouteError, Link } from 'react-router-dom';

// Per-route error element (route option `errorElement`). React Router will
// catch render exceptions thrown anywhere under the route's element tree and
// render this in place of the broken page.
export function RouteError() {
  const error = useRouteError();

  let title = 'Page failed to load';
  let detail = '';

  if (isRouteErrorResponse(error)) {
    title = `${error.status} ${error.statusText}`;
    detail = typeof error.data === 'string' ? error.data : '';
  } else if (error instanceof Error) {
    detail = error.message;
  }

  return (
    <div
      role="alert"
      className="m-4 rounded border border-[var(--color-danger)] bg-[var(--color-surface)] p-4"
    >
      <h2 className="mb-2 text-lg font-semibold text-[var(--color-danger)]">{title}</h2>
      {detail ? <p className="mb-3 text-sm text-[var(--color-text-dim)]">{detail}</p> : null}
      <Link
        to="/"
        className="inline-block rounded bg-[var(--color-accent)] px-3 py-1 text-sm text-white hover:opacity-90"
      >
        Back to statistics
      </Link>
    </div>
  );
}
