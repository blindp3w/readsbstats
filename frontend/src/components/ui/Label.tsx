import type { LabelHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

export function Label({ className, ...props }: LabelHTMLAttributes<HTMLLabelElement>) {
  return (
    <label
      className={cn('block text-xs font-medium uppercase tracking-wide text-[var(--color-text-dim)]', className)}
      {...props}
    />
  );
}
