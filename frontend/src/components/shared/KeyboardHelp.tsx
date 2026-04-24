import { useUIStore } from '@/stores/ui-store';

const shortcuts = [
  { key: '?', description: 'Show / hide this help' },
  { key: '/', description: 'Focus search' },
  { key: 'Esc', description: 'Clear search / collapse all' },
  { key: 'r', description: 'Refresh dashboard' },
];

export function KeyboardHelp() {
  const showHelp = useUIStore((s) => s.showHelp);
  const toggleHelp = useUIStore((s) => s.toggleHelp);

  if (!showHelp) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60"
      onClick={toggleHelp}
    >
      <div
        className="bg-[--color-surface] border border-[--color-border] rounded-xl p-6 shadow-2xl
                    min-w-[320px] max-w-[400px]"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold text-[--color-text] mb-4">Keyboard Shortcuts</h3>
        <dl className="space-y-2.5">
          {shortcuts.map(({ key, description }) => (
            <div key={key} className="flex items-center gap-3">
              <kbd
                className="inline-flex items-center justify-center min-w-[2rem] px-2 py-0.5
                           rounded bg-[--color-bg] border border-[--color-border]
                           text-xs font-mono text-[--color-text] select-none"
              >
                {key}
              </kbd>
              <dd className="text-sm text-[--color-text2]">{description}</dd>
            </div>
          ))}
        </dl>
        <p className="mt-4 text-xs text-[--color-text2] text-center">
          Press <kbd className="px-1 py-0.5 rounded bg-[--color-bg] border border-[--color-border] text-[--color-text] font-mono text-[10px]">?</kbd> or click outside to close
        </p>
      </div>
    </div>
  );
}
