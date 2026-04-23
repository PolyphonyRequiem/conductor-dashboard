import { Layers } from 'lucide-react';
import { AgentTypeIcon } from '@/components/shared/AgentTypeIcon';
import type { RunData } from '@/types/dashboard';

interface Props {
  run: RunData;
}

interface Segment {
  name: string;
  type: 'workflow' | 'agent';
  isActive: boolean;
  agentType?: RunData['current_agent_type'];
}

export function PowerlineBreadcrumbs({ run }: Props) {
  const runningSubs = (run.subworkflows || []).filter((s) => s.status === 'running');
  const segments: Segment[] = [];

  // Workflow segments
  segments.push({
    name: run.name,
    type: 'workflow',
    isActive: runningSubs.length === 0,
  });

  for (let i = 0; i < runningSubs.length; i++) {
    const sw = runningSubs[i]!;
    const swName = (sw.workflow || '').replace('./', '').replace('.yaml', '');
    segments.push({
      name: swName,
      type: 'workflow',
      isActive: i === runningSubs.length - 1,
    });
  }

  // Active agent as final segment
  const activeAgent = run.current_agent || '';
  const activeAgentType = run.current_agent_type || 'agent';

  const total = segments.length + (activeAgent ? 1 : 0);

  return (
    <div className="flex items-stretch shrink-0">
      {segments.map((seg, i) => {
        const isLast = !activeAgent && i === segments.length - 1;
        return (
          <div
            key={i}
            className={segmentClasses(seg.isActive, false, i === 0, isLast, total)}
            style={seg.isActive ? { animation: 'pl-active-pulse 2.5s ease-in-out infinite' } : undefined}
          >
            <Layers size={14} className="shrink-0" />
            <span>{seg.name}</span>
          </div>
        );
      })}
      {activeAgent && (
        <div
          className={segmentClasses(false, true, false, true, total)}
          style={{ animation: 'pl-agent-pulse 2.5s ease-in-out infinite' }}
        >
          <AgentTypeIcon type={activeAgentType} size={14} className="shrink-0" />
          <span>{activeAgent}</span>
        </div>
      )}
    </div>
  );
}

function segmentClasses(
  isActive: boolean,
  isAgent: boolean,
  isFirst: boolean,
  isLast: boolean,
  _total: number,
): string {
  const base = 'flex items-center gap-1.5 px-3 py-1 text-[0.82rem] font-medium whitespace-nowrap -mr-2.5 relative';

  // Background colors
  let colors: string;
  if (isAgent) {
    colors = 'bg-[#1a2e1a] text-[#4eda8a]';
  } else if (isActive) {
    colors = 'bg-[#1a3a5c] text-[#7ab8e8]';
  } else {
    colors = 'bg-[#2a3545] text-[#8ba4c0]';
  }

  // Clip-path for seamless chevron rendering
  let clip: string;
  if (isFirst && isLast) {
    clip = '[clip-path:polygon(0_4px,4px_0,calc(100%-4px)_0,100%_4px,100%_calc(100%-4px),calc(100%-4px)_100%,4px_100%,0_calc(100%-4px))]';
  } else if (isFirst) {
    clip = 'rounded-l [clip-path:polygon(0_0,calc(100%-10px)_0,100%_50%,calc(100%-10px)_100%,0_100%)]';
  } else if (isLast) {
    clip = '[clip-path:polygon(0_0,calc(100%-4px)_0,100%_4px,100%_calc(100%-4px),calc(100%-4px)_100%,0_100%,10px_50%)] pl-5.5 pr-3 mr-0';
  } else {
    clip = '[clip-path:polygon(0_0,calc(100%-10px)_0,100%_50%,calc(100%-10px)_100%,0_100%,10px_50%)] pl-5.5';
  }

  return `${base} ${colors} ${clip}`;
}
