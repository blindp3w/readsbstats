/**
 * Render-coverage for the trivial presentational wrappers that had no test:
 * CardDescription, DialogHeader/DialogFooter, SheetHeader. They each render a
 * plain element, forward arbitrary props (so data-testid lands on the node),
 * and merge a custom className over their base classes.
 */
import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { CardDescription } from '@/components/ui/Card';
import { DialogHeader, DialogFooter } from '@/components/ui/Dialog';
import { SheetHeader } from '@/components/ui/Sheet';

describe('CardDescription', () => {
  it('renders a <p>, merges className, forwards props', () => {
    render(
      <CardDescription className="custom-x" data-testid="cd">
        hello
      </CardDescription>,
    );
    const el = screen.getByTestId('cd');
    expect(el.tagName).toBe('P');
    expect(el.textContent).toContain('hello');
    expect(el.className).toContain('custom-x');
    expect(el.className).toContain('text-sm');
  });
});

describe('DialogHeader / DialogFooter', () => {
  it('DialogHeader renders a <div>, merges className, forwards props', () => {
    render(
      <DialogHeader className="dh-x" data-testid="dh">
        head
      </DialogHeader>,
    );
    const el = screen.getByTestId('dh');
    expect(el.tagName).toBe('DIV');
    expect(el.textContent).toContain('head');
    expect(el.className).toContain('dh-x');
    expect(el.className).toContain('space-y-1');
  });

  it('DialogFooter renders a <div>, merges className, forwards props', () => {
    render(
      <DialogFooter className="df-x" data-testid="df">
        foot
      </DialogFooter>,
    );
    const el = screen.getByTestId('df');
    expect(el.tagName).toBe('DIV');
    expect(el.textContent).toContain('foot');
    expect(el.className).toContain('df-x');
    expect(el.className).toContain('justify-end');
  });
});

describe('SheetHeader', () => {
  it('renders a <div>, merges className, forwards props', () => {
    render(
      <SheetHeader className="sh-x" data-testid="sh">
        sheet
      </SheetHeader>,
    );
    const el = screen.getByTestId('sh');
    expect(el.tagName).toBe('DIV');
    expect(el.textContent).toContain('sheet');
    expect(el.className).toContain('sh-x');
    expect(el.className).toContain('space-y-1');
  });
});
