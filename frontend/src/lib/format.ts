/** Format a token count with locale grouping (e.g., 1,234,567) */
export function fmtTokens(n: number): string {
  return n ? n.toLocaleString() : '—';
}

/** Format cost to 4 decimal places */
export function fmtCost(n: number): string {
  return n ? `$${n.toFixed(4)}` : '—';
}

/** Format cost to 2 decimal places */
export function fmtCost2(n: number): string {
  return n ? `$${n.toFixed(2)}` : '—';
}

/** Format seconds into a human-readable duration string */
export function fmtDuration(sec: number): string {
  if (!sec || sec <= 0) return '—';
  const s = Math.round(sec);
  const mins = Math.floor(s / 60);
  const hrs = Math.floor(mins / 60);
  const remainMins = mins % 60;
  const remainSecs = s % 60;
  if (hrs) return `${hrs}h ${remainMins}m ${remainSecs}s`;
  if (mins) return `${mins}m ${remainSecs}s`;
  return `${s}s`;
}

/** Format a live elapsed time from a start timestamp (epoch seconds) */
export function fmtElapsed(startedAt: number): string {
  if (!startedAt) return '';
  const now = Date.now() / 1000;
  return fmtDuration(now - startedAt);
}

/** Format a success rate as percentage */
export function fmtPercent(rate: number): string {
  return `${(rate * 100).toFixed(0)}%`;
}
