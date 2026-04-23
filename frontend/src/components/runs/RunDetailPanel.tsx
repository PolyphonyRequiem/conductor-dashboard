import { ExternalLink, GitBranch, Copy, Layers } from 'lucide-react';
import type { RunData, TypeStateDef } from '@/types/dashboard';
import { fmtCost, fmtTokens, fmtDuration, stateBadgeClass, categoryBarColor } from '@/lib/format';
import { useConductorWs } from '@/hooks/use-conductor-ws';
import { EmbeddedWorkflowGraph } from '@/components/graph/EmbeddedWorkflowGraph';
import { toast } from '@/components/shared/Toast';

interface Props {
  run: RunData;
}

export function RunDetailPanel({ run }: Props) {
  const hierarchy = run.hierarchy;
  const subworkflows = run.subworkflows || [];
  const isLive = run.process_alive && !!run.dashboard_port;

  const { events, connected } = useConductorWs({
    logFile: run.log_file,
    enabled: isLive,
  });

  const copyReplayCmd = () => {
    if (run.replay_cmd) {
      navigator.clipboard.writeText(run.replay_cmd);
      toast('📋 Replay command copied', 'ok');
    }
  };

  return (
    <div className="border-t border-[--color-border] px-4 py-3 text-sm space-y-4">
      {/* Top action bar: Conductor UI + Replay */}
      <div className="flex items-center gap-3 flex-wrap">
        {run.dashboard_url && (
          <a
            href={run.dashboard_url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-[--color-accent]/10 border border-[--color-accent]/30 text-[--color-accent] hover:bg-[--color-accent]/20 transition-colors font-medium"
          >
            <ExternalLink size={12} />
            Conductor UI :{run.dashboard_port}
          </a>
        )}
        {run.replay_cmd && (
          <button
            onClick={copyReplayCmd}
            className="inline-flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-md bg-[--color-surface] border border-[--color-border] text-[--color-text2] hover:bg-[--color-surface-hover] transition-colors"
          >
            <Copy size={12} />
            Copy Replay Cmd
          </button>
        )}
        {run.cwd && (
          <span className="text-xs text-[--color-text2] truncate max-w-[300px]" title={run.cwd}>
            📁 {run.cwd.split(/[\\/]/).slice(-2).join('/')}
          </span>
        )}
      </div>

      {/* Embedded Workflow Graph (live runs) */}
      {isLive && events.length > 0 && (
        <div>
          <div className="flex items-center gap-2 text-xs uppercase tracking-wide text-[--color-text2] mb-1.5">
            <span>Workflow Graph</span>
            {connected && <span className="w-1.5 h-1.5 rounded-full bg-[--color-green] animate-pulse" />}
          </div>
          <EmbeddedWorkflowGraph events={events} height={280} focusAgent={run.current_agent} />
        </div>
      )}

      {/* Work item hierarchy details */}
      {hierarchy && hierarchy.focus && (
        <div>
          <div className="text-xs uppercase tracking-wide text-[--color-text2] mb-1.5">Work Item Hierarchy</div>
          {/* Focus item */}
          {(() => {
            const focusDefs = hierarchy.type_defs?.[hierarchy.focus.type];
            const focusStateDef = focusDefs?.find((d) => d.name === hierarchy.focus.state);
            const focusCategory = focusStateDef?.category;
            const typeColor = hierarchy.type_colors?.[hierarchy.focus.type];
            return (
              <div className="flex items-center gap-2 text-xs mb-2">
                {typeColor && (
                  <span className="w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: `#${typeColor}` }} />
                )}
                <span className="font-medium text-[--color-text]">{hierarchy.focus.type} #{hierarchy.focus.id}</span>
                <span className="truncate">{hierarchy.focus.title}</span>
                <span className={`shrink-0 px-1.5 py-0.5 rounded text-[10px] font-medium ${stateBadgeClass(hierarchy.focus.state, focusCategory)}`}>
                  {hierarchy.focus.state}
                </span>
              </div>
            );
          })()}
          {/* Level breakdowns with actual state colors */}
          {hierarchy.levels.map((level, i) => {
            const total = level.total || 1;
            const typeDefs = hierarchy.type_defs?.[level.type] ?? [];
            // Build ordered state segments from type_defs (so order matches the process template)
            const segments = buildStateSegments(level.states, typeDefs, total);
            // Summary: count completed vs total
            const completedCount = typeDefs
              .filter((d) => d.category === 'Completed')
              .reduce((sum, d) => sum + (level.states[d.name] ?? 0), 0);
            const typeColor = hierarchy.type_colors?.[level.type];

            return (
              <div key={i} className="flex items-center gap-2 text-xs mb-1.5">
                <span className="text-[--color-text2] min-w-[55px] flex items-center gap-1">
                  {typeColor && <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ backgroundColor: `#${typeColor}` }} />}
                  {level.type}
                </span>
                <div className="flex h-2 flex-1 max-w-[220px] rounded overflow-hidden bg-[--color-border]">
                  {segments.map((seg, j) => (
                    <div
                      key={j}
                      title={`${seg.name}: ${seg.count}`}
                      style={{ width: `${seg.pct}%`, backgroundColor: seg.color }}
                    />
                  ))}
                </div>
                <span className="text-[--color-text2] tabular-nums whitespace-nowrap">
                  {completedCount}/{total}
                </span>
                {/* Inline state counts */}
                <span className="flex gap-1.5 text-[10px]">
                  {segments.filter((s) => s.count > 0).map((seg, j) => (
                    <span key={j} style={{ color: seg.color }} title={seg.name}>
                      {seg.count} {seg.name}
                    </span>
                  ))}
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* Subworkflows */}
      {subworkflows.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wide text-[--color-text2] mb-1.5">
            <Layers size={12} className="inline mr-1" />
            Subworkflows ({subworkflows.length})
          </div>
          <div className="grid grid-cols-[auto_auto_1fr_auto] gap-x-3 gap-y-0.5 text-xs font-mono">
            {subworkflows.map((sw, i) => (
              <div key={i} className="contents">
                <span>{sw.status === 'running' ? '🔄' : '✅'}</span>
                <span className="text-[--color-accent]">{sw.workflow.replace('./', '').replace('.yaml', '')}</span>
                <span className="text-[--color-text2] truncate">{sw.item_key ? `[${sw.item_key}]` : ''}</span>
                <span className="text-[--color-text2] text-right tabular-nums">{sw.elapsed > 0 ? fmtDuration(sw.elapsed) : '—'}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Worktree details */}
      {run.worktree && (run.worktree.branch || run.worktree.name) && (
        <div className="flex items-center gap-3 text-xs text-[--color-text2]">
          {run.worktree.name && (
            <span className="flex items-center gap-1">
              📦 <strong className="text-[--color-text]">{run.worktree.name}</strong>
            </span>
          )}
          {run.worktree.branch && (
            <span className="flex items-center gap-1">
              <GitBranch size={11} className="text-[--color-green]" />
              <span className="text-[--color-accent]">{run.worktree.branch}</span>
            </span>
          )}
          {run.worktree.toplevel && (
            <span className="truncate max-w-[200px]" title={run.worktree.toplevel}>
              {run.worktree.toplevel}
            </span>
          )}
        </div>
      )}

      {/* Agent summary (compact, collapsible in future) */}
      {run.agents.length > 0 && (
        <details className="group">
          <summary className="text-xs uppercase tracking-wide text-[--color-text2] cursor-pointer select-none hover:text-[--color-text]">
            Agents ({run.agent_count}) · Cost: {run.cost_str} · Tokens: {run.tokens_str}
          </summary>
          <table className="w-full text-xs mt-1.5">
            <thead>
              <tr className="text-[--color-text2]">
                <th className="text-left py-1">Name</th>
                <th className="text-left py-1">Model</th>
                <th className="text-right py-1">Elapsed</th>
                <th className="text-right py-1">Tokens</th>
                <th className="text-right py-1">Cost</th>
              </tr>
            </thead>
            <tbody>
              {run.agents.map((a, i) => (
                <tr key={i} className="border-t border-[--color-border]/50">
                  <td className="py-1">{a.name}</td>
                  <td className="py-1 text-[--color-text2]">{a.model}</td>
                  <td className="py-1 text-right tabular-nums">{fmtDuration(a.elapsed)}</td>
                  <td className="py-1 text-right tabular-nums">{fmtTokens(a.tokens)}</td>
                  <td className="py-1 text-right tabular-nums">{fmtCost(a.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </details>
      )}
    </div>
  );
}

interface StateSegment {
  name: string;
  count: number;
  pct: number;
  color: string;
  category: string;
}

/** Build ordered progress bar segments from raw state counts + type definitions.
 *  Order follows the process template (Proposed → InProgress → Completed → Removed),
 *  rendered right-to-left so Completed is on the left (filled first). */
function buildStateSegments(
  states: Record<string, number>,
  defs: TypeStateDef[],
  total: number,
): StateSegment[] {
  if (defs.length === 0) {
    // Fallback: no type defs, render raw state counts with heuristic colors
    return Object.entries(states)
      .filter(([, cnt]) => cnt > 0)
      .map(([name, count]) => ({
        name,
        count,
        pct: Math.round((count / total) * 100),
        color: categoryBarColor('Proposed'),
        category: 'Proposed',
      }));
  }

  // Desired render order: Completed first (left), then InProgress, Proposed, Removed
  const categoryOrder = ['Completed', 'InProgress', 'Proposed', 'Removed'];
  const sorted = [...defs].sort(
    (a, b) => categoryOrder.indexOf(a.category) - categoryOrder.indexOf(b.category),
  );

  return sorted
    .map((def) => {
      const count = states[def.name] ?? 0;
      return {
        name: def.name,
        count,
        pct: Math.round((count / total) * 100),
        color: def.color && def.color !== 'b2b2b2' && def.color !== 'ffffff'
          ? `#${def.color}`
          : categoryBarColor(def.category),
        category: def.category,
      };
    })
    .filter((s) => s.count > 0);
}
