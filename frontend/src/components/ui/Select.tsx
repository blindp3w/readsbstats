import * as Sel from '@radix-ui/react-select';
import { forwardRef, type ReactNode } from 'react';
import { cn } from '@/lib/cn';

// Radix Select with shadcn-flavoured styling tuned to our dark theme.
// Replaces native <select> in places that need a polished pop-over (nav
// units selector, history filters). Native <select> is still fine for
// short inline filter rows where the platform UI is preferable on mobile.

export const Select = Sel.Root;
export const SelectValue = Sel.Value;

interface TriggerProps extends React.ComponentPropsWithoutRef<typeof Sel.Trigger> {
  className?: string;
}

export const SelectTrigger = forwardRef<HTMLButtonElement, TriggerProps>(
  ({ className, children, ...props }, ref) => (
    <Sel.Trigger
      ref={ref}
      className={cn(
        'inline-flex w-full items-center justify-between gap-2 rounded border px-3 py-1.5 text-sm',
        'border-[var(--color-border-default)] bg-[var(--color-bg)] text-[var(--color-text)]',
        'min-h-[40px]',
        'transition-colors hover:border-[var(--color-text-dim)] data-[state=open]:border-[var(--color-accent)]',
        'focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]',
        'disabled:cursor-not-allowed disabled:opacity-50',
        className,
      )}
      {...props}
    >
      {children}
      <Sel.Icon className="text-[var(--color-text-dim)]" aria-hidden>
        ▾
      </Sel.Icon>
    </Sel.Trigger>
  ),
);
SelectTrigger.displayName = 'SelectTrigger';

export function SelectContent({
  children,
  className,
  ...props
}: React.ComponentPropsWithoutRef<typeof Sel.Content>) {
  return (
    <Sel.Portal>
      <Sel.Content
        position="popper"
        sideOffset={4}
        className={cn(
          // Above Leaflet panes (which max at z-700). Otherwise nav unit-selector
          // dropdown is clipped by the map on /v2/map.
          'z-[1100] overflow-hidden rounded border bg-[var(--color-surface)] shadow-lg',
          'border-[var(--color-border-default)]',
          // Width matches trigger via Radix; min-w prevents jitter
          'min-w-[var(--radix-select-trigger-width)]',
          'data-[state=open]:animate-in data-[state=closed]:animate-out',
          className,
        )}
        {...props}
      >
        <Sel.Viewport className="p-1">{children}</Sel.Viewport>
      </Sel.Content>
    </Sel.Portal>
  );
}

interface ItemProps extends React.ComponentPropsWithoutRef<typeof Sel.Item> {
  children: ReactNode;
}

export const SelectItem = forwardRef<HTMLDivElement, ItemProps>(
  ({ children, className, ...props }, ref) => (
    <Sel.Item
      ref={ref}
      className={cn(
        'relative flex cursor-pointer select-none items-center rounded px-3 py-1.5 text-sm outline-none',
        'text-[var(--color-text)] data-[highlighted]:bg-[var(--color-surface-2)]',
        'data-[state=checked]:text-[var(--color-accent)]',
        'min-h-[40px]',
        className,
      )}
      {...props}
    >
      <Sel.ItemText>{children}</Sel.ItemText>
    </Sel.Item>
  ),
);
SelectItem.displayName = 'SelectItem';
