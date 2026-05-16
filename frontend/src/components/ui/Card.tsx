import type { HTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

// Minimal Card primitives. Inspired by shadcn/ui; rewritten by hand to avoid
// shadcn CLI's Tailwind v3-centric defaults and keep the surface tiny.

export function Card({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        'rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface)]',
        'shadow-[var(--shadow-sm)]',
        className,
      )}
      {...props}
    />
  );
}

export function CardHeader({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('px-4 pt-4 pb-2', className)} {...props} />;
}

export function CardTitle({ className, ...props }: HTMLAttributes<HTMLHeadingElement>) {
  return <h2 className={cn('text-base font-semibold', className)} {...props} />;
}

export function CardDescription({ className, ...props }: HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p className={cn('text-sm text-[var(--color-text-dim)]', className)} {...props} />
  );
}

export function CardContent({ className, ...props }: HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('px-4 pb-4', className)} {...props} />;
}
