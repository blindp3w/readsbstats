import * as Pop from '@radix-ui/react-popover';
import { forwardRef } from 'react';
import { cn } from '@/lib/cn';

// Floating panel anchored to a trigger. Used for the Custom range picker
// on /v2/ and /v2/metrics. Closes on outside click / ESC / Apply.

export const Popover = Pop.Root;
export const PopoverTrigger = Pop.Trigger;
export const PopoverAnchor = Pop.Anchor;
export const PopoverClose = Pop.Close;

type ContentProps = React.ComponentPropsWithoutRef<typeof Pop.Content>;

export const PopoverContent = forwardRef<HTMLDivElement, ContentProps>(
  ({ className, sideOffset = 6, align = 'end', collisionPadding = 8, ...props }, ref) => (
    <Pop.Portal>
      <Pop.Content
        ref={ref}
        sideOffset={sideOffset}
        align={align}
        collisionPadding={collisionPadding}
        className={cn(
          // Held at z-[1050] so popovers anchored to controls on /map sit
          // above MapLibre's canvas and any future map-overlay UI.
          'z-[1050] rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface)] p-3 shadow-[var(--shadow-lg)]',
          'data-[state=open]:animate-in data-[state=closed]:animate-out',
          'focus:outline-none',
          className,
        )}
        {...props}
      />
    </Pop.Portal>
  ),
);
PopoverContent.displayName = 'PopoverContent';
