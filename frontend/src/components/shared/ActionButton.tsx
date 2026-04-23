import { useState, useCallback } from 'react';
import { toast } from './Toast';

interface Props {
  label: string;
  loadingLabel?: string;
  colorClass: string;
  onClick: () => Promise<{ error?: string; status?: string; workflow?: string }>;
  successMessage?: string;
}

/** Button with optimistic loading state and toast feedback */
export function ActionButton({ label, loadingLabel, colorClass, onClick, successMessage }: Props) {
  const [loading, setLoading] = useState(false);

  const handleClick = useCallback(async () => {
    setLoading(true);
    try {
      const result = await onClick();
      if (result.error) {
        toast(`❌ ${result.error}`, 'err');
      } else {
        toast(successMessage ?? `✅ ${label} launched`, 'ok');
      }
    } catch (e) {
      toast(`❌ ${e instanceof Error ? e.message : 'Unknown error'}`, 'err');
    } finally {
      setLoading(false);
    }
  }, [onClick, label, successMessage]);

  return (
    <button
      onClick={handleClick}
      disabled={loading}
      className={`text-xs px-2 py-0.5 rounded border ${colorClass} ${loading ? 'opacity-50 cursor-wait' : ''}`}
    >
      {loading ? (loadingLabel ?? '…') : label}
    </button>
  );
}
