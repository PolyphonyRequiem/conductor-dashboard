import { useDashboardStore } from '@/stores/dashboard-store';
import { useSSE } from '@/hooks/use-sse';
import { Header } from '@/components/layout/Header';
import { StatsBar } from '@/components/layout/StatsBar';
import { ActiveRunCard } from '@/components/runs/ActiveRunCard';
import { AbandonedSection } from '@/components/runs/AbandonedSection';
import { CompletedRunsTable } from '@/components/runs/CompletedRunsTable';
import { FailedRunsTable } from '@/components/runs/FailedRunsTable';
import { MetricsDashboard } from '@/components/metrics/MetricsDashboard';
import { ToastContainer } from '@/components/shared/Toast';

export default function App() {
  useSSE();
  const data = useDashboardStore((s) => s.data);

  if (!data) {
    return (
      <div className="p-5">
        <Header />
        <div className="text-[--color-text2] text-sm">Loading dashboard data...</div>
      </div>
    );
  }

  return (
    <div className="p-5 max-w-[1400px] mx-auto">
      <Header />
      <StatsBar stats={data.stats} />

      {/* Active Runs */}
      <h2 className="text-lg font-semibold text-[--color-accent] border-b border-[--color-border] pb-1.5 mb-3">
        Active Runs
      </h2>
      {data.active_runs.length === 0 ? (
        <div className="bg-[--color-surface] border border-[--color-border] rounded-lg p-5 mb-5 text-[--color-text2]">
          No active workflows
        </div>
      ) : (
        <div className="mb-5">
          {data.active_runs.map((r, i) => (
            <ActiveRunCard key={r.log_file || `active-${i}`} run={r} index={i} keyPrefix="active" />
          ))}
        </div>
      )}

      {/* Abandoned Runs */}
      <AbandonedSection runs={data.abandoned_runs} />

      {/* Completed Runs */}
      <CompletedRunsTable runs={data.completed_runs} />

      {/* Failed Runs */}
      <FailedRunsTable runs={data.failed_runs} />

      {/* Metrics */}
      <MetricsDashboard />

      <ToastContainer />
    </div>
  );
}
