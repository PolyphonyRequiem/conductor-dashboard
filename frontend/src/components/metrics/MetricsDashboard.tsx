import { useMemo } from 'react';
import { useDashboardStore } from '@/stores/dashboard-store';
import { useUIStore } from '@/stores/ui-store';
import { fmtCost, fmtCost2, fmtTokens, fmtDuration, fmtPercent } from '@/lib/format';
import { CostBurnChart } from './CostBurnChart';
import { RunTimelineChart } from './RunTimelineChart';
import type { RawRun, AgentMetric, WorkflowMetric, ModelMetric, TopAgent } from '@/types/dashboard';

type MetricsRange = '24h' | '7d' | '30d' | 'all';

function rangeCutoff(range: MetricsRange): number {
  const now = Date.now() / 1000;
  switch (range) {
    case '24h': return now - 86400;
    case '7d': return now - 604800;
    case '30d': return now - 2592000;
    default: return 0;
  }
}

function computeMetrics(runs: RawRun[], range: MetricsRange) {
  const cutoff = rangeCutoff(range);
  const filtered = runs.filter((r) => r.started_at >= cutoff);

  const byWorkflow: Record<string, WorkflowMetric> = {};
  const byModel: Record<string, ModelMetric> = {};
  const byAgent: Record<string, AgentMetric & { _elapsed_sum: number }> = {};
  let totalCost = 0, totalTokens = 0, totalRuns = 0, totalCompleted = 0, totalFailed = 0;

  for (const r of filtered) {
    totalRuns++;
    totalCost += r.total_cost;
    totalTokens += r.total_tokens;
    if (r.status === 'completed') totalCompleted++;
    else if (r.status === 'failed') totalFailed++;

    const wk = r.name || '(unknown)';
    if (!byWorkflow[wk]) byWorkflow[wk] = { runs: 0, completed: 0, failed: 0, total_cost: 0, total_tokens: 0, total_runtime_sec: 0, avg_duration_sec: 0, success_rate: 0 };
    const w = byWorkflow[wk]!;
    w.runs++;
    w.total_cost += r.total_cost;
    w.total_tokens += r.total_tokens;
    if (r.status === 'completed') w.completed++;
    else if (r.status === 'failed') w.failed++;
    if (r.duration_sec > 0) w.total_runtime_sec += r.duration_sec;

    for (const a of r.agents) {
      if (a.model) {
        if (!byModel[a.model]) byModel[a.model] = { cost: 0, tokens: 0, invocations: 0 };
        const m = byModel[a.model]!;
        m.cost += a.cost_usd;
        m.tokens += a.tokens;
        m.invocations++;
      }
      if (a.name) {
        if (!byAgent[a.name]) byAgent[a.name] = { invocations: 0, total_cost: 0, total_tokens: 0, total_elapsed: 0, avg_elapsed: 0, _elapsed_sum: 0 };
        const ag = byAgent[a.name]!;
        ag.invocations++;
        ag.total_cost += a.cost_usd;
        ag.total_tokens += a.tokens;
        ag._elapsed_sum += a.elapsed;
      }
    }
  }

  // Finalize averages
  for (const w of Object.values(byWorkflow)) {
    const completedWithDuration = w.total_runtime_sec > 0 ? w.runs : 0;
    w.avg_duration_sec = completedWithDuration > 0 ? w.total_runtime_sec / completedWithDuration : 0;
    w.success_rate = w.runs > 0 ? w.completed / w.runs : 0;
  }
  for (const ag of Object.values(byAgent)) {
    ag.total_elapsed = ag._elapsed_sum;
    ag.avg_elapsed = ag.invocations > 0 ? ag._elapsed_sum / ag.invocations : 0;
  }

  const topAgents: TopAgent[] = Object.entries(byAgent)
    .map(([name, v]) => ({ name, total_cost: v.total_cost, invocations: v.invocations, total_tokens: v.total_tokens }))
    .sort((a, b) => b.total_cost - a.total_cost)
    .slice(0, 10);

  return {
    byWorkflow: Object.fromEntries(Object.entries(byWorkflow).sort((a, b) => b[1].total_cost - a[1].total_cost)),
    byModel: Object.fromEntries(Object.entries(byModel).sort((a, b) => b[1].cost - a[1].cost)),
    byAgent: Object.fromEntries(Object.entries(byAgent).sort((a, b) => b[1].total_cost - a[1].total_cost)),
    topAgents,
    totals: { cost: totalCost, tokens: totalTokens, runs: totalRuns, completed: totalCompleted, failed: totalFailed },
  };
}

const RANGES: MetricsRange[] = ['24h', '7d', '30d', 'all'];

