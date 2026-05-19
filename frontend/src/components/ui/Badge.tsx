import type { HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

interface Props extends HTMLAttributes<HTMLSpanElement> {
  variant?: 'default' | 'success' | 'warn' | 'danger' | 'muted';
}

const variantClass: Record<NonNullable<Props['variant']>, string> = {
  default: 'border border-[var(--color-border-default)] text-[var(--color-text-dim)]',
  success: 'border border-[var(--color-success)] text-[var(--color-success)]',
  warn: 'border border-[var(--color-warn)] text-[var(--color-warn)]',
  danger: 'border border-[var(--color-danger)] text-[var(--color-danger)]',
  muted: 'border border-[var(--color-border-default)] text-[var(--color-text-dim)]',
};

export function Badge({ variant = 'default', className, ...props }: Props) {
  return (
    <span
      className={cn(
        // whitespace-nowrap so e.g. "ADS-B" can never break at the hyphen
        // in a narrow column on mobile.
        'inline-flex items-center gap-1 whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium tabular-nums',
        variantClass[variant],
        className,
      )}
      {...props}
    />
  );
}
