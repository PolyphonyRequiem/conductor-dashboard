import { useRef } from 'react';
import { useDashboardStore } from '@/stores/dashboard-store';
import { useUIStore } from '@/stores/ui-store';
import { useSSE } from '@/hooks/use-sse';
import { useKeyboardShortcuts } from '@/hooks/use-keyboard-shortcuts';
import { Header } from '@/components/layout/Header';
import { StatsBar } from '@/components/layout/StatsBar';
import { SearchBar } from '@/components/layout/SearchBar';
import { ActiveRunCard } from '@/components/runs/ActiveRunCard';
import { AbandonedSection } from '@/components/runs/AbandonedSection';
import { CompletedRunsTable } from '@/components/runs/CompletedRunsTable';
import { FailedRunsTable } from '@/components/runs/FailedRunsTable';
import { MetricsDashboard } from '@/components/metrics/MetricsDashboard';
import { KeyboardHelp } from '@/components/shared/KeyboardHelp';
import { ToastContainer } from '@/components/shared/Toast';
import type { RunData } from '@/types/dashboard';

function filterRuns(runs: RunData[], filterText: string): RunData[] {
  if (!filterText.trim()) return runs;
  const q = filterText.toLowerCase();
  return runs.filter(r =>
    (r.name || '').toLowerCase().includes(q) ||
    (r.work_item_id || '').toLowerCase().includes(q) ||
    (r.work_item_title || '').toLowerCase().includes(q) ||
    (r.purpose || '').toLowerCase().includes(q) ||
    (r.status || '').toLowerCase().includes(q) ||
    (r.run_id || '').toLowerCase().includes(q)
  );
}

export default function App() {
  useSSE();
  const data = useDashboardStore((s) => s.data);
  const filterText = useUIStore((s) => s.filterText);
  const searchRef = useRef<HTMLInputElement>(null);
  useKeyboardShortcuts(searchRef);

  if (!data) {
    return (
      <div className="p-5">
        <Header />
        <div className="text-[--color-text2] text-sm">Loading dashboard data...</div>
      </div>
    );
  }

  const activeRuns = filterRuns(data.active_runs, filterText);
  const abandonedRuns = filterRuns(data.abandoned_runs, filterText);
  const completedRuns = filterRuns(data.completed_runs, filterText);
  const failedRuns = filterRuns(data.failed_runs, filterText);

  return (
    <div className="p-5 max-w-[1400px] mx-auto">
      <Header />
      <StatsBar stats={data.stats} />
      <SearchBar ref={searchRef} />

      {/* Active Runs */}
      <h2 className="text-lg font-semibold text-[--color-accent] border-b border-[--color-border] pb-1.5 mb-3">
        Active Runs
      </h2>
      {activeRuns.length === 0 ? (
        <div className="bg-[--color-surface] border border-[--color-border] rounded-lg p-5 mb-5 text-[--color-text2]">
          {filterText ? 'No active runs match filter' : 'No active workflows'}
        </div>
      ) : (
        <div className="mb-5">
          {activeRuns.map((r, i) => (
            <ActiveRunCard key={r.log_file || `active-${i}`} run={r} index={i} keyPrefix="active" />
          ))}
        </div>
      )}

      {/* Abandoned Runs */}
      <AbandonedSection runs={abandonedRuns} />

      {/* Completed Runs */}
      <CompletedRunsTable runs={completedRuns} />

      {/* Failed Runs */}
      <FailedRunsTable runs={failedRuns} />

      {/* Metrics */}
      <MetricsDashboard />

      <KeyboardHelp />
      <ToastContainer />
    </div>
  );
}
