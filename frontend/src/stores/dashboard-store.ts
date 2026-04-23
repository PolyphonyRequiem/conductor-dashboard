import { create } from 'zustand';
import type { DashboardData } from '@/types/dashboard';

interface DashboardState {
  /** Current dashboard data from the backend */
  data: DashboardData | null;
  /** Whether we've received at least one snapshot */
  connected: boolean;
  /** SSE connection error (null if healthy) */
  error: string | null;
  /** Actions */
  setData: (data: DashboardData) => void;
  setConnected: (connected: boolean) => void;
  setError: (error: string | null) => void;
}

export const useDashboardStore = create<DashboardState>((set) => ({
  data: null,
  connected: false,
  error: null,
  setData: (data) => set({ data, connected: true, error: null }),
  setConnected: (connected) => set({ connected }),
  setError: (error) => set({ error, connected: false }),
}));
