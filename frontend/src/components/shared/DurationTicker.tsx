import { useEffect, useState } from 'react';
import { fmtDuration } from '@/lib/format';

interface Props {
  startedAt: number;
  className?: string;
}

/** Live-updating duration ticker for active runs */
export function DurationTicker({ startedAt, className }: Props) {
  const [, setTick] = useState(0);

  useEffect(() => {
    if (!startedAt) return;
    const id = setInterval(() => setTick((t) => t + 1), 1000);
    return () => clearInterval(id);
  }, [startedAt]);

  if (!startedAt) return null;
  const elapsed = Date.now() / 1000 - startedAt;
  return <span className={className}>{fmtDuration(elapsed)}</span>;
}
