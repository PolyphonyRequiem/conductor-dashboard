import type { DashboardStats } from '@/types/dashboard';

interface Props {
  stats: DashboardStats;
}

function Stat({ label, value, color }: { label: string; value: number | string; color: string }) {
  return (
    <div className="bg-[--color-surface] border border-[--color-border] rounded-lg px-5 py-3.5 min-w-[140px]">
      <div className="text-xs uppercase tracking-wide text-[--color-text2]">{label}</div>
      <div className={`text-2xl font-semibold mt-1 ${color}`}>{value}</div>
    </div>
  );
}

export function StatsBar({ stats }: Props) {
  return (
    <div className="flex gap-4 flex-wrap mb-5">
      <Stat label="Active Now" value={stats.active} color="text-[--color-yellow]" />
      <Stat label="Gates Waiting" value={stats.gates_waiting} color="text-[--color-yellow]" />
      <Stat label="Abandoned" value={stats.abandoned} color="text-[--color-red]" />
    </div>
  );
}
