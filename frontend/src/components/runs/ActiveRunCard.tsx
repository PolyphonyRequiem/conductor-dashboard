import { ChevronRight, ExternalLink, GitBranch, Hash, Layers, DollarSign, Zap } from 'lucide-react';
import { useUIStore } from '@/stores/ui-store';
import { PowerlineBreadcrumbs } from './PowerlineBreadcrumbs';
import { RunDetailPanel } from './RunDetailPanel';
import { DurationTicker } from '@/components/shared/DurationTicker';
import { fmtCost2, fmtTokens } from '@/lib/format';
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

  // Enrichment badges for Row 2
  const badges: React.ReactNode[] = [];

  if (run.work_item_id) {
    const wiLabel = `${run.work_item_type ? `${run.work_item_type} ` : ''}#${run.work_item_id}${run.work_item_title ? ` ${run.work_item_title}` : ''}`;
    if (run.work_item_url) {
      badges.push(
        <a
          key="wi"
          href={run.work_item_url}
          target="_blank"
          rel="noopener noreferrer"
          className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-blue-900/30 border border-blue-700/40 text-blue-300 hover:bg-blue-900/50 transition-colors truncate max-w-[340px]"
          onClick={(e) => e.stopPropagation()}
        >
          <Hash size={10} className="shrink-0" />
          <span className="truncate">{wiLabel}</span>
          <ExternalLink size={9} className="shrink-0 opacity-60" />
        </a>,
      );
    } else {
      badges.push(
        <span key="wi" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-blue-900/30 border border-blue-700/40 text-blue-300 truncate max-w-[340px]">
          <Hash size={10} className="shrink-0" />
          <span className="truncate">{wiLabel}</span>
        </span>,
      );
    }
  }

  if (run.worktree?.branch) {
    badges.push(
      <span key="br" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-green-900/30 border border-green-700/40 text-green-300 truncate max-w-[300px]">
        <GitBranch size={10} className="shrink-0" />
        <span className="truncate">{run.worktree.branch}</span>
      </span>,
    );
  }

  if (run.iteration > 1) {
    badges.push(
      <span key="it" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-purple-900/30 border border-purple-700/40 text-purple-300">
        <Layers size={10} />
        Iter {run.iteration}
      </span>,
    );
  }

  if (run.total_cost > 0) {
    badges.push(
      <span key="cost" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-yellow-900/20 border border-yellow-700/30 text-yellow-300">
        <DollarSign size={10} />
        {fmtCost2(run.total_cost)}
      </span>,
    );
  }

  if (run.total_tokens > 0) {
    badges.push(
      <span key="tok" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-cyan-900/20 border border-cyan-700/30 text-cyan-300">
        <Zap size={10} />
        {fmtTokens(run.total_tokens)}
      </span>,
    );
  }

  if (run.hierarchy?.progress) {
    const p = run.hierarchy.progress;
    const total = p.done + p.doing + p.todo;
    if (total > 0) {
      badges.push(
        <span key="prog" className="inline-flex items-center gap-1.5 text-xs px-2 py-0.5 rounded-full bg-[--color-surface] border border-[--color-border] text-[--color-text2]">
          <span className="flex gap-px h-1.5 w-12 rounded overflow-hidden">
            {p.done > 0 && <span className="bg-[--color-green]" style={{ flex: p.done }} />}
            {p.doing > 0 && <span className="bg-[--color-yellow]" style={{ flex: p.doing }} />}
            {p.todo > 0 && <span className="bg-[--color-border]" style={{ flex: p.todo }} />}
          </span>
          {p.done}/{total}
        </span>,
      );
    }
  }

  return (
    <div
      className={`bg-[--color-surface] border border-[--color-border] rounded-lg overflow-hidden border-l-3 mb-3 ${borderClass} ${isAbandoned ? 'opacity-65' : ''}`}
    >
      {/* Row 1: Breadcrumbs + runtime + status */}
      <div
        className="flex items-center gap-3 px-4 py-2.5 cursor-pointer hover:bg-[--color-surface-hover] transition-colors"
        onClick={() => toggleExpand(key)}
      >
        <ChevronRight
          size={14}
          className={`text-[--color-text2] transition-transform shrink-0 ${isExpanded ? 'rotate-90' : ''}`}
        />
        <PowerlineBreadcrumbs run={run} />
        <div className="flex items-center gap-3 ml-auto shrink-0">
          {run.status === 'running' && run.started_at && (
            <DurationTicker startedAt={run.started_at} className="text-[--color-text2] text-sm tabular-nums" />
          )}
          {statusLabel}
        </div>
      </div>

      {/* Row 2: Enrichment badges (always visible when present) */}
      {badges.length > 0 && (
        <div
          className="flex items-center gap-1.5 px-4 pb-2.5 pl-10 flex-wrap cursor-pointer"
          onClick={() => toggleExpand(key)}
        >
          {badges}
        </div>
      )}

      {/* Expanded detail panel */}
      <div
        className={`overflow-hidden transition-[max-height] duration-300 ${isExpanded ? 'max-h-[2000px]' : 'max-h-0'}`}
      >
        <RunDetailPanel run={run} />
      </div>
    </div>
  );
}
