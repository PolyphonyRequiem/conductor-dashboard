import { useEffect, RefObject } from 'react';
import { useUIStore } from '@/stores/ui-store';

export function useKeyboardShortcuts(searchRef: RefObject<HTMLInputElement | null>) {
  useEffect(() => {
    function handler(e: KeyboardEvent) {
      const target = e.target as HTMLElement;
      const isInput =
        target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.isContentEditable;

      // Escape works even when focused on input
      if (e.key === 'Escape') {
        if (searchRef.current && document.activeElement === searchRef.current) {
          searchRef.current.blur();
          useUIStore.getState().setFilterText('');
        } else {
          useUIStore.getState().collapseAll();
        }
        return;
      }

      // All other shortcuts are suppressed when focus is in an input
      if (isInput) return;

      if (e.key === '/') {
        e.preventDefault();
        searchRef.current?.focus();
        return;
      }

      if (e.key === '?') {
        e.preventDefault();
        useUIStore.getState().toggleHelp();
        return;
      }

      if (e.key === 'r') {
        window.location.reload();
        return;
      }
    }

    document.addEventListener('keydown', handler);
    return () => document.removeEventListener('keydown', handler);
  }, [searchRef]);
}
