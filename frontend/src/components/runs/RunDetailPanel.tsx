import type { RunData } from '@/types/dashboard';
import { fmtCost, fmtTokens, fmtDuration } from '@/lib/format';

interface Props {
  run: RunData;
}

export function RunDetailPanel({ run }: Props) {
  const hierarchy = run.hierarchy;
  const subworkflows = run.subworkflows || [];

  return (
    <div className="border-t border-[--color-border] px-4 py-3 text-sm space-y-3">
      {/* Work Item */}
      {run.work_item_id && (
        <div className="flex items-center gap-2">
          <span className="text-[--color-text2] text-xs uppercase tracking-wide">Work Item</span>
          {run.work_item_url ? (
            <a
              href={run.work_item_url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-[--color-accent] hover:underline"
            >
              {run.work_item_type ? `[${run.work_item_type}] ` : ''}#{run.work_item_id}{run.work_item_title ? ` ${run.work_item_title}` : ''}
            </a>
          ) : (
            <span>{run.work_item_type ? `[${run.work_item_type}] ` : ''}#{run.work_item_id}{run.work_item_title ? ` ${run.work_item_title}` : ''}</span>
          )}
        </div>
      )}

      {/* Hierarchy Progress */}
      {hierarchy && hierarchy.progress && (
        <div>
          <div className="flex gap-0.5 h-2 rounded overflow-hidden mb-1">
            {hierarchy.progress.done > 0 && (
              <div className="bg-[--color-green]" style={{ flex: hierarchy.progress.done }} />
            )}
            {hierarchy.progress.doing > 0 && (
              <div className="bg-[--color-yellow]" style={{ flex: hierarchy.progress.doing }} />
            )}
            {hierarchy.progress.todo > 0 && (
              <div className="bg-[--color-border]" style={{ flex: hierarchy.progress.todo }} />
            )}
          </div>
          <div className="text-xs text-[--color-text2]">
            ✅ {hierarchy.progress.done} done · 🔧 {hierarchy.progress.doing} in progress · ○ {hierarchy.progress.todo} to do
          </div>
        </div>
      )}

      {/* Subworkflows */}
      {subworkflows.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wide text-[--color-text2] mb-1">Subworkflows</div>
          <div className="font-mono text-xs space-y-0.5">
            {subworkflows.map((sw, i) => (
              <div key={i} className="flex gap-2">
                <span className="text-[--color-text2]">{i === subworkflows.length - 1 ? '└─' : '├─'}</span>
                <span>{sw.status === 'running' ? '🔄' : '✅'}</span>
                <span className="text-[--color-accent]">{sw.workflow.replace('./', '').replace('.yaml', '')}</span>
                {sw.item_key && <span className="text-[--color-text2]">[{sw.item_key}]</span>}
                {sw.elapsed > 0 && <span className="text-[--color-text2]">{fmtDuration(sw.elapsed)}</span>}
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Agent Summary */}
      {run.agents.length > 0 && (
        <div>
          <div className="text-xs uppercase tracking-wide text-[--color-text2] mb-1">Agents ({run.agent_count})</div>
          <table className="w-full text-xs">
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
                  <td className="py-1 text-right">{fmtDuration(a.elapsed)}</td>
                  <td className="py-1 text-right">{fmtTokens(a.tokens)}</td>
                  <td className="py-1 text-right">{fmtCost(a.cost_usd)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {/* Dashboard link + Replay command */}
      <div className="flex flex-wrap gap-4 text-xs text-[--color-text2]">
        {run.dashboard_url && (
          <a href={run.dashboard_url} target="_blank" rel="noopener noreferrer" className="text-[--color-accent] hover:underline">
            Open Conductor UI →
          </a>
        )}
        {run.replay_cmd && (
          <code className="bg-[--color-bg] border border-[--color-border] rounded px-2 py-0.5 break-all select-all">
            {run.replay_cmd}
          </code>
        )}
      </div>

      {/* Cost + Token totals */}
      {(run.total_cost > 0 || run.total_tokens > 0) && (
        <div className="flex gap-6 text-xs text-[--color-text2]">
          <span>Cost: {run.cost_str}</span>
          <span>Tokens: {run.tokens_str}</span>
        </div>
      )}
    </div>
  );
}
