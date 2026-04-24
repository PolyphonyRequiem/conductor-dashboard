/** Hook to fetch conductor workflow events from a running instance's REST API */
import { useEffect, useRef, useState } from 'react';
import type { WorkflowEvent } from '@/types/events';

interface UseConductorWsOptions {
  logFile: string;
  dashboardPort?: number;
  enabled?: boolean;
}

const POLL_INTERVAL = 5000;

export function useConductorWs({ dashboardPort, enabled = true }: UseConductorWsOptions) {
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval> | undefined>(undefined);
  const lastLenRef = useRef<number>(0);

  useEffect(() => {
    if (!enabled || !dashboardPort) return;

    const fetchState = async () => {
      try {
        const res = await fetch(`http://localhost:${dashboardPort}/api/state`);
        if (!res.ok) return;
        const data = (await res.json()) as WorkflowEvent[];
        // Only update if we got new events
        if (data.length !== lastLenRef.current) {
          lastLenRef.current = data.length;
          setEvents(data);
          setConnected(true);
        }
      } catch {
        setConnected(false);
      }
    };

    fetchState();
    timerRef.current = setInterval(fetchState, POLL_INTERVAL);

    return () => {
      clearInterval(timerRef.current);
      setConnected(false);
    };
  }, [dashboardPort, enabled]);

  return { events, connected };
}
