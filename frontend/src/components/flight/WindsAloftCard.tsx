import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/Card';
import { Table, THead, TBody, TR, TH, TD } from '@/components/ui/Table';
import { useFormat } from '@/hooks/useFormat';
import { windsAloftRows } from '@/lib/vdl2Points';
import type { Vdl2Position } from '@/lib/types';

// Winds/temperatures aloft reported by the aircraft over ACARS (only the h1-abs CSV
// dialect carries them). One compact row per fix, oldest first; the whole card is
// absent when no fix has temperature or wind — most flights.
export function WindsAloftCard({ fixes }: { fixes: Vdl2Position[] }) {
  const { fmtTs, fmtAlt, fmtSpd } = useFormat();
  const rows = windsAloftRows(fixes);
  if (rows.length === 0) return null;
  return (
    <Card data-testid="flight-winds-card">
      <CardHeader>
        <CardTitle>Winds + temps aloft ({rows.length})</CardTitle>
      </CardHeader>
      <CardContent>
        {/* Cap the height like the position log; Table scrolls horizontally on phones. */}
        <div className="max-h-72 overflow-y-auto">
          <Table>
            <THead>
              <TR>
                <TH>Time</TH>
                <TH>Alt</TH>
                <TH>Temp</TH>
                <TH>Wind</TH>
              </TR>
            </THead>
            <TBody>
              {rows.map((r, i) => (
                <TR key={`${r.ts}-${i}`} data-testid="winds-aloft-row">
                  <TD className="tabnum whitespace-nowrap text-xs">{fmtTs(r.ts)}</TD>
                  <TD className="tabnum whitespace-nowrap text-xs">
                    {r.alt != null ? fmtAlt(r.alt) : '—'}
                  </TD>
                  <TD className="tabnum whitespace-nowrap text-xs">
                    {r.temp_c != null ? `${r.temp_c} °C` : '—'}
                  </TD>
                  <TD className="tabnum whitespace-nowrap text-xs">
                    {r.wind_dir != null || r.wind_kt != null
                      ? [
                          r.wind_dir != null ? `${String(Math.round(r.wind_dir)).padStart(3, '0')}°` : null,
                          r.wind_kt != null ? fmtSpd(r.wind_kt) : null,
                        ]
                          .filter(Boolean)
                          .join(' / ')
                      : '—'}
                  </TD>
                </TR>
              ))}
            </TBody>
          </Table>
        </div>
      </CardContent>
    </Card>
  );
}
