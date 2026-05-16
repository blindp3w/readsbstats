import type { ButtonHTMLAttributes } from 'react';
import { cn } from '@/lib/cn';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger';
export type ButtonSize = 'sm' | 'md';

interface Props extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

const variantClass: Record<ButtonVariant, string> = {
  primary:
    'bg-[var(--color-accent)] text-white shadow-[var(--shadow-sm)] hover:bg-[var(--color-accent-hover)] active:translate-y-px focus-visible:ring-[var(--color-accent)]',
  secondary:
    'border border-[var(--color-border-default)] bg-[var(--color-surface)]/60 text-[var(--color-text)] hover:bg-[var(--color-surface-2)] hover:border-[var(--color-border-strong)] focus-visible:ring-[var(--color-accent)]',
  ghost:
    'text-[var(--color-text)] hover:bg-[var(--color-surface-2)] focus-visible:ring-[var(--color-accent)]',
  danger:
    'border border-[var(--color-danger)]/70 text-[var(--color-danger)] hover:bg-[var(--color-danger)]/10 focus-visible:ring-[var(--color-danger)]',
};

const sizeClass: Record<ButtonSize, string> = {
  sm: 'px-2 py-1 text-xs min-h-[32px]',
  md: 'px-3 py-1.5 text-sm min-h-[40px]',
};

// Exposed so non-button elements (e.g. anchor used as a download link) can
// match the Button's metrics exactly without nesting <button> in <a>.
export function buttonClass(
  variant: ButtonVariant = 'primary',
  size: ButtonSize = 'md',
  extra?: string,
): string {
  return cn(
    'inline-flex items-center justify-center gap-1.5 rounded font-medium transition-colors',
    'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-1 focus-visible:ring-offset-[var(--color-bg)]',
    'disabled:cursor-not-allowed disabled:opacity-50',
    variantClass[variant],
    sizeClass[size],
    extra,
  );
}

// Minimum height keys to mobile touch-target guidance (44px Apple HIG).
// On mobile-first we err toward the larger end; the `sm` variant is for
// inline contexts (table action cells) where compactness matters more.
export function Button({
  variant = 'primary',
  size = 'md',
  className,
  type = 'button',
  ...props
}: Props) {
  return (
    <button
      // eslint-disable-next-line react/button-has-type
      type={type}
      className={buttonClass(variant, size, className)}
      {...props}
    />
  );
}
