import { ChevronRight } from 'lucide-react';
import { useUIStore } from '@/stores/ui-store';
import { RunDetailPanel } from './RunDetailPanel';
import { ActionButton } from '@/components/shared/ActionButton';
import { actionInvestigate, actionRestart } from '@/lib/api';
import type { RunData } from '@/types/dashboard';

interface Props {
  runs: RunData[];
}

export function FailedRunsTable({ runs }: Props) {
  const expandedRuns = useUIStore((s) => s.expandedRuns);
  const toggleExpand = useUIStore((s) => s.toggleExpand);
  const reviewedRuns = useUIStore((s) => s.reviewedRuns);
  const toggleReviewed = useUIStore((s) => s.toggleReviewed);
  const showReviewed = useUIStore((s) => s.showReviewedFailed);
  const setShowReviewed = useUIStore((s) => s.setShowReviewedFailed);

  const visible = showReviewed ? runs : runs.filter((r) => !reviewedRuns.has(r.log_file));

  return (
    <div>
      <h2 className="text-lg font-semibold text-[--color-accent] border-b border-[--color-border] pb-1.5 mb-3 flex items-center gap-2.5">
        Failed Runs
        <button
          className={`text-xs px-2 py-0.5 rounded border ${showReviewed ? 'bg-[--color-accent]/10 border-[--color-accent]/30 text-[--color-accent]' : 'border-[--color-border] text-[--color-text2]'}`}
          onClick={() => setShowReviewed(!showReviewed)}
        >
          {showReviewed ? 'Hide' : 'Show'} Reviewed
        </button>
      </h2>
      {visible.length === 0 ? (
        <div className="bg-[--color-surface] border border-[--color-border] rounded-lg p-5 mb-5 text-[--color-text2]">
          No failed runs
        </div>
      ) : (
        <table className="w-full border-collapse bg-[--color-surface] border border-[--color-border] rounded-lg overflow-hidden mb-5 text-sm">
          <thead>
            <tr className="bg-[#1c2128]">
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold w-8"></th>
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Workflow</th>
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Error</th>
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Agent</th>
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Started</th>
              <th className="text-right px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Cost</th>
              <th className="text-right px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => {
              const key = `failed-${r.log_file}`;
              const isExpanded = expandedRuns.has(key);
              const isReviewed = reviewedRuns.has(r.log_file);
              return (
                <FailedRow
                  key={r.log_file}
                  run={r}
                  isExpanded={isExpanded}
                  isReviewed={isReviewed}
                  onToggleExpand={() => toggleExpand(key)}
                  onToggleReviewed={() => toggleReviewed(r.log_file)}
                />
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function FailedRow({ run, isExpanded, isReviewed, onToggleExpand, onToggleReviewed }: {
  run: RunData;
  isExpanded: boolean;
  isReviewed: boolean;
  onToggleExpand: () => void;
  onToggleReviewed: () => void;
}) {
  return (
    <>
      <tr
        className={`border-t border-[--color-border] cursor-pointer hover:bg-[--color-surface-hover] border-l-2 border-l-[--color-red] ${isReviewed ? 'opacity-45' : ''}`}
        onClick={onToggleExpand}
      >
        <td className="px-3 py-2">
          <ChevronRight size={12} className={`text-[--color-text2] transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
        </td>
        <td className="px-3 py-2 font-semibold text-[--color-accent]">{run.name}</td>
        <td className="px-3 py-2">
          {run.error_type && (
            <span className="px-1.5 py-0.5 rounded text-xs bg-red-900/50 text-red-300">{run.error_type}</span>
          )}
        </td>
        <td className="px-3 py-2 text-[--color-yellow]">{run.failed_agent || '—'}</td>
        <td className="px-3 py-2 text-[--color-text2] text-xs whitespace-nowrap">{run.started_at_str}</td>
        <td className="px-3 py-2 text-right">{run.cost_str}</td>
        <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
          <div className="flex gap-1 justify-end">
            <ActionButton
              label="Investigate"
              loadingLabel="Launching..."
              colorClass="border-yellow-600/40 text-yellow-400 hover:bg-yellow-900/20"
              onClick={() => actionInvestigate(run.log_file)}
              successMessage="🔍 Investigation launched"
            />
            <ActionButton
              label="Restart"
              loadingLabel="Starting..."
              colorClass="border-blue-600/40 text-blue-400 hover:bg-blue-900/20"
              onClick={() => actionRestart(run.log_file)}
              successMessage="🔄 Restart launched"
            />
            <button
              onClick={onToggleReviewed}
              className="text-xs px-2 py-0.5 rounded border border-[--color-border] text-[--color-text2] hover:bg-[--color-surface-hover]"
            >
              {isReviewed ? 'Unmark' : 'Reviewed'}
            </button>
          </div>
        </td>
      </tr>
      {isExpanded && (
        <tr>
          <td colSpan={7} className="p-0">
            <RunDetailPanel run={run} />
          </td>
        </tr>
      )}
    </>
  );
}
