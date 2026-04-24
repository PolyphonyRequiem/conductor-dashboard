import { useMemo } from 'react';
import {
  LineChart, Line, XAxis, YAxis, Tooltip, Legend,
  ResponsiveContainer, CartesianGrid,
} from 'recharts';
import type { RawRun } from '@/types/dashboard';
import { fmtCost2 } from '@/lib/format';

// Accent colors for top workflows
const COLORS = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#bc8cff', '#79c0ff', '#56d364'];

interface Props {
  runs: RawRun[];
}

interface DataPoint {
  time: number;
  label: string;
  [workflow: string]: number | string;
}

export function CostBurnChart({ runs }: Props) {
  const { data, workflows } = useMemo(() => {
    if (runs.length === 0) return { data: [], workflows: [] };

    // Find top 5 workflows by cost
    const wfCost: Record<string, number> = {};
    for (const r of runs) {
      const name = r.name || '(unknown)';
      wfCost[name] = (wfCost[name] || 0) + r.total_cost;
    }
    const topWfs = Object.entries(wfCost)
      .sort((a, b) => b[1] - a[1])
      .slice(0, 5)
      .map(([name]) => name);

    // Sort runs by started_at, build cumulative series
    const sorted = [...runs]
      .filter((r) => r.started_at > 0 && r.total_cost > 0)
      .sort((a, b) => a.started_at - b.started_at);

    if (sorted.length === 0) return { data: [], workflows: [] };

    const cumulative: Record<string, number> = {};
    for (const wf of topWfs) cumulative[wf] = 0;
    cumulative['_total'] = 0;

    const points: DataPoint[] = [];
    for (const r of sorted) {
      const name = r.name || '(unknown)';
      // Use ended_at for realized cost, or started_at as fallback
      const ts = r.ended_at ?? r.started_at;
      cumulative['_total'] = (cumulative['_total'] ?? 0) + r.total_cost;
      if (topWfs.includes(name)) {
        cumulative[name] = (cumulative[name] ?? 0) + r.total_cost;
      }
      const point: DataPoint = {
        time: ts,
        label: new Date(ts * 1000).toLocaleDateString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }),
      };
      point['Total'] = Math.round((cumulative['_total'] ?? 0) * 100) / 100;
      for (const wf of topWfs) {
        point[wf] = Math.round((cumulative[wf] ?? 0) * 100) / 100;
      }
      points.push(point);
    }

    return { data: points, workflows: topWfs };
  }, [runs]);

  if (data.length < 2) return null;

  return (
    <div className="mb-4">
      <h3 className="text-sm font-semibold text-[--color-text2] mb-2">Cumulative Cost Over Time</h3>
      <div className="bg-[--color-surface] border border-[--color-border] rounded-lg p-3" style={{ height: 280 }}>
        <ResponsiveContainer width="100%" height="100%">
          <LineChart data={data} margin={{ top: 5, right: 20, bottom: 5, left: 10 }}>
            <CartesianGrid strokeDasharray="3 3" stroke="#30363d" />
            <XAxis
              dataKey="label"
              tick={{ fill: '#8b949e', fontSize: 11 }}
              tickLine={{ stroke: '#30363d' }}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fill: '#8b949e', fontSize: 11 }}
              tickLine={{ stroke: '#30363d' }}
              tickFormatter={(v: number) => `$${v}`}
            />
            <Tooltip
              contentStyle={{ backgroundColor: '#161b22', border: '1px solid #30363d', borderRadius: 6, fontSize: 12 }}
              labelStyle={{ color: '#e6edf3' }}
              formatter={(value: unknown) => fmtCost2(Number(value))}
            />
            <Legend
              wrapperStyle={{ fontSize: 11, color: '#8b949e' }}
            />
            <Line
              type="monotone"
              dataKey="Total"
              stroke="#e6edf3"
              strokeWidth={2}
              dot={false}
            />
            {workflows.map((wf, i) => (
              <Line
                key={wf}
                type="monotone"
                dataKey={wf}
                stroke={COLORS[i % COLORS.length]}
                strokeWidth={1.5}
                dot={false}
                strokeDasharray={i > 2 ? '5 3' : undefined}
              />
            ))}
          </LineChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}
