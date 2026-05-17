import * as Tip from '@radix-ui/react-tooltip';
import { forwardRef, type ReactNode } from 'react';
import { cn } from '@/lib/cn';

// Radix Tooltip with shadcn-flavoured styling. Mount <TooltipProvider> once
// at the app root so every tooltip shares the same delay/skip behaviour.
//
// For one-off usage prefer <SimpleTooltip content="…">{trigger}</SimpleTooltip>.
// For composed triggers (e.g. nesting Tooltip + Popover on the same button)
// use the Root/Trigger/Content primitives directly.

export const TooltipProvider = Tip.Provider;
export const Tooltip = Tip.Root;
export const TooltipTrigger = Tip.Trigger;

export const TooltipContent = forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof Tip.Content>
>(({ className, sideOffset = 4, collisionPadding = 8, ...props }, ref) => (
  <Tip.Portal>
    <Tip.Content
      ref={ref}
      sideOffset={sideOffset}
      collisionPadding={collisionPadding}
      className={cn(
        // Above Leaflet panes (z-700) so tooltips on /map render correctly.
        'z-[1100] max-w-xs rounded border px-2 py-1 text-xs leading-tight shadow-lg',
        'border-[var(--color-border-default)] bg-[var(--color-surface)] text-[var(--color-text)]',
        'data-[state=delayed-open]:animate-in data-[state=closed]:animate-out',
        className,
      )}
      {...props}
    />
  </Tip.Portal>
));
TooltipContent.displayName = 'TooltipContent';

interface SimpleTooltipProps {
  content: ReactNode;
  children: ReactNode;
  side?: 'top' | 'right' | 'bottom' | 'left';
  align?: 'start' | 'center' | 'end';
  // Some triggers benefit from a shorter hover delay (e.g. badges that
  // users mouse over briefly); accept an override per-instance.
  delayDuration?: number;
}

export function SimpleTooltip({
  content,
  children,
  side = 'top',
  align = 'center',
  delayDuration,
}: SimpleTooltipProps) {
  return (
    <Tooltip delayDuration={delayDuration}>
      <TooltipTrigger asChild>{children}</TooltipTrigger>
      <TooltipContent side={side} align={align}>
        {content}
      </TooltipContent>
    </Tooltip>
  );
}
