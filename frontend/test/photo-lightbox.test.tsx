/**
 * v2.9.1 — PhotoLightbox click-to-enlarge wrapper. Used by Flight detail
 * + Aircraft detail. Tests pin:
 *   - default state: trigger renders, dialog content not in DOM
 *   - click trigger: dialog opens, large image + photographer + source
 *     link appear
 *   - degrades: when no large/thumbnail URL, falls through to children
 *     (no dialog wrapping)
 */

import { describe, it, expect } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { PhotoLightbox } from '@/components/PhotoLightbox';

const PHOTO = {
  large_url: 'https://example.com/big.jpg',
  thumbnail_url: 'https://example.com/thumb.jpg',
  link_url: 'https://www.planespotters.net/photo/12345',
  photographer: 'Jane Doe',
  is_type_photo: false,
};

function renderTrigger(props: Partial<React.ComponentProps<typeof PhotoLightbox>> = {}) {
  return render(
    <PhotoLightbox photo={PHOTO} alt="SP-LRF" {...props}>
      <button type="button" data-testid="trigger-btn">
        <img src="https://example.com/thumb.jpg" alt="SP-LRF" />
      </button>
    </PhotoLightbox>,
  );
}

describe('PhotoLightbox', () => {
  it('renders the trigger but not the dialog content initially', () => {
    const { container } = renderTrigger();
    expect(container.querySelector('[data-testid="trigger-btn"]')).toBeTruthy();
    expect(document.querySelector('[data-testid="photo-lightbox-content"]')).toBeNull();
  });

  it('clicking the trigger opens the dialog with large image + footer', () => {
    const { container } = renderTrigger();
    fireEvent.click(container.querySelector('[data-testid="trigger-btn"]')!);
    const content = document.querySelector('[data-testid="photo-lightbox-content"]');
    expect(content).toBeTruthy();
    const img = content!.querySelector('img');
    expect(img?.getAttribute('src')).toBe('https://example.com/big.jpg');
    const footer = document.querySelector('[data-testid="photo-lightbox-footer"]');
    expect(footer?.textContent).toContain('Jane Doe');
    const link = document.querySelector('[data-testid="photo-lightbox-source-link"]');
    expect(link?.getAttribute('href')).toBe('https://www.planespotters.net/photo/12345');
    expect(link?.getAttribute('target')).toBe('_blank');
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('degrades to plain children when there is no large_url or thumbnail_url', () => {
    const { container } = renderTrigger({
      photo: { ...PHOTO, large_url: null, thumbnail_url: null },
    });
    // Children still render but click does NOT open a dialog.
    expect(container.querySelector('[data-testid="trigger-btn"]')).toBeTruthy();
    fireEvent.click(container.querySelector('[data-testid="trigger-btn"]')!);
    expect(document.querySelector('[data-testid="photo-lightbox-content"]')).toBeNull();
  });

  it('omits the source link when link_url is missing', () => {
    const { container } = renderTrigger({ photo: { ...PHOTO, link_url: null } });
    fireEvent.click(container.querySelector('[data-testid="trigger-btn"]')!);
    expect(document.querySelector('[data-testid="photo-lightbox-content"]')).toBeTruthy();
    expect(document.querySelector('[data-testid="photo-lightbox-source-link"]')).toBeNull();
  });

  it('rejects non-HTTPS large_url via safeUrl and degrades', () => {
    const { container } = renderTrigger({
      photo: { ...PHOTO, large_url: 'http://example.com/insecure.jpg', thumbnail_url: null },
    });
    fireEvent.click(container.querySelector('[data-testid="trigger-btn"]')!);
    expect(document.querySelector('[data-testid="photo-lightbox-content"]')).toBeNull();
  });

  it('falls back to thumbnail_url when large_url is missing', () => {
    const { container } = renderTrigger({
      photo: { ...PHOTO, large_url: null },
    });
    fireEvent.click(container.querySelector('[data-testid="trigger-btn"]')!);
    const img = document
      .querySelector('[data-testid="photo-lightbox-content"]')
      ?.querySelector('img');
    expect(img?.getAttribute('src')).toBe('https://example.com/thumb.jpg');
  });
});
