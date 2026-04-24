import { useEffect, useRef } from 'react';
import { useDashboardStore } from '@/stores/dashboard-store';
import type { RunData } from '@/types/dashboard';

/**
 * Edge-trigger browser notifications when a gate opens on an active run.
 * Only fires once per run+gate transition (gate_waiting: false → true).
 */
export function useGateNotifications() {
  const activeRuns = useDashboardStore((s) => s.data?.active_runs);
  const seenGates = useRef<Set<string>>(new Set());
  const permissionAsked = useRef(false);

  useEffect(() => {
    if (!activeRuns) return;

    // Lazily request permission on first gate-waiting run
    const hasGates = activeRuns.some((r) => r.gate_waiting);
    if (hasGates && !permissionAsked.current && 'Notification' in window && Notification.permission === 'default') {
      permissionAsked.current = true;
      Notification.requestPermission();
    }

    for (const run of activeRuns) {
      const key = run.run_id || run.log_file;
      if (!key) continue;

      if (run.gate_waiting) {
        if (!seenGates.current.has(key)) {
          seenGates.current.add(key);
          fireNotification(run);
        }
      } else {
        // Gate resolved — remove from seen so it can re-trigger
        seenGates.current.delete(key);
      }
    }

    // Clean up stale keys for runs no longer active
    const activeKeys = new Set(activeRuns.map((r) => r.run_id || r.log_file).filter(Boolean));
    for (const key of seenGates.current) {
      if (!activeKeys.has(key)) seenGates.current.delete(key);
    }
  }, [activeRuns]);
}

function fireNotification(run: RunData) {
  if (!('Notification' in window) || Notification.permission !== 'granted') return;

  const title = `🚦 Gate waiting: ${run.name || 'workflow'}`;
  const body = run.gate_agent
    ? `Agent "${run.gate_agent}" requires approval`
    : 'A human gate requires your attention';

  try {
    const n = new Notification(title, {
      body,
      icon: '/favicon.svg',
      tag: `gate-${run.run_id || run.log_file}`,
    });
    // Click notification → focus dashboard
    n.onclick = () => {
      window.focus();
      n.close();
    };
  } catch {
    // Notification API may fail in some contexts
  }
}
