/** WebSocket hook to stream conductor events from a running instance */
import { useEffect, useRef, useState, useCallback } from 'react';
import type { WorkflowEvent } from '@/types/events';

interface UseConductorWsOptions {
  logFile: string;
  enabled?: boolean;
}

export function useConductorWs({ logFile, enabled = true }: UseConductorWsOptions) {
  const [events, setEvents] = useState<WorkflowEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const retriesRef = useRef(0);

  const connect = useCallback(() => {
    if (!enabled || !logFile) return;

    const encodedLog = encodeURIComponent(logFile);
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const url = `${protocol}//${window.location.host}/api/run/${encodedLog}/ws`;

    const ws = new WebSocket(url);
    wsRef.current = ws;

    ws.onopen = () => {
      setConnected(true);
      retriesRef.current = 0;
    };

    ws.onmessage = (msg) => {
      try {
        const event = JSON.parse(msg.data) as WorkflowEvent;
        setEvents((prev) => [...prev, event]);
      } catch {
        // Ignore malformed messages
      }
    };

    ws.onclose = () => {
      setConnected(false);
      wsRef.current = null;
      // Retry with backoff up to 5 times
      if (retriesRef.current < 5 && enabled) {
        const delay = Math.min(1000 * 2 ** retriesRef.current, 15000);
        retriesRef.current++;
        setTimeout(connect, delay);
      }
    };

    ws.onerror = () => {
      ws.close();
    };
  }, [logFile, enabled]);

  useEffect(() => {
    connect();
    return () => {
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [connect]);

  return { events, connected };
}
