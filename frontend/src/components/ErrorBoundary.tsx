import { Component, type ErrorInfo, type ReactNode } from 'react';

interface Props {
  children: ReactNode;
  fallback?: (error: Error, reset: () => void) => ReactNode;
}

interface State {
  error: Error | null;
}

// App-level error boundary. Without this, a single render exception in any
// child (e.g. a Recharts crash on malformed data) takes down the whole SPA
// and the user sees a blank page. Per-route errorElement covers route-scoped
// failures; this catches everything above the router (and the router itself).
export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null };

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    // Console is the right destination — service-worker/observability can
    // pick this up later. Don't toast — toasts disappear; a boundary is the
    // user-facing surface.
    console.error('[ErrorBoundary]', error, info);
  }

  reset = (): void => this.setState({ error: null });

  render() {
    if (this.state.error) {
      if (this.props.fallback) return this.props.fallback(this.state.error, this.reset);
      return (
        <div
          role="alert"
          className="m-4 rounded border border-[var(--color-danger)] bg-[var(--color-surface)] p-4"
        >
          <h2 className="mb-2 text-lg font-semibold text-[var(--color-danger)]">
            Something went wrong
          </h2>
          <p className="mb-3 text-sm text-[var(--color-text-dim)]">{this.state.error.message}</p>
          <button
            type="button"
            onClick={this.reset}
            className="rounded bg-[var(--color-accent)] px-3 py-1 text-sm text-white hover:opacity-90"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
