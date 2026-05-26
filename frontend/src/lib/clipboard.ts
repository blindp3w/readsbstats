// Cross-context clipboard helper.
//
// Production deploys serve from `http://192.168.1.60` (plain HTTP on the
// LAN), where `navigator.clipboard` is **undefined** — that API only
// exists in secure contexts (HTTPS / localhost / file://). The fallback
// path therefore runs in every real session on the Pi; the modern
// `navigator.clipboard` path only fires under `npm run dev` (localhost)
// or HTTPS deployments.

export async function copyToClipboard(value: string): Promise<boolean> {
  if (window.isSecureContext && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(value);
      return true;
    } catch {
      // Permission denied / iframe sandbox — fall through to execCommand.
    }
  }
  const ta = document.createElement('textarea');
  ta.value = value;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.opacity = '0';
  ta.style.pointerEvents = 'none';
  document.body.appendChild(ta);
  try {
    ta.focus();
    ta.select();
    return document.execCommand('copy');
  } catch {
    return false;
  } finally {
    // Audit 2026-05-26: removal must run even when execCommand throws,
    // otherwise repeated copy failures leak hidden DOM nodes and steal
    // focus from interactive controls.
    ta.remove();
  }
}
