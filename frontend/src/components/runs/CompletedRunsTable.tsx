import { ChevronRight } from 'lucide-react';
import { useUIStore } from '@/stores/ui-store';
import { RunDetailPanel } from './RunDetailPanel';
import { ActionButton } from '@/components/shared/ActionButton';
import { actionReview } from '@/lib/api';
import type { RunData } from '@/types/dashboard';

interface Props {
  runs: RunData[];
}

export function CompletedRunsTable({ runs }: Props) {
  const expandedRuns = useUIStore((s) => s.expandedRuns);
  const toggleExpand = useUIStore((s) => s.toggleExpand);
  const reviewedRuns = useUIStore((s) => s.reviewedRuns);
  const toggleReviewed = useUIStore((s) => s.toggleReviewed);
  const showReviewed = useUIStore((s) => s.showReviewedCompleted);
  const setShowReviewed = useUIStore((s) => s.setShowReviewedCompleted);

  const visible = showReviewed ? runs : runs.filter((r) => !reviewedRuns.has(r.log_file));

  return (
    <div>
      <h2 className="text-lg font-semibold text-[--color-accent] border-b border-[--color-border] pb-1.5 mb-3 flex items-center gap-2.5">
        Completed Runs
        <button
          className={`text-xs px-2 py-0.5 rounded border ${showReviewed ? 'bg-[--color-accent]/10 border-[--color-accent]/30 text-[--color-accent]' : 'border-[--color-border] text-[--color-text2]'}`}
          onClick={() => setShowReviewed(!showReviewed)}
        >
          {showReviewed ? 'Hide' : 'Show'} Reviewed
        </button>
      </h2>
      {visible.length === 0 ? (
        <div className="bg-[--color-surface] border border-[--color-border] rounded-lg p-5 mb-5 text-[--color-text2]">
          No completed runs
        </div>
      ) : (
        <table className="w-full border-collapse bg-[--color-surface] border border-[--color-border] rounded-lg overflow-hidden mb-5 text-sm">
          <thead>
            <tr className="bg-[#1c2128]">
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold w-8"></th>
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Workflow</th>
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Started</th>
              <th className="text-left px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Duration</th>
              <th className="text-right px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Cost</th>
              <th className="text-right px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Tokens</th>
              <th className="text-right px-3 py-2.5 text-xs uppercase tracking-wide text-[--color-text2] font-semibold">Actions</th>
            </tr>
          </thead>
          <tbody>
            {visible.map((r) => {
              const key = `completed-${r.log_file}`;
              const isExpanded = expandedRuns.has(key);
              const isReviewed = reviewedRuns.has(r.log_file);
              return (
                <RunRow
                  key={r.log_file}
                  run={r}
                  expandKey={key}
                  isExpanded={isExpanded}
                  isReviewed={isReviewed}
                  onToggleExpand={() => toggleExpand(key)}
                  onToggleReviewed={() => toggleReviewed(r.log_file)}
                  showReviewAction={r.review_available}
                />
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function RunRow({
  run, expandKey: _expandKey, isExpanded, isReviewed, onToggleExpand, onToggleReviewed, showReviewAction,
}: {
  run: RunData;
  expandKey: string;
  isExpanded: boolean;
  isReviewed: boolean;
  onToggleExpand: () => void;
  onToggleReviewed: () => void;
  showReviewAction: boolean;
}){
  return (
    <>
      <tr
        className={`border-t border-[--color-border] cursor-pointer hover:bg-[--color-surface-hover] border-l-2 border-l-[--color-green] ${isReviewed ? 'opacity-45' : ''}`}
        onClick={onToggleExpand}
      >
        <td className="px-3 py-2">
          <ChevronRight size={12} className={`text-[--color-text2] transition-transform ${isExpanded ? 'rotate-90' : ''}`} />
        </td>
        <td className="px-3 py-2 font-semibold text-[--color-accent]">{run.name}</td>
        <td className="px-3 py-2 text-[--color-text2] text-xs whitespace-nowrap">{run.started_at_str}</td>
        <td className="px-3 py-2">{run.elapsed}</td>
        <td className="px-3 py-2 text-right">{run.cost_str}</td>
        <td className="px-3 py-2 text-right">{run.tokens_str}</td>
        <td className="px-3 py-2 text-right" onClick={(e) => e.stopPropagation()}>
          <div className="flex gap-1 justify-end">
            {showReviewAction && (
              <ActionButton
                label="Review"
                loadingLabel="Launching..."
                colorClass="border-green-600/40 text-green-400 hover:bg-green-900/20"
                onClick={() => actionReview(run.log_file)}
                successMessage="📋 Review launched"
              />
            )}
            <button onClick={onToggleReviewed} className="text-xs px-2 py-0.5 rounded border border-[--color-border] text-[--color-text2] hover:bg-[--color-surface-hover]">
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
