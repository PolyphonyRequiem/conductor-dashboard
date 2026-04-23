import { useEffect, useRef, useState } from 'react';
import { fmtDuration } from '@/lib/format';

interface Props {
  startedAt: number;
  className?: string;
}

/** Live-updating duration ticker using requestAnimationFrame for smooth updates */
export function DurationTicker({ startedAt, className }: Props) {
  const [display, setDisplay] = useState('');
  const rafRef = useRef<number>(0);
  const lastStr = useRef('');

  useEffect(() => {
    if (!startedAt) return;

    function tick() {
      const elapsed = Date.now() / 1000 - startedAt;
      const str = fmtDuration(elapsed);
      // Only update state when the string actually changes (every ~1s)
      if (str !== lastStr.current) {
        lastStr.current = str;
        setDisplay(str);
      }
      rafRef.current = requestAnimationFrame(tick);
    }

    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [startedAt]);

  if (!startedAt) return null;
  return <span className={className}>{display}</span>;
}
