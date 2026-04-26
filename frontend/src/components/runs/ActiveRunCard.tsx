import { ChevronRight, GitBranch, DollarSign, Zap } from 'lucide-react';
import { useUIStore } from '@/stores/ui-store';
import { PowerlineBreadcrumbs } from './PowerlineBreadcrumbs';
import { RunDetailPanel } from './RunDetailPanel';
import { DurationTicker } from '@/components/shared/DurationTicker';
import { WorkItemIcon } from '@/components/shared/WorkItemIcon';
import { ConfirmButton } from '@/components/shared/ConfirmButton';
import { actionStop } from '@/lib/api';
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

  if (run.worktree?.name) {
    badges.push(
      <span key="wt" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-orange-900/30 border border-orange-700/40 text-orange-300 truncate max-w-[200px]">
        📦 <span className="truncate">{run.worktree.name}</span>
      </span>,
    );
  }

  if (run.worktree?.branch) {
    badges.push(
      <span key="br" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-green-900/30 border border-green-700/40 text-green-300 truncate max-w-[300px]">
        <GitBranch size={10} className="shrink-0" />
        <span className="truncate">{run.worktree.branch}</span>
      </span>,
    );
  }

  if (run.hierarchy?.levels && run.hierarchy.levels.length > 0) {
    for (const lv of run.hierarchy.levels) {
      const total = lv.total || 0;
      if (total === 0) continue;

      const typeDefs = run.hierarchy.type_defs?.[lv.type] ?? [];
      const completedStates = typeDefs
        .filter((d) => d.category === 'Completed')
        .map((d) => d.name);
      const completedCount = completedStates.length > 0
        ? completedStates.reduce((sum, name) => sum + (lv.states[name] ?? 0), 0)
        : Object.entries(lv.states)
            .filter(([name]) => ['done', 'completed', 'closed', 'resolved'].includes(name.toLowerCase()))
            .reduce((sum, [, cnt]) => sum + cnt, 0);
      const inProgressStates = typeDefs
        .filter((d) => d.category === 'InProgress')
        .map((d) => d.name);
      const inProgressCount = inProgressStates.length > 0
        ? inProgressStates.reduce((sum, name) => sum + (lv.states[name] ?? 0), 0)
        : Object.entries(lv.states)
            .filter(([name]) => ['doing', 'started', 'active', 'committed', 'in progress'].includes(name.toLowerCase()))
            .reduce((sum, [, cnt]) => sum + cnt, 0);

      const typeColor = run.hierarchy.type_colors?.[lv.type];
      const hexColor = typeColor ? `#${typeColor}` : '#888';
      const iconId = run.hierarchy.type_icons?.[lv.type] ?? 'icon_clipboard';

      // If nothing is started or completed, dim the whole badge
      const allUnstarted = completedCount === 0 && inProgressCount === 0;

      badges.push(
        <span
          key={`lvl-${lv.type}`}
          className={`inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border ${allUnstarted ? 'text-[--color-text2] opacity-50' : 'text-[--color-text2]'}`}
          style={{
            borderColor: allUnstarted ? 'var(--color-border)' : `${hexColor}40`,
            backgroundColor: allUnstarted ? 'transparent' : `${hexColor}15`,
          }}
          title={`${lv.type}: ${completedCount} done, ${inProgressCount} in progress, ${total - completedCount - inProgressCount} not started, ${total} total`}
        >
          <WorkItemIcon iconId={iconId} color={allUnstarted ? '#555' : hexColor} size={12} />
          {total > 1 && (
            <>
              <span className="flex gap-px h-1.5 w-8 rounded overflow-hidden bg-[--color-border]">
                {completedCount > 0 && (
                  <span style={{ width: `${Math.round((completedCount / total) * 100)}%`, backgroundColor: '#3fb950' }} />
                )}
                {inProgressCount > 0 && (
                  <span style={{ width: `${Math.round((inProgressCount / total) * 100)}%`, backgroundColor: '#58a6ff' }} />
                )}
              </span>
              <span className="tabular-nums">{completedCount}/{total}</span>
            </>
          )}
          {total === 1 && <span className="tabular-nums">1</span>}
        </span>,
      );
    }
  }

  // Title-line inline metrics
  const titleMetrics: React.ReactNode[] = [];

  if (run.iteration > 1) {
    titleMetrics.push(
      <span key="it" className="text-xs text-purple-300 tabular-nums">
        iter {run.iteration}
      </span>,
    );
  }

  if (run.total_cost > 0) {
    titleMetrics.push(
      <span key="cost" className="inline-flex items-center gap-0.5 text-xs text-yellow-300 tabular-nums">
        <DollarSign size={10} />
        {fmtCost2(run.total_cost)}
      </span>,
    );
  }

  if (run.total_tokens > 0) {
    titleMetrics.push(
      <span key="tok" className="inline-flex items-center gap-0.5 text-xs text-cyan-300 tabular-nums">
        <Zap size={10} />
        {fmtTokens(run.total_tokens)}
      </span>,
    );
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
        <div className="flex items-center gap-2.5 ml-auto shrink-0">
          {titleMetrics.length > 0 && (
            <span className="flex items-center gap-2 border-r border-[--color-border] pr-2.5">
              {titleMetrics}
            </span>
          )}
          {run.status === 'running' && run.started_at && (
            <DurationTicker startedAt={run.started_at} className="text-[--color-text2] text-sm tabular-nums" />
          )}
          {statusLabel}
          <span onClick={(e) => e.stopPropagation()}>
            <ConfirmButton
              label={isAbandoned ? 'Dismiss' : 'Stop'}
              colorClass="border-red-600/40 text-red-400 hover:bg-red-900/20"
              onClick={() => actionStop(run.log_file)}
              successMessage={isAbandoned ? '🗑️ Run dismissed' : '🛑 Process stopped'}
            />
          </span>
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
