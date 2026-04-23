import { useCallback, useEffect, useState } from 'react';

interface ToastMessage {
  id: number;
  text: string;
  kind: 'ok' | 'err';
}

let _nextId = 0;
let _addToast: ((text: string, kind: 'ok' | 'err') => void) | null = null;

/** Show a toast notification from anywhere */
export function toast(text: string, kind: 'ok' | 'err' = 'ok') {
  _addToast?.(text, kind);
}

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  const addToast = useCallback((text: string, kind: 'ok' | 'err') => {
    const id = ++_nextId;
    setToasts((prev) => [...prev, { id, text, kind }]);
    setTimeout(() => {
      setToasts((prev) => prev.filter((t) => t.id !== id));
    }, 6000);
  }, []);

  useEffect(() => {
    _addToast = addToast;
    return () => { _addToast = null; };
  }, [addToast]);

  if (toasts.length === 0) return null;

  return (
    <div className="fixed top-4 right-4 z-50 flex flex-col gap-2">
      {toasts.map((t) => (
        <div
          key={t.id}
          className={`px-4 py-3 rounded-lg border text-sm shadow-lg transition-opacity ${
            t.kind === 'err'
              ? 'bg-[#5a1f1f] border-red-500/40 text-red-200'
              : 'bg-[#1f3d1f] border-green-500/40 text-green-200'
          }`}
        >
          {t.text}
        </div>
      ))}
    </div>
  );
}
