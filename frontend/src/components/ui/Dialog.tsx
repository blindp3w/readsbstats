import * as Dlg from '@radix-ui/react-dialog';
import { forwardRef } from 'react';
import { cn } from '@/lib/cn';

// Radix Dialog with shadcn styling. Replaces native window.confirm() for
// destructive actions — gives us proper focus trapping, ESC-to-close, and
// a styled surface that matches the rest of the UI.

export const Dialog = Dlg.Root;
export const DialogTrigger = Dlg.Trigger;
export const DialogClose = Dlg.Close;

export const DialogContent = forwardRef<
  HTMLDivElement,
  React.ComponentPropsWithoutRef<typeof Dlg.Content>
>(({ className, children, ...props }, ref) => (
  <Dlg.Portal>
    <Dlg.Overlay className="fixed inset-0 z-[999] bg-black/60 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out" />
    <Dlg.Content
      ref={ref}
      className={cn(
        'fixed left-1/2 top-1/2 z-[1000] w-[min(420px,calc(100vw-2rem))] -translate-x-1/2 -translate-y-1/2',
        'rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface)] p-5 shadow-2xl',
        'focus:outline-none',
        className,
      )}
      {...props}
    >
      {children}
    </Dlg.Content>
  </Dlg.Portal>
));
DialogContent.displayName = 'DialogContent';

export function DialogHeader({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn('mb-3 space-y-1', className)} {...props} />;
}

export const DialogTitle = forwardRef<
  HTMLHeadingElement,
  React.ComponentPropsWithoutRef<typeof Dlg.Title>
>(({ className, ...props }, ref) => (
  <Dlg.Title
    ref={ref}
    className={cn('text-base font-semibold text-[var(--color-text)]', className)}
    {...props}
  />
));
DialogTitle.displayName = 'DialogTitle';

export const DialogDescription = forwardRef<
  HTMLParagraphElement,
  React.ComponentPropsWithoutRef<typeof Dlg.Description>
>(({ className, ...props }, ref) => (
  <Dlg.Description
    ref={ref}
    className={cn('text-sm text-[var(--color-text-dim)]', className)}
    {...props}
  />
));
DialogDescription.displayName = 'DialogDescription';

export function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn('mt-4 flex flex-wrap items-center justify-end gap-2', className)}
      {...props}
    />
  );
}
