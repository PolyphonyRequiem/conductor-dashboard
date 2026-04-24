/** Simplified ReactFlow node components for embedded graph view */
import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Bot, ShieldCheck, Terminal, Layers, Play, Square } from 'lucide-react';
import type { LayoutNodeData } from './graph-layout';
import { DurationTicker } from '@/components/shared/DurationTicker';
import { fmtDuration } from '@/lib/format';

// Border colors by status
const STATUS_BORDER: Record<string, string> = {
  pending: '#484f58',
  running: '#d29922',
  completed: '#3fb950',
  failed: '#f85149',
  waiting: '#d29922',
};

// Per-type accent colors for icon + subtle bg tint
const TYPE_ACCENT: Record<string, { icon: string; bg: string; bgActive: string }> = {
  agent:       { icon: '#58a6ff', bg: '#161d2d', bgActive: '#1a2840' },
  script:      { icon: '#8b949e', bg: '#1a1d22', bgActive: '#22272e' },
  human_gate:  { icon: '#d29922', bg: '#221d14', bgActive: '#2e2818' },
  workflow:    { icon: '#bc8cff', bg: '#1e182e', bgActive: '#281e3e' },
};

function getNodeStyle(data: LayoutNodeData) {
  const accent = TYPE_ACCENT[data.type] ?? TYPE_ACCENT.agent!;
  const isActive = data.status === 'running' || data.status === 'waiting';
  return {
    borderColor: STATUS_BORDER[data.status] ?? '#484f58',
    backgroundColor: isActive ? accent!.bgActive : (data.status === 'completed' ? '#141e18' : data.status === 'failed' ? '#241414' : accent!.bg),
    iconColor: accent!.icon,
  };
}

function NodeShell({ data, children }: { data: LayoutNodeData; children: React.ReactNode }) {
  const style = getNodeStyle(data);
  const isRunning = data.status === 'running';
  return (
    <div
      className={`rounded-lg border-2 px-3 py-2 text-xs min-w-[160px] transition-all ${isRunning ? 'shadow-[0_0_12px_rgba(210,153,34,0.3)]' : ''}`}
      style={{ borderColor: style.borderColor, backgroundColor: style.backgroundColor }}
    >
      <Handle type="target" position={Position.Left} className="!bg-[#484f58] !border-0 !w-2 !h-2" />
      {children}
      <Handle type="source" position={Position.Right} className="!bg-[#484f58] !border-0 !w-2 !h-2" />
    </div>
  );
}

function StatusIndicator({ data }: { data: LayoutNodeData }) {
  if (data.status === 'running' && data.startedAt) {
    return <DurationTicker startedAt={data.startedAt} className="text-[#d29922] text-[10px]" />;
  }
  if (data.status === 'completed' && data.elapsed) {
    return <span className="text-[#3fb950] text-[10px]">{fmtDuration(data.elapsed)}</span>;
  }
  if (data.status === 'failed') {
    return <span className="text-[#f85149] text-[10px]">{data.error_type || 'failed'}</span>;
  }
  if (data.status === 'waiting') {
    return <span className="text-[#d29922] text-[10px] animate-pulse">awaiting input</span>;
  }
  if (data.status === 'pending') {
    return <span className="text-[#484f58] text-[10px]">pending</span>;
  }
  return null;
}

export const AgentNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  const { iconColor } = getNodeStyle(d);
  return (
    <NodeShell data={d}>
      <div className="flex items-center gap-1.5">
        <Bot size={12} style={{ color: iconColor }} className="shrink-0" />
        <span className="text-[#e6edf3] font-medium truncate">{d.label}</span>
      </div>
      <StatusIndicator data={d} />
    </NodeShell>
  );
});
AgentNode.displayName = 'AgentNode';

export const ScriptNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  const { iconColor } = getNodeStyle(d);
  return (
    <NodeShell data={d}>
      <div className="flex items-center gap-1.5">
        <Terminal size={12} style={{ color: iconColor }} className="shrink-0" />
        <span className="text-[#e6edf3] font-medium truncate">{d.label}</span>
      </div>
      <StatusIndicator data={d} />
    </NodeShell>
  );
});
ScriptNode.displayName = 'ScriptNode';

export const GateNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  const { iconColor } = getNodeStyle(d);
  return (
    <NodeShell data={d}>
      <div className="flex items-center gap-1.5">
        <ShieldCheck size={12} style={{ color: iconColor }} className="shrink-0" />
        <span className="text-[#e6edf3] font-medium truncate">{d.label}</span>
      </div>
      <StatusIndicator data={d} />
    </NodeShell>
  );
});
GateNode.displayName = 'GateNode';

export const WorkflowSubNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  const { iconColor } = getNodeStyle(d);
  return (
    <NodeShell data={d}>
      <div className="flex items-center gap-1.5">
        <Layers size={12} style={{ color: iconColor }} className="shrink-0" />
        <span className="text-[#e6edf3] font-medium truncate">{d.label}</span>
      </div>
      <StatusIndicator data={d} />
    </NodeShell>
  );
});
WorkflowSubNode.displayName = 'WorkflowSubNode';

export const StartNode = memo(({ data: _ }: NodeProps) => (
  <div className="w-8 h-8 rounded-full bg-[#1a2e1a] border-2 border-[#3fb950] flex items-center justify-center">
    <Play size={12} className="text-[#3fb950]" />
    <Handle type="source" position={Position.Right} className="!bg-[#3fb950] !border-0 !w-2 !h-2" />
  </div>
));
StartNode.displayName = 'StartNode';

export const EndNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  const color = d.status === 'completed' ? '#3fb950' : '#484f58';
  return (
    <div className="w-8 h-8 rounded-full border-2 flex items-center justify-center" style={{ borderColor: color, backgroundColor: '#161b22' }}>
      <Square size={10} style={{ color }} />
      <Handle type="target" position={Position.Left} className="!border-0 !w-2 !h-2" style={{ background: color }} />
    </div>
  );
});
EndNode.displayName = 'EndNode';

export const nodeTypes = {
  agentNode: AgentNode,
  scriptNode: ScriptNode,
  gateNode: GateNode,
  workflowNode: WorkflowSubNode,
  startNode: StartNode,
  endNode: EndNode,
};
