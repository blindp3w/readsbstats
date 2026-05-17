import type { InputHTMLAttributes, SelectHTMLAttributes } from 'react';
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

// Audit-12 — renamed from `Select` to `NativeSelect` to disambiguate from
// the Radix `Select` primitive in `@/components/ui/Select`. Same DOM shape
// (plain <select>); the Radix variant is the styled-dropdown one.
export function NativeSelect({ className, ...props }: SelectHTMLAttributes<HTMLSelectElement>) {
  return <select className={cn(fieldClass, className)} {...props} />;
}
