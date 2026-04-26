import { ChevronRight, ExternalLink, GitBranch, DollarSign, Zap, Tag, Clock } from 'lucide-react';
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

/** Thin labeled divider for enrichment groups — left-justified */
function GroupDivider({ label }: { label: string }) {
  return (
    <div className="flex items-center gap-2 text-[10px] uppercase tracking-wider text-[--color-text2] opacity-60 mt-1 mb-0.5 pl-1">
      <span>{label}</span>
      <span className="h-px flex-1" style={{ backgroundColor: '#484f58' }} />
    </div>
  );
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

  // --- Enrichment groups ---

  // Git group
  const gitBadges: React.ReactNode[] = [];
  if (run.worktree?.name) {
    gitBadges.push(
      <span key="wt" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-orange-900/30 border border-orange-700/40 text-orange-300 truncate max-w-[200px]">
        📦 <span className="truncate">{run.worktree.name}</span>
      </span>,
    );
  }
  if (run.worktree?.branch) {
    gitBadges.push(
      <span key="br" className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full bg-green-900/30 border border-green-700/40 text-green-300 truncate max-w-[300px]">
        <GitBranch size={10} className="shrink-0" />
        <span className="truncate">{run.worktree.branch}</span>
      </span>,
    );
  }

  // ADO group
  const adoBadges: React.ReactNode[] = [];

  // Work item title badge (moved from breadcrumbs)
  if (run.work_item_id) {
    const wiType = run.work_item_type || '';
    const typeColor = run.hierarchy?.type_colors?.[wiType];
    const hex = typeColor ? `#${typeColor}` : '#58a6ff';
    const iconId = run.hierarchy?.type_icons?.[wiType] ?? (wiType ? 'icon_clipboard' : '');
    const title = run.display_title && run.display_title !== `#${run.work_item_id}` ? run.display_title : '';
    const badge = (
      <span className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border truncate max-w-[400px]" style={{ borderColor: `${hex}40`, backgroundColor: `${hex}15`, color: hex }}>
        {iconId && <WorkItemIcon iconId={iconId} color={hex} size={12} />}
        <span className="font-medium">#{run.work_item_id}</span>
        {title && <span className="truncate opacity-80">{title}</span>}
        {run.work_item_url && <ExternalLink size={9} className="shrink-0 opacity-50" />}
      </span>
    );
    adoBadges.push(
      run.work_item_url ? (
        <a key="wi" href={run.work_item_url} target="_blank" rel="noopener noreferrer" className="hover:brightness-125 transition-all" onClick={(e) => e.stopPropagation()}>
          {badge}
        </a>
      ) : <span key="wi">{badge}</span>,
    );
  }

  // Tags
  const allTags = run.display_tags || [];
  if (allTags.length > 0) {
    const maxTags = 3;
    const visibleTags = allTags.slice(0, maxTags);
    for (const tag of visibleTags) {
      adoBadges.push(
        <span key={`tag-${tag}`} className="inline-flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded-full bg-purple-900/30 border border-purple-700/30 text-purple-300">
          <Tag size={8} className="shrink-0 opacity-60" />
          {tag}
        </span>,
      );
    }
    if (allTags.length > maxTags) {
      adoBadges.push(
        <span key="tag-overflow" className="text-[10px] px-1.5 py-0.5 rounded-full bg-purple-900/20 border border-purple-700/20 text-purple-400 tabular-nums" title={allTags.slice(maxTags).join(', ')}>
          +{allTags.length - maxTags}
        </span>,
      );
    }
  }

  // Hierarchy level badges
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

      const lvTypeColor = run.hierarchy.type_colors?.[lv.type];
      const lvHex = lvTypeColor ? `#${lvTypeColor}` : '#888';
      const lvIconId = run.hierarchy.type_icons?.[lv.type] ?? 'icon_clipboard';
      const unstartedCount = total - completedCount - inProgressCount;

      adoBadges.push(
        <span
          key={`lvl-${lv.type}`}
          className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border text-[--color-text2]"
          style={{ borderColor: `${lvHex}40`, backgroundColor: `${lvHex}15` }}
          title={`${lv.type}: ${completedCount} done, ${inProgressCount} in progress, ${unstartedCount} not started, ${total} total`}
        >
          <WorkItemIcon iconId={lvIconId} color={lvHex} size={12} />
          {total > 1 && (
            <>
              <span className="flex gap-px h-1.5 w-8 rounded overflow-hidden bg-[--color-border]">
                {completedCount > 0 && <span style={{ width: `${Math.round((completedCount / total) * 100)}%`, backgroundColor: '#3fb950' }} />}
                {inProgressCount > 0 && <span style={{ width: `${Math.round((inProgressCount / total) * 100)}%`, backgroundColor: '#58a6ff' }} />}
                {unstartedCount > 0 && <span style={{ width: `${Math.round((unstartedCount / total) * 100)}%`, backgroundColor: '#484f58' }} />}
              </span>
              <span className="tabular-nums">{completedCount}/{total}</span>
            </>
          )}
          {total === 1 && <span className="tabular-nums">1</span>}
        </span>,
      );
    }
  }

  const hasEnrichments = gitBadges.length > 0 || adoBadges.length > 0;

  const displayTitle = run.display_title || '';

  return (
    <div
      className={`relative bg-[--color-surface] border border-[--color-border] rounded-lg overflow-visible border-l-3 ${displayTitle ? 'mt-4' : ''} mb-3 ${borderClass} ${isAbandoned ? 'opacity-65' : ''}`}
    >
      {/* Title overlay on top border */}
      {displayTitle && (
        <span
          className="absolute -top-2.5 left-8 z-10 px-3 text-xs font-bold text-[--color-text] truncate max-w-[60%]"
          style={{ backgroundColor: '#161b22' }}
        >
          {displayTitle}
        </span>
      )}
      {/* Row 1: Breadcrumbs + metrics + status + stop */}
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
          {run.status === 'running' && run.started_at && (
            <span className="inline-flex items-center gap-1 text-[--color-text2] text-xs">
              <Clock size={10} />
              <DurationTicker startedAt={run.started_at} className="tabular-nums" />
            </span>
          )}
          {run.total_cost > 0 && (
            <span className="inline-flex items-center gap-0.5 text-xs text-yellow-300 tabular-nums">
              <DollarSign size={10} />
              {fmtCost2(run.total_cost)}
            </span>
          )}
          {run.total_tokens > 0 && (
            <span className="inline-flex items-center gap-0.5 text-xs text-cyan-300 tabular-nums">
              <Zap size={10} />
              {fmtTokens(run.total_tokens)}
            </span>
          )}
          {run.iteration > 1 && (
            <span className="text-xs text-purple-300 tabular-nums">
              iter {run.iteration}
            </span>
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

      {/* Enrichment groups */}
      {hasEnrichments && (
        <div className="px-4 pb-2.5 pl-10 space-y-1 cursor-pointer" onClick={() => toggleExpand(key)}>
          {gitBadges.length > 0 && (
            <>
              <GroupDivider label="git" />
              <div className="flex items-center gap-1.5 flex-wrap">{gitBadges}</div>
            </>
          )}
          {adoBadges.length > 0 && (
            <>
              <GroupDivider label="ado" />
              <div className="flex items-center gap-1.5 flex-wrap">{adoBadges}</div>
            </>
          )}
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
