import { ChevronRight } from 'lucide-react';
import { useUIStore } from '@/stores/ui-store';
import { PowerlineBreadcrumbs } from './PowerlineBreadcrumbs';
import { RunDetailPanel } from './RunDetailPanel';
import { DurationTicker } from '@/components/shared/DurationTicker';
import type { RunData } from '@/types/dashboard';

interface Props {
  run: RunData;
  index: number;
  keyPrefix: string;
}

export function ActiveRunCard({ run, index, keyPrefix }: Props) {
  const expandedRuns = useUIStore((s) => s.expandedRuns);
  const toggleExpand = useUIStore((s) => s.toggleExpand);

  const key = run.log_file || `${keyPrefix}-${index}`;
  const isExpanded = expandedRuns.has(key);
  const isAbandoned = !run.process_alive;

  let borderClass = 'border-l-[--color-border]';
  if (run.gate_waiting && isAbandoned) borderClass = 'border-l-[--color-red]';
  else if (run.gate_waiting) borderClass = 'border-l-[--color-yellow]';
  else if (isAbandoned) borderClass = 'border-l-[--color-red]';

  const statusLabel = run.gate_waiting ? (
    isAbandoned ? (
      <span className="px-2 py-0.5 rounded text-xs font-bold bg-red-900/60 text-red-300">GATE ABANDONED</span>
    ) : (
      <span className="flex items-center gap-1.5">
        <span className="animate-pulse">🚨</span>
        <span className="px-2 py-0.5 rounded text-xs font-semibold bg-yellow-900/40 text-yellow-300">
          Human Gate — {run.gate_agent}
        </span>
      </span>
    )
  ) : isAbandoned ? (
    <span className="px-2 py-0.5 rounded text-xs font-bold bg-red-900/60 text-red-300">ABANDONED</span>
  ) : run.current_agent ? (
    <span className="px-2 py-0.5 rounded text-xs font-semibold bg-green-900/40 text-green-300">Running</span>
  ) : (
    <span className="px-2 py-0.5 rounded text-xs font-semibold bg-blue-900/40 text-blue-300">Idle</span>
  );

  const worktree = run.worktree;
  const worktreeBadge = worktree?.branch ? (
    <span className="text-xs text-[--color-text2] bg-[--color-surface] border border-[--color-border] rounded px-1.5 py-0.5 shrink-0">
      🌿 {worktree.branch}
    </span>
  ) : null;

  return (
    <div
      className={`bg-[--color-surface] border border-[--color-border] rounded-lg overflow-hidden border-l-3 mb-3 ${borderClass} ${isAbandoned ? 'opacity-65' : ''}`}
    >
      {/* Header row — clickable */}
      <div
        className="flex items-center gap-3 px-4 py-3 cursor-pointer hover:bg-[--color-surface-hover] transition-colors"
        onClick={() => toggleExpand(key)}
      >
        <ChevronRight
          size={14}
          className={`text-[--color-text2] transition-transform ${isExpanded ? 'rotate-90' : ''}`}
        />
        <PowerlineBreadcrumbs run={run} />
        {worktreeBadge}
        {run.status === 'running' && run.started_at && (
          <DurationTicker startedAt={run.started_at} className="text-[--color-text2] ml-auto text-sm" />
        )}
        <div className="ml-auto">{statusLabel}</div>
      </div>

      {/* Expandable detail panel */}
      <div
        className={`overflow-hidden transition-[max-height] duration-300 ${isExpanded ? 'max-h-[1000px]' : 'max-h-0'}`}
      >
        <RunDetailPanel run={run} />
      </div>
    </div>
  );
}
