import { create } from 'zustand';

type MetricsRange = '24h' | '7d' | '30d' | 'all';

interface UIState {
  /** Set of expanded run card keys */
  expandedRuns: Set<string>;
  /** Set of reviewed run log files */
  reviewedRuns: Set<string>;
  /** Show/hide reviewed completed runs */
  showReviewedCompleted: boolean;
  /** Show/hide reviewed failed runs */
  showReviewedFailed: boolean;
  /** Show/hide abandoned runs */
  showAbandoned: boolean;
  /** Metrics time range filter */
  metricsRange: MetricsRange;
  /** Search/filter text for runs */
  filterText: string;
  /** Whether keyboard help overlay is visible */
  showHelp: boolean;
  /** Actions */
  toggleExpand: (key: string) => void;
  collapseAll: () => void;
  toggleReviewed: (logFile: string) => void;
  setShowReviewedCompleted: (show: boolean) => void;
  setShowReviewedFailed: (show: boolean) => void;
  setShowAbandoned: (show: boolean) => void;
  setMetricsRange: (range: MetricsRange) => void;
  setFilterText: (text: string) => void;
  toggleHelp: () => void;
}

function loadSet(key: string): Set<string> {
  try {
    const raw = localStorage.getItem(key);
    if (raw) return new Set(JSON.parse(raw) as string[]);
  } catch { /* ignore */ }
  return new Set();
}

function saveSet(key: string, s: Set<string>) {
  localStorage.setItem(key, JSON.stringify([...s]));
}

function loadBool(key: string, fallback: boolean): boolean {
  const v = localStorage.getItem(key);
  if (v === null) return fallback;
  return v === '1';
}

export const useUIStore = create<UIState>((set) => ({
  expandedRuns: new Set(),
  reviewedRuns: loadSet('conductor-reviewed-runs'),
  showReviewedCompleted: false,
  showReviewedFailed: false,
  showAbandoned: loadBool('conductor-show-abandoned', false),
  metricsRange: (localStorage.getItem('conductor-metrics-range') as MetricsRange) || '24h',
  filterText: '',
  showHelp: false,

  toggleExpand: (key) => set((state) => {
    const next = new Set(state.expandedRuns);
    if (next.has(key)) next.delete(key); else next.add(key);
    return { expandedRuns: next };
  }),

  collapseAll: () => set({ expandedRuns: new Set() }),

  toggleReviewed: (logFile) => set((state) => {
    const next = new Set(state.reviewedRuns);
    if (next.has(logFile)) next.delete(logFile); else next.add(logFile);
    saveSet('conductor-reviewed-runs', next);
    return { reviewedRuns: next };
  }),

  setShowReviewedCompleted: (show) => set({ showReviewedCompleted: show }),
  setShowReviewedFailed: (show) => set({ showReviewedFailed: show }),

  setShowAbandoned: (show) => {
    localStorage.setItem('conductor-show-abandoned', show ? '1' : '0');
    set({ showAbandoned: show });
  },

  setMetricsRange: (range) => {
    localStorage.setItem('conductor-metrics-range', range);
    set({ metricsRange: range });
  },

  setFilterText: (text) => set({ filterText: text }),
  toggleHelp: () => set((state) => ({ showHelp: !state.showHelp })),
}));