export function MetricsDashboard() {
  const runsRaw = useDashboardStore((s) => s.data?.runs_raw ?? []);
  const range = useUIStore((s) => s.metricsRange);
  const setRange = useUIStore((s) => s.setMetricsRange);

  const metrics = useMemo(() => computeMetrics(runsRaw, range), [runsRaw, range]);
  const filteredRuns = useMemo(() => {
    const cutoff = rangeCutoff(range);
    return runsRaw.filter((r) => r.started_at >= cutoff);
  }, [runsRaw, range]);

  return (
    <div>
      <h2 className="text-lg font-semibold text-[--color-accent] border-b border-[--color-border] pb-1.5 mb-3 flex items-center gap-2.5">
        Metrics
        <div className="flex gap-1 ml-auto">
          {RANGES.map((r) => (
            <button
              key={r}
              onClick={() => setRange(r)}
              className={`text-xs px-2.5 py-1 rounded border ${
                r === range
                  ? 'bg-[--color-accent]/10 border-[--color-accent]/30 text-[--color-accent]'
                  : 'border-[--color-border] text-[--color-text2] hover:bg-[--color-surface-hover]'
              }`}
            >
              {r}
            </button>
          ))}
        </div>
      </h2>

      {/* Totals bar */}
      <div className="flex gap-4 flex-wrap mb-4 text-sm">
        <span>Runs: <strong>{metrics.totals.runs}</strong></span>
        <span className="text-[--color-green]">Completed: <strong>{metrics.totals.completed}</strong></span>
        <span className="text-[--color-red]">Failed: <strong>{metrics.totals.failed}</strong></span>
        <span>Cost: <strong>{fmtCost2(metrics.totals.cost)}</strong></span>
        <span>Tokens: <strong>{fmtTokens(metrics.totals.tokens)}</strong></span>
      </div>

      {/* Charts */}
      <CostBurnChart runs={filteredRuns} />
      <RunTimelineChart runs={filteredRuns} />

      {/* By Workflow */}
      <MetricsTable
        title="By Workflow"
        headers={['Workflow', 'Runs', 'OK', 'Fail', 'Success%', 'Total Runtime', 'Avg Duration', 'Cost', 'Tokens']}
        rows={Object.entries(metrics.byWorkflow).map(([name, w]) => [
          name, String(w.runs), String(w.completed), String(w.failed),
          fmtPercent(w.success_rate), fmtDuration(w.total_runtime_sec),
          fmtDuration(w.avg_duration_sec), fmtCost(w.total_cost), fmtTokens(w.total_tokens),
        ])}
      />

      {/* By Model */}
      <MetricsTable
        title="By Model"
        headers={['Model', 'Cost', 'Tokens', 'Invocations']}
        rows={Object.entries(metrics.byModel).map(([name, m]) => [
          name, fmtCost(m.cost), fmtTokens(m.tokens), String(m.invocations),
        ])}
      />

      {/* By Agent */}
      <MetricsTable
        title="By Agent"
        headers={['Agent', 'Invocations', 'Cost', 'Tokens', 'Total Elapsed', 'Avg Elapsed']}
        rows={Object.entries(metrics.byAgent).map(([name, a]) => [
          name, String(a.invocations), fmtCost(a.total_cost), fmtTokens(a.total_tokens),
          fmtDuration(a.total_elapsed), fmtDuration(a.avg_elapsed),
        ])}
      />

      {/* Top Agents by Cost */}
      {metrics.topAgents.length > 0 && (
        <MetricsTable
          title="Top Agents by Cost"
          headers={['Agent', 'Cost', 'Invocations', 'Tokens']}
          rows={metrics.topAgents.map((a) => [
            a.name, fmtCost(a.total_cost), String(a.invocations), fmtTokens(a.total_tokens),
          ])}
        />
      )}
    </div>
  );
}

function MetricsTable({ title, headers, rows }: { title: string; headers: string[]; rows: string[][] }) {
  if (rows.length === 0) return null;
  return (
    <div className="mb-4">
      <h3 className="text-sm font-semibold text-[--color-text2] mb-1.5">{title}</h3>
      <table className="w-full border-collapse bg-[--color-surface] border border-[--color-border] rounded-lg overflow-hidden text-sm">
        <thead>
          <tr className="bg-[#1c2128]">
            {headers.map((h, i) => (
              <th key={i} className={`px-3 py-2 text-xs uppercase tracking-wide text-[--color-text2] font-semibold ${i === 0 ? 'text-left' : 'text-right'}`}>
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, ri) => (
            <tr key={ri} className="border-t border-[--color-border]">
              {row.map((cell, ci) => (
                <td key={ci} className={`px-3 py-1.5 ${ci === 0 ? 'text-left font-medium' : 'text-right'}`}>
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
