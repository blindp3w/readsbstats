// Click-to-enlarge wrapper for aircraft photos. Wraps the caller's
// thumbnail in a Radix Dialog trigger; on click opens a centered modal
// showing the large image + photographer credit + "view on source" link
// (when link_url is available — typically a Planespotters listing).
//
// Used by the Flight detail page and the Aircraft detail page (M3
// follow-up, v2.9.1). The caller renders its own thumbnail so the
// existing aspect-ratio / sizing / focus styling stays in its own
// context.
//
// When `largeUrl` resolves to "" via safeUrl (no photo, non-HTTPS, etc.)
// the lightbox degrades to just rendering the children — no trigger, no
// dialog. The caller doesn't need to branch.

import { type ReactNode } from 'react';
import { Cross2Icon, ExternalLinkIcon } from '@radix-ui/react-icons';
import { Dialog, DialogTrigger, DialogClose } from '@/components/ui/Dialog';
import * as Dlg from '@radix-ui/react-dialog';
import { safeUrl } from '@/lib/safeUrl';
import { cn } from '@/lib/cn';

interface Props {
  photo:
    | {
        large_url: string | null;
        thumbnail_url: string | null;
        link_url: string | null;
        photographer: string | null;
        is_type_photo?: boolean;
      }
    | null
    | undefined;
  // The aircraft alt text — used on both the trigger thumbnail (caller's
  // <img>) and the enlarged image inside the dialog.
  alt: string;
  // The trigger element. Must be a SINGLE React element (button-like)
  // for Radix asChild semantics — wrap your <img> in a <button>.
  children: ReactNode;
}

export function PhotoLightbox({ photo, alt, children }: Props) {
  const largeUrl = safeUrl(photo?.large_url ?? null) || safeUrl(photo?.thumbnail_url ?? null);
  const linkUrl = safeUrl(photo?.link_url ?? null);
  // Degrade to just rendering the children when there's no enlarged image
  // to show. The caller's thumbnail still works as a no-op visual.
  if (!largeUrl) return <>{children}</>;
  return (
    <Dialog>
      <DialogTrigger asChild>{children}</DialogTrigger>
      <Dlg.Portal>
        <Dlg.Overlay className="fixed inset-0 z-[999] bg-black/80 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out" />
        <Dlg.Content
          className={cn(
            'fixed left-1/2 top-1/2 z-[1000] -translate-x-1/2 -translate-y-1/2',
            'flex max-h-[calc(100vh-2rem)] w-[min(960px,calc(100vw-2rem))] flex-col',
            'overflow-hidden rounded-lg border border-[var(--color-border-default)] bg-[var(--color-surface)] shadow-2xl',
            'focus:outline-none',
          )}
          data-testid="photo-lightbox-content"
        >
          <Dlg.Title className="sr-only">{alt}</Dlg.Title>
          <Dlg.Description className="sr-only">
            Aircraft photo
            {photo?.photographer ? ` by ${photo.photographer}` : ''}
          </Dlg.Description>
          <div className="relative flex flex-1 items-center justify-center bg-[var(--color-bg)]">
            <img
              src={largeUrl}
              alt={alt}
              loading="lazy"
              // w-full + object-contain: image fills the dialog width
              // (or scales down to fit max-h) while preserving its
              // natural aspect ratio. When the source only returned a
              // thumbnail (~240 px) it upscales to the container width;
              // when it returned a true large variant (~900 px+) it
              // renders crisp. Without w-full small images were
              // rendering at their natural size and the dialog felt
              // empty (flight_details5.png).
              className="block h-auto w-full max-w-full object-contain"
              style={{ maxHeight: 'calc(100vh - 6rem)' }}
            />
            <DialogClose asChild>
              <button
                type="button"
                aria-label="Close"
                data-testid="photo-lightbox-close"
                className="absolute right-2 top-2 inline-flex h-9 w-9 items-center justify-center rounded-full bg-[var(--color-surface)]/80 text-[var(--color-text)] backdrop-blur transition-colors hover:bg-[var(--color-surface)] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[var(--color-accent)]"
              >
                <Cross2Icon width={18} height={18} aria-hidden="true" />
              </button>
            </DialogClose>
          </div>
          {(photo?.photographer || linkUrl) && (
            <div
              className="flex flex-wrap items-center justify-between gap-3 border-t border-[var(--color-border-default)] px-4 py-2 text-xs text-[var(--color-text-dim)]"
              data-testid="photo-lightbox-footer"
            >
              <span>
                {photo?.photographer ? <>© {photo.photographer}</> : null}
                {photo?.is_type_photo ? ' · type photo' : ''}
              </span>
              {linkUrl ? (
                <a
                  href={linkUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-[var(--color-accent)] hover:underline"
                  data-testid="photo-lightbox-source-link"
                >
                  view on source
                  <ExternalLinkIcon aria-hidden="true" />
                </a>
              ) : null}
            </div>
          )}
        </Dlg.Content>
      </Dlg.Portal>
    </Dialog>
  );
}
