import * as DM from '@radix-ui/react-dropdown-menu';
import { forwardRef } from 'react';
import { cn } from '@/lib/cn';

export const DropdownMenu = DM.Root;
export const DropdownMenuTrigger = DM.Trigger;

export const DropdownMenuContent = forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof DM.Content>
>(({ className, align = 'end', sideOffset = 6, collisionPadding = 8, ...props }, ref) => (
  <DM.Portal>
    <DM.Content
      ref={ref}
      align={align}
      sideOffset={sideOffset}
      collisionPadding={collisionPadding}
      className={cn(
        // Held at z-[1100] so the mobile nav dropdown stays above the map
        // on /map regardless of MapLibre overlay z-indices.
        'z-[1100] min-w-[200px] overflow-hidden rounded border bg-[var(--color-surface)] p-1 shadow-lg',
        'border-[var(--color-border-default)]',
        'data-[state=open]:animate-in data-[state=closed]:animate-out',
        className,
      )}
      {...props}
    />
  </DM.Portal>
));
DropdownMenuContent.displayName = 'DropdownMenuContent';

export const DropdownMenuItem = forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof DM.Item>
>(({ className, ...props }, ref) => (
  <DM.Item
    ref={ref}
    className={cn(
      'relative flex cursor-pointer select-none items-center rounded px-3 py-2 text-sm outline-none',
      'text-[var(--color-text-dim)]',
      'data-[highlighted]:bg-[var(--color-surface-2)] data-[highlighted]:text-[var(--color-text)]',
      'data-[disabled]:cursor-not-allowed data-[disabled]:opacity-50',
      'min-h-[44px]',
      className,
    )}
    {...props}
  />
));
DropdownMenuItem.displayName = 'DropdownMenuItem';

export const DropdownMenuSeparator = forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof DM.Separator>
>(({ className, ...props }, ref) => (
  <DM.Separator
    ref={ref}
    className={cn('-mx-1 my-1 h-px bg-[var(--color-border-default)]', className)}
    {...props}
  />
));
DropdownMenuSeparator.displayName = 'DropdownMenuSeparator';
