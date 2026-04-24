import { useMemo } from 'react';
import {
  BarChart, Bar, XAxis, YAxis, Tooltip,
  ResponsiveContainer, CartesianGrid, Cell,
} from 'recharts';
import type { RawRun } from '@/types/dashboard';
import { fmtDuration } from '@/lib/format';

const STATUS_COLORS: Record<string, string> = {
  completed: '#3fb950',
  failed: '#f85149',
  running: '#58a6ff',
  interrupted: '#d29922',
  timeout: '#d29922',
  unknown: '#8b949e',
  parse_error: '#8b949e',
};

interface Props {
  runs: RawRun[];
}

export function RunTimelineChart({ runs }: Props) {
  const data = useMemo(() => {
    if (runs.length === 0) return [];

    const now = Date.now() / 1000;

    // Take the most recent 30 runs that have start times
    const recent = [...runs]
      .filter((r) => r.started_at > 0)
      .sort((a, b) => b.started_at - a.started_at)
      .slice(0, 30)
      .reverse();

    if (recent.length === 0) return [];

    const minStart = recent[0]!.started_at;

    return recent.map((r, i) => {
      const duration = r.duration_sec > 0
        ? r.duration_sec
        : (r.ended_at && r.ended_at > r.started_at)
          ? r.ended_at - r.started_at
          : (r.status === 'running' ? now - r.started_at : 0);

      return {
        label: `${r.name || '?'}${r.run_id ? ` (${r.run_id.slice(0, 6)})` : ''}`,
        start: r.started_at - minStart,
        duration: Math.max(duration, 1), // at least 1s for visibility
        status: r.status,
        name: r.name || '(unknown)',
        _index: i,
      };
    });
  }, [runs]);

  if (data.length < 2) return null;

  return (
    <div className="mb-4">
      <h3 className="text-sm font-semibold text-[--color-text2] mb-2">Run Timeline (recent 30)</h3>
      <div className="bg-[--color-surface] border border-[--color-border] rounded-lg p-3" style={{ height: Math.max(200, data.length * 24 + 60) }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={data}
            layout="vertical"
            margin={{ top: 5, right: 20, bottom: 5, left: 10 }}
            barSize={14}
          >
            <CartesianGrid strokeDasharray="3 3" stroke="#30363d" horizontal={false} />
            <XAxis
              type="number"
              tick={{ fill: '#8b949e', fontSize: 11 }}
              tickLine={{ stroke: '#30363d' }}
              tickFormatter={(v: number) => fmtDuration(v)}
              domain={[0, 'dataMax']}
            />
            <YAxis
              type="category"
              dataKey="label"
              width={180}
              tick={{ fill: '#8b949e', fontSize: 10 }}
              tickLine={false}
            />
            <Tooltip
              contentStyle={{ backgroundColor: '#161b22', border: '1px solid #30363d', borderRadius: 6, fontSize: 12 }}
              labelStyle={{ color: '#e6edf3' }}
              // eslint-disable-next-line @typescript-eslint/no-explicit-any
              formatter={((value: unknown, _name: unknown, props: any) => {
                return [fmtDuration(Number(value)), `Duration (${props?.payload?.status ?? ''})`];
              }) as any}
            />
            {/* Invisible bar for start offset */}
            <Bar dataKey="start" stackId="timeline" fill="transparent" radius={0} />
            {/* Visible bar for duration */}
            <Bar dataKey="duration" stackId="timeline" radius={[0, 4, 4, 0]}>
              {data.map((entry, index) => (
                <Cell
                  key={index}
                  fill={STATUS_COLORS[entry.status] || '#8b949e'}
                  fillOpacity={0.85}
                />
              ))}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="flex gap-3 mt-1.5 text-[10px] text-[--color-text2]">
        {Object.entries(STATUS_COLORS).slice(0, 4).map(([status, color]) => (
          <span key={status} className="flex items-center gap-1">
            <span className="inline-block w-2.5 h-2.5 rounded-sm" style={{ backgroundColor: color }} />
            {status}
          </span>
        ))}
      </div>
    </div>
  );
}
