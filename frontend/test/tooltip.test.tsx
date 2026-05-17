/**
 * Tooltip wrapper — confirms <SimpleTooltip> and the primitives work
 * within <TooltipProvider> and reveal their content on focus.
 *
 * Radix Tooltip opens on pointerover OR focus; focus is the more
 * deterministic path in jsdom (no synthetic hover events needed).
 */

import { describe, it, expect } from 'vitest';
import { render, fireEvent, waitFor } from '@testing-library/react';
import {
  SimpleTooltip,
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from '@/components/ui/Tooltip';

function withProvider(ui: React.ReactNode) {
  // delayDuration=0 so the tooltip opens synchronously on focus, no waiting.
  return <TooltipProvider delayDuration={0}>{ui}</TooltipProvider>;
}

describe('Tooltip', () => {
  it('SimpleTooltip wraps a focusable trigger and shows content on focus', async () => {
    const { getByRole } = render(
      withProvider(
        <SimpleTooltip content="hello tooltip">
          <button type="button">trigger</button>
        </SimpleTooltip>,
      ),
    );
    const btn = getByRole('button', { name: /trigger/i });
    btn.focus();
    fireEvent.focus(btn);
    await waitFor(() => {
      // Radix renders TooltipContent into a Portal — query document.body.
      expect(document.body.textContent).toContain('hello tooltip');
    });
  });

  it('does not render content until the trigger is focused', () => {
    const { container } = render(
      withProvider(
        <SimpleTooltip content="hidden until focused">
          <button type="button">trigger</button>
        </SimpleTooltip>,
      ),
    );
    expect(container.textContent).not.toContain('hidden until focused');
    expect(document.body.textContent).not.toContain('hidden until focused');
  });

  it('primitives compose (manual Trigger/Content)', async () => {
    const { getByRole } = render(
      withProvider(
        <Tooltip>
          <TooltipTrigger asChild>
            <button type="button">manual</button>
          </TooltipTrigger>
          <TooltipContent>manual body</TooltipContent>
        </Tooltip>,
      ),
    );
    const btn = getByRole('button', { name: /manual/i });
    btn.focus();
    fireEvent.focus(btn);
    await waitFor(() => {
      expect(document.body.textContent).toContain('manual body');
    });
  });
});
