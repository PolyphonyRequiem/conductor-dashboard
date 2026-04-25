import { Layers, Tag, ExternalLink } from 'lucide-react';
import { AgentTypeIcon } from '@/components/shared/AgentTypeIcon';
import { WorkItemIcon } from '@/components/shared/WorkItemIcon';
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

  // Title provider: work item badge with type color/icon
  const wiType = run.work_item_type || '';
  const typeColor = run.hierarchy?.type_colors?.[wiType];
  const hexColor = typeColor ? `#${typeColor}` : '#58a6ff';
  const iconId = run.hierarchy?.type_icons?.[wiType] ?? (wiType ? 'icon_clipboard' : '');
  const wiId = run.work_item_id || '';
  const displayTitle = run.display_title || '';
  const wiUrl = run.work_item_url || '';

  // Tags
  const allTags = run.display_tags || [];
  const maxTags = 3;
  const visibleTags = allTags.slice(0, maxTags);
  const overflowCount = allTags.length - maxTags;

  const titleContent = wiId ? (
    <span
      className="inline-flex items-center gap-1 text-xs px-2 py-0.5 rounded-full border truncate max-w-[400px]"
      style={{
        borderColor: `${hexColor}40`,
        backgroundColor: `${hexColor}15`,
        color: hexColor,
      }}
    >
      {iconId && <WorkItemIcon iconId={iconId} color={hexColor} size={12} />}
      <span className="font-medium shrink-0">#{wiId}</span>
      {displayTitle && displayTitle !== `#${wiId}` && (
        <span className="truncate opacity-80">{displayTitle}</span>
      )}
      {wiUrl && <ExternalLink size={9} className="shrink-0 opacity-50" />}
    </span>
  ) : null;

  const titleElement = wiUrl && titleContent ? (
    <a
      href={wiUrl}
      target="_blank"
      rel="noopener noreferrer"
      className="hover:brightness-125 transition-all"
      onClick={(e) => e.stopPropagation()}
    >
      {titleContent}
    </a>
  ) : titleContent;

  return (
    <div className="flex items-center gap-2 min-w-0 flex-1">
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
      {titleElement}
      {visibleTags.length > 0 && (
        <span className="flex items-center gap-1 shrink-0">
          {visibleTags.map((tag) => (
            <span
              key={tag}
              className="inline-flex items-center gap-0.5 text-[10px] px-1.5 py-0.5 rounded-full bg-purple-900/30 border border-purple-700/30 text-purple-300"
            >
              <Tag size={8} className="shrink-0 opacity-60" />
              {tag}
            </span>
          ))}
          {overflowCount > 0 && (
            <span
              className="text-[10px] px-1.5 py-0.5 rounded-full bg-purple-900/20 border border-purple-700/20 text-purple-400 tabular-nums"
              title={allTags.slice(maxTags).join(', ')}
            >
              +{overflowCount}
            </span>
          )}
        </span>
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
