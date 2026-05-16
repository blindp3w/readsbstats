import * as TG from '@radix-ui/react-toggle-group';
import { forwardRef } from 'react';
import { cn } from '@/lib/cn';

// Single-select toggle group. Replaces the pill-style filter buttons on the
// gallery page. Accessible via Radix (arrow-key navigation, ARIA roles).

export const ToggleGroupRoot = forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof TG.Root>
>(({ className, ...props }, ref) => (
  <TG.Root
    ref={ref}
    className={cn(
      'inline-flex flex-wrap gap-0.5 rounded border border-[var(--color-border-default)] bg-[var(--color-surface)] p-0.5',
      className,
    )}
    {...props}
  />
));
ToggleGroupRoot.displayName = 'ToggleGroupRoot';

export const ToggleGroupItem = forwardRef<
  HTMLButtonElement,
  React.ComponentPropsWithoutRef<typeof TG.Item>
>(({ className, children, ...props }, ref) => (
  <TG.Item
    ref={ref}
    className={cn(
      'inline-flex items-center justify-center rounded px-3 text-xs font-medium transition-colors',
      // Mobile: 36px min-h for tap target; desktop: more compact (~28px).
      'min-h-[36px] min-w-[44px] md:min-h-[28px]',
      'text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)]',
      'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
      'data-[state=on]:bg-[var(--color-accent)] data-[state=on]:text-white',
      'data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50',
      className,
    )}
    {...props}
  >
    {children}
  </TG.Item>
));
ToggleGroupItem.displayName = 'ToggleGroupItem';
