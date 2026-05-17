import * as Dlg from '@radix-ui/react-dialog';
import { Cross2Icon } from '@radix-ui/react-icons';
import { forwardRef } from 'react';
import { cn } from '@/lib/cn';

// Slide-out side panel built on Radix Dialog. Used for the live-map's
// per-aircraft detail drawer. Same focus-trap and ESC-to-close as Dialog,
// but anchored to the screen edge with a slide animation.

export const Sheet = Dlg.Root;
export const SheetTrigger = Dlg.Trigger;
export const SheetClose = Dlg.Close;

type Side = 'right' | 'left' | 'top' | 'bottom';

const sideClass: Record<Side, string> = {
  right:
    'right-0 top-0 h-full w-[min(420px,90vw)] border-l data-[state=open]:animate-in data-[state=open]:slide-in-from-right data-[state=closed]:animate-out data-[state=closed]:slide-out-to-right',
  left:
    'left-0 top-0 h-full w-[min(420px,90vw)] border-r data-[state=open]:animate-in data-[state=open]:slide-in-from-left data-[state=closed]:animate-out data-[state=closed]:slide-out-to-left',
  top:
    'top-0 inset-x-0 h-[60vh] border-b data-[state=open]:animate-in data-[state=open]:slide-in-from-top data-[state=closed]:animate-out data-[state=closed]:slide-out-to-top',
  bottom:
    'bottom-0 inset-x-0 h-[60vh] border-t data-[state=open]:animate-in data-[state=open]:slide-in-from-bottom data-[state=closed]:animate-out data-[state=closed]:slide-out-to-bottom',
};

interface ContentProps extends React.ComponentPropsWithoutRef<typeof Dlg.Content> {
  side?: Side;
}

export const SheetContent = forwardRef<HTMLDivElement, ContentProps>(
  ({ className, side = 'right', children, ...props }, ref) => (
    <Dlg.Portal>
      {/* z-index above Leaflet's max (popup pane = 700). Tailwind's z-50 = 50
          which is well below — without this the side panel rendered BEHIND
          the map tiles on /v2/map. */}
      <Dlg.Overlay className="fixed inset-0 z-[999] bg-black/40 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out" />
      <Dlg.Content
        ref={ref}
        className={cn(
          'fixed z-[1000] overflow-y-auto border-[var(--color-border-default)] bg-[var(--color-surface)] p-5 shadow-2xl focus:outline-none',
          sideClass[side],
          className,
        )}
        {...props}
      >
        {/* Corner close button — always visible affordance. Mobile users
            in particular look for the X first; ESC isn't discoverable. */}
        <Dlg.Close
          aria-label="Close"
          className="absolute right-3 top-3 rounded p-1 text-[var(--color-text-dim)] hover:bg-[var(--color-surface-2)] hover:text-[var(--color-text)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
        >
          <Cross2Icon aria-hidden="true" />
        </Dlg.Close>
        {children}
      </Dlg.Content>
    </Dlg.Portal>
  ),
);
SheetContent.displayName = 'SheetContent';

export function SheetHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('mb-3 space-y-1', className)} {...props} />;
}

export const SheetTitle = forwardRef<
  HTMLHeadingElement,
  React.ComponentPropsWithoutRef<typeof Dlg.Title>
>(({ className, ...props }, ref) => (
  <Dlg.Title
    ref={ref}
    className={cn('text-base font-semibold text-[var(--color-text)]', className)}
    {...props}
  />
));
SheetTitle.displayName = 'SheetTitle';

export const SheetDescription = forwardRef<
  HTMLParagraphElement,
  React.ComponentPropsWithoutRef<typeof Dlg.Description>
>(({ className, ...props }, ref) => (
  <Dlg.Description
    ref={ref}
    className={cn('text-sm text-[var(--color-text-dim)]', className)}
    {...props}
  />
));
SheetDescription.displayName = 'SheetDescription';
