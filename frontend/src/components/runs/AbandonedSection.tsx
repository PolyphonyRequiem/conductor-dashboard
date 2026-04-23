import { useUIStore } from '@/stores/ui-store';
import { ActiveRunCard } from './ActiveRunCard';
import type { RunData } from '@/types/dashboard';

interface Props {
  runs: RunData[];
}

export function AbandonedSection({ runs }: Props) {
  const showAbandoned = useUIStore((s) => s.showAbandoned);
  const setShowAbandoned = useUIStore((s) => s.setShowAbandoned);

  if (runs.length === 0) return null;

  return (
    <div>
      <h2 className="text-lg font-semibold text-[--color-accent] border-b border-[--color-border] pb-1.5 mb-3 flex items-center gap-2.5">
        Abandoned Runs
        <span className="text-xs bg-red-900/50 text-red-300 px-1.5 py-0.5 rounded">{runs.length}</span>
        <button
          className="text-xs px-2 py-0.5 rounded border border-[--color-border] text-[--color-text2] hover:bg-[--color-surface-hover]"
          onClick={() => setShowAbandoned(!showAbandoned)}
        >
          {showAbandoned ? 'Hide' : 'Show'}
        </button>
      </h2>
      {showAbandoned && (
        <div>
          {runs.map((r, i) => (
            <ActiveRunCard key={r.log_file || `abandoned-${i}`} run={r} index={i} keyPrefix="abandoned" />
          ))}
        </div>
      )}
    </div>
  );
}
