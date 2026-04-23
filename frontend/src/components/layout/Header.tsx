import { useDashboardStore } from '@/stores/dashboard-store';

export function Header() {
  const connected = useDashboardStore((s) => s.connected);
  const error = useDashboardStore((s) => s.error);

  return (
    <header className="mb-5">
      <h1 className="text-2xl font-bold">🎼 Conductor Dashboard</h1>
      <p className="text-[--color-text2] text-sm">
        {connected ? (
          <span className="text-[--color-green]">● Connected (real-time)</span>
        ) : error ? (
          <span className="text-[--color-red]">● {error}</span>
        ) : (
          <span className="text-[--color-text2]">● Connecting...</span>
        )}
      </p>
    </header>
  );
}
