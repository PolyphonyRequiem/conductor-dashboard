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

/** ADO state category → Tailwind badge classes (fallback when no twig color available) */
const CATEGORY_BADGE: Record<string, string> = {
  proposed: 'bg-gray-700/40 text-gray-300',
  inprogress: 'bg-blue-900/40 text-blue-300',
  completed: 'bg-green-900/40 text-green-300',
  removed: 'bg-red-900/40 text-red-300',
};

export function stateBadgeClass(state: string, category?: string): string {
  if (category) {
    return CATEGORY_BADGE[category.toLowerCase()] ?? CATEGORY_BADGE.proposed!;
  }
  const s = state.toLowerCase();
  if (['done', 'closed', 'completed', 'resolved'].includes(s)) return CATEGORY_BADGE.completed!;
  if (['doing', 'active', 'started', 'in progress', 'committed'].includes(s)) return CATEGORY_BADGE.inprogress!;
  if (['removed', 'cut'].includes(s)) return CATEGORY_BADGE.removed!;
  if (['to do', 'proposed', 'new', 'design', 'requested'].includes(s)) return CATEGORY_BADGE.proposed!;
  return 'bg-[--color-surface] text-[--color-text2]';
}

/** Get inline style for a state based on its hex color from twig DB */
export function stateColorStyle(hexColor?: string): React.CSSProperties | undefined {
  if (!hexColor || hexColor === 'b2b2b2' || hexColor === 'ffffff') return undefined;
  return { color: `#${hexColor}` };
}

/** Category → progress bar color */
export function categoryBarColor(category: string): string {
  switch (category.toLowerCase()) {
    case 'completed': return '#3fb950';
    case 'inprogress': return '#58a6ff';
    case 'proposed': return '#8b949e';
    case 'removed': return '#f85149';
    default: return '#30363d';
  }
}
