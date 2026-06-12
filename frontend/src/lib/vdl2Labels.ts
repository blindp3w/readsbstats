// Human-readable names for ACARS 2-char message labels.
//
// Sources: ARINC 618/620 standard label conventions, the airframesio
// acars-message-documentation research files, and the acarsonline label list —
// cross-checked against the live feed (6.4-day dump, 2026-06). Many labels are
// airline-defined rather than standardized; those get an honest generic name
// instead of invented semantics (notably label 49, whose observed Etihad/LOT
// movement form differs from e.g. Air Canada's use of the same code).
//
// Codes are stored uppercase at ingest; `labelName` normalizes its input so the
// filter-input surface (raw user typing) resolves too. Unknown codes return
// null — callers fall back to the bare code.

const AIRLINE_DEFINED = 'Airline-defined report';

export const VDL2_LABEL_NAMES: Record<string, string> = {
  // Link control / system
  Q0: 'Link test',
  Q1: 'Q-series operational report',
  Q2: 'ETA report',
  Q3: 'Q-series operational report',
  Q4: 'Q-series operational report',
  Q5: 'Q-series operational report',
  Q6: 'Q-series operational report',
  QX: 'Q-series operational report',
  _D: 'No info to transmit (general response)',
  SA: 'Media advisory',
  HX: 'Undelivered uplink report',
  MA: 'MIAM multi-block message',

  // OOOI block-time reports (QA–QD original set, QE/QF fuel/destination
  // variants, QP–QS the set this feed actually carries — see vdl2/oooi.py)
  QA: 'OUT report (off gate)',
  QB: 'OFF report (wheels up)',
  QC: 'ON report (wheels down)',
  QD: 'IN report (on gate)',
  QE: 'OUT / fuel report',
  QF: 'OFF / destination report',
  QP: 'OUT report (off gate)',
  QQ: 'OFF report (wheels up)',
  QR: 'ON report (wheels down)',
  QS: 'IN report (on gate)',

  // Terminal-equipment and weather
  H1: 'Terminal equipment data (ACMS/FMS/CFDIU)',
  H2: 'Meteorological report',
  '5U': 'Weather request',
  '5Z': 'Airline-designated downlink',

  // ATS datalink (ARINC 623 / FANS B-series)
  B0: 'AFN logon (CPDLC contact)',
  B1: 'Oceanic clearance request',
  B2: 'Oceanic clearance readback',
  B3: 'Departure clearance request',
  B4: 'Departure clearance readback',
  B5: 'ATS datalink message',
  B6: 'ATS datalink message',
  B7: 'ATS datalink message',
  B8: 'ATS datalink message',
  B9: 'ATIS request (D-ATIS)',

  // Reasonably documented numeric labels
  '11': 'In-range arrival report',
  '13': 'Arrival gate / ETA request',
  '15': 'Position report',
  '16': 'Position report (AUTPOS)',
  '17': 'Position / weather report',
  '3J': 'Airline downlink message',
  '49': 'Airline-defined status/movement report',
  '80': 'Airline-defined automated report',

  // Airline-defined ranges — no standardized meaning
  '10': AIRLINE_DEFINED,
  '12': AIRLINE_DEFINED,
  '14': AIRLINE_DEFINED,
  '18': AIRLINE_DEFINED,
  '19': AIRLINE_DEFINED,
  '1B': AIRLINE_DEFINED,
  '1L': AIRLINE_DEFINED,
  '1M': AIRLINE_DEFINED,
  '20': AIRLINE_DEFINED,
  '22': AIRLINE_DEFINED,
  '26': AIRLINE_DEFINED,
  '27': AIRLINE_DEFINED,
  '2A': AIRLINE_DEFINED,
  '2F': AIRLINE_DEFINED,
  '2P': AIRLINE_DEFINED,
  '2T': AIRLINE_DEFINED,
  '2Z': AIRLINE_DEFINED,
  '30': AIRLINE_DEFINED,
  '33': AIRLINE_DEFINED,
  '34': AIRLINE_DEFINED,
  '35': AIRLINE_DEFINED,
  '36': AIRLINE_DEFINED,
  '37': AIRLINE_DEFINED,
  '38': AIRLINE_DEFINED,
  '3P': AIRLINE_DEFINED,
  '42': AIRLINE_DEFINED,
  '44': AIRLINE_DEFINED,
  '4W': AIRLINE_DEFINED,
  '82': AIRLINE_DEFINED,
  '83': AIRLINE_DEFINED,
  '84': AIRLINE_DEFINED,
  '85': AIRLINE_DEFINED,
  '88': AIRLINE_DEFINED,
  '8A': AIRLINE_DEFINED,
  '8B': AIRLINE_DEFINED,
  '8C': AIRLINE_DEFINED,
  '8F': AIRLINE_DEFINED,
  '8S': AIRLINE_DEFINED,
  CA: AIRLINE_DEFINED,
  CD: AIRLINE_DEFINED,
  VK: AIRLINE_DEFINED,
};

export function labelName(code: string | null | undefined): string | null {
  if (!code) return null;
  return VDL2_LABEL_NAMES[code.trim().toUpperCase()] ?? null;
}
