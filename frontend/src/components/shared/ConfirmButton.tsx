import { useState, useCallback, useEffect, useRef } from 'react';
import { toast } from './Toast';

interface Props {
  label: string;
  confirmLabel?: string;
  loadingLabel?: string;
  colorClass: string;
  onClick: () => Promise<{ error?: string; status?: string }>;
  successMessage?: string;
  /** Seconds before the confirm state auto-reverts (default: 4) */
  timeout?: number;
}

/** Button with inline confirm step: click → "Sure? ✓ ✗" → action/revert */
export function ConfirmButton({
  label, confirmLabel, loadingLabel, colorClass, onClick, successMessage, timeout = 4,
}: Props) {
  const [state, setState] = useState<'idle' | 'confirming' | 'loading'>('idle');
  const timerRef = useRef<ReturnType<typeof setTimeout>>(undefined);

  useEffect(() => () => clearTimeout(timerRef.current), []);

  const handleInitialClick = useCallback(() => {
    setState('confirming');
    timerRef.current = setTimeout(() => setState('idle'), timeout * 1000);
  }, [timeout]);

  const handleConfirm = useCallback(async () => {
    clearTimeout(timerRef.current);
    setState('loading');
    try {
      const result = await onClick();
      if (result.error) {
        toast(`❌ ${result.error}`, 'err');
      } else {
        toast(successMessage ?? `✅ ${label} done`, 'ok');
      }
    } catch (e) {
      toast(`❌ ${e instanceof Error ? e.message : 'Unknown error'}`, 'err');
    } finally {
      setState('idle');
    }
  }, [onClick, label, successMessage]);

  const handleCancel = useCallback(() => {
    clearTimeout(timerRef.current);
    setState('idle');
  }, []);

  if (state === 'loading') {
    return (
      <span className={`text-xs px-2 py-0.5 rounded border ${colorClass} opacity-50 cursor-wait`}>
        {loadingLabel ?? '…'}
      </span>
    );
  }

  if (state === 'confirming') {
    return (
      <span className="inline-flex items-center gap-1">
        <span className="text-[10px] text-[--color-text2]">{confirmLabel ?? 'Sure?'}</span>
        <button
          onClick={handleConfirm}
          className="text-[10px] px-1.5 py-0.5 rounded border border-red-600/50 text-red-400 hover:bg-red-900/30 transition-colors"
        >
          ✓
        </button>
        <button
          onClick={handleCancel}
          className="text-[10px] px-1.5 py-0.5 rounded border border-[--color-border] text-[--color-text2] hover:bg-[--color-surface-hover] transition-colors"
        >
          ✗
        </button>
      </span>
    );
  }

  return (
    <button
      onClick={handleInitialClick}
      className={`text-xs px-2 py-0.5 rounded border ${colorClass}`}
    >
      {label}
    </button>
  );
}
