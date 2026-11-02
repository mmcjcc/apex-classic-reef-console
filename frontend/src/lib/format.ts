// Small formatting helpers shared by all pages. Times arrive as UTC epoch seconds and are shown in this device's zone.

export function ago(ts: number | null | undefined, now: number = Date.now() / 1000): string {
  if (!ts) return 'never';
  const s = Math.max(0, now - ts);
  if (s < 90) return `${Math.round(s)} s ago`;
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${(s / 3600).toFixed(1)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}

export function clock(ts: number | null | undefined): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function longClock(ts: number | null | undefined): string {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString([], { weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

/** A probe's value with the precision its unit deserves. */
export function reading(value: number | null | undefined, unit: string): string {
  if (value === null || value === undefined) return '—';
  if (unit === 'pH') return value.toFixed(2);
  if (unit === 'mV') return Math.round(value).toString();
  return value.toFixed(1);
}

export function unitText(unit: string): string {
  return unit === 'pH' || unit === 'switch' ? '' : unit;
}

/** The range of a series as "low – high". */
export function span(points: [number, number][], unit: string): string {
  if (points.length === 0) return 'no history yet';
  const values = points.map((p) => p[1]);
  return `${reading(Math.min(...values), unit)} – ${reading(Math.max(...values), unit)}`;
}

/** datetime-local input value for a Date, in this device's zone. */
export function localInput(date: Date): string {
  const pad = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export const EVENT_TEXT: Record<string, string> = {
  'condition.start': 'Alert started',
  'condition.end': 'Alert ended',
  'apex.power': 'Apex power',
  'apex.program': 'Programs changed',
  'apex.timezone': 'Apex timezone changed',
  'apex.clock': 'Apex clock',
  'module.new': 'New module',
  'history.fill': 'History filled',
  'history.backfill': 'History backfilled',
  'history.paused': 'History fill paused',
  'analysis.unusual': 'Unusual reading',
  'analysis.drift': 'Drift',
  'analysis.step': 'Level step',
  'analysis.frozen': 'Frozen probe',
  'analysis.recurring': 'Recurring departure',
  'analysis.probe': 'Probe off its tests',
  'analysis.equipment': 'Equipment change',
  'analysis.drip': 'Reactor drip slowing',
  'report.weekly': 'Weekly report',
};
