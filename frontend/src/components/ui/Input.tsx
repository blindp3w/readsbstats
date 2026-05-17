import type { InputHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

const fieldClass = cn(
  'w-full rounded border border-[var(--color-border-default)] bg-[var(--color-bg)] px-3 py-2 text-sm',
  'text-[var(--color-text)] placeholder:text-[var(--color-text-dim)]',
  'focus:border-[var(--color-accent)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]',
  'disabled:cursor-not-allowed disabled:opacity-50',
);

export function Input({ className, ...props }: InputHTMLAttributes<HTMLInputElement>) {
  return <input className={cn(fieldClass, className)} {...props} />;
}
