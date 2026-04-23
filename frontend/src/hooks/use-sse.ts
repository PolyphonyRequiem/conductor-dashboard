import { useEffect, useRef } from 'react';
import { useDashboardStore } from '@/stores/dashboard-store';
import { useUIStore } from '@/stores/ui-store';
import type { DashboardData } from '@/types/dashboard';

/** Connects to the SSE endpoint and keeps the dashboard store in sync. */
export function useSSE() {
  const setData = useDashboardStore((s) => s.setData);
  const setError = useDashboardStore((s) => s.setError);
  const setConnected = useDashboardStore((s) => s.setConnected);
  const retryDelay = useRef(1000);

  useEffect(() => {
    let es: EventSource | null = null;
    let retryTimer: ReturnType<typeof setTimeout> | null = null;

    function connect() {
      const reviewed = [...useUIStore.getState().reviewedRuns].join(',');
      const params = reviewed ? `?reviewed=${encodeURIComponent(reviewed)}` : '';
      es = new EventSource(`/api/events${params}`);

      es.addEventListener('snapshot', (e: MessageEvent) => {
        retryDelay.current = 1000; // reset backoff on success
        const data = JSON.parse(e.data) as DashboardData;
        setData(data);
      });

      es.addEventListener('update', (e: MessageEvent) => {
        const data = JSON.parse(e.data) as DashboardData;
        setData(data);
      });

      es.addEventListener('ping', () => {
        // Heartbeat — connection is alive
      });

      es.onerror = () => {
        es?.close();
        setConnected(false);
        setError('Connection lost — reconnecting...');
        // Exponential backoff: 1s, 2s, 4s, 8s, max 30s
        retryTimer = setTimeout(() => {
          retryDelay.current = Math.min(retryDelay.current * 2, 30000);
          connect();
        }, retryDelay.current);
      };

      es.onopen = () => {
        setConnected(true);
        setError(null);
        retryDelay.current = 1000;
      };
    }

    connect();

    return () => {
      es?.close();
      if (retryTimer) clearTimeout(retryTimer);
    };
  }, [setData, setError, setConnected]);
}
