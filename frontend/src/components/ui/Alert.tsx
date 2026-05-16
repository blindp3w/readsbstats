import type { HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

interface Props extends HTMLAttributes<HTMLDivElement> {
  variant?: 'info' | 'warn' | 'error';
}

const variantClass: Record<NonNullable<Props['variant']>, string> = {
  info: 'border-[var(--color-accent)] text-[var(--color-text)]',
  warn: 'border-[var(--color-warn)] text-[var(--color-text)]',
  error: 'border-[var(--color-danger)] text-[var(--color-danger)]',
};

export function Alert({ variant = 'info', className, ...props }: Props) {
  return (
    <div
      role="alert"
      className={cn(
        'rounded border bg-[var(--color-surface)] px-3 py-2 text-sm',
        variantClass[variant],
        className,
      )}
      {...props}
    />
  );
}
