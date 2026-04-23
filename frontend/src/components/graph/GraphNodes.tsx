/** Simplified ReactFlow node components for embedded graph view */
import { memo } from 'react';
import { Handle, Position, type NodeProps } from '@xyflow/react';
import { Bot, ShieldCheck, Terminal, Layers, Play, Square } from 'lucide-react';
import type { LayoutNodeData } from './graph-layout';
import { DurationTicker } from '@/components/shared/DurationTicker';
import { fmtDuration } from '@/lib/format';

const STATUS_COLORS: Record<string, string> = {
  pending: '#30363d',
  running: '#d29922',
  completed: '#3fb950',
  failed: '#f85149',
  waiting: '#d29922',
};

const STATUS_BG: Record<string, string> = {
  pending: '#161b22',
  running: '#1a2a1a',
  completed: '#1a2e1a',
  failed: '#2a1a1a',
  waiting: '#2a2a1a',
};

function NodeShell({ data, children }: { data: LayoutNodeData; children: React.ReactNode }) {
  const borderColor = STATUS_COLORS[data.status] ?? '#30363d';
  const bgColor = STATUS_BG[data.status] ?? '#161b22';
  return (
    <div
      className="rounded-lg border-2 px-3 py-2 text-xs min-w-[160px]"
      style={{ borderColor, backgroundColor: bgColor }}
    >
      <Handle type="target" position={Position.Top} className="!bg-[#30363d] !border-0 !w-2 !h-2" />
      {children}
      <Handle type="source" position={Position.Bottom} className="!bg-[#30363d] !border-0 !w-2 !h-2" />
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
  return null;
}

export const AgentNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  return (
    <NodeShell data={d}>
      <div className="flex items-center gap-1.5">
        <Bot size={12} className="text-[#58a6ff] shrink-0" />
        <span className="text-[#e6edf3] font-medium truncate">{d.label}</span>
      </div>
      <StatusIndicator data={d} />
    </NodeShell>
  );
});
AgentNode.displayName = 'AgentNode';

export const ScriptNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  return (
    <NodeShell data={d}>
      <div className="flex items-center gap-1.5">
        <Terminal size={12} className="text-[#8b949e] shrink-0" />
        <span className="text-[#e6edf3] font-medium truncate">{d.label}</span>
      </div>
      <StatusIndicator data={d} />
    </NodeShell>
  );
});
ScriptNode.displayName = 'ScriptNode';

export const GateNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  return (
    <NodeShell data={d}>
      <div className="flex items-center gap-1.5">
        <ShieldCheck size={12} className="text-[#d29922] shrink-0" />
        <span className="text-[#e6edf3] font-medium truncate">{d.label}</span>
      </div>
      <StatusIndicator data={d} />
    </NodeShell>
  );
});
GateNode.displayName = 'GateNode';

export const WorkflowSubNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  return (
    <NodeShell data={d}>
      <div className="flex items-center gap-1.5">
        <Layers size={12} className="text-[#bc8cff] shrink-0" />
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
    <Handle type="source" position={Position.Bottom} className="!bg-[#3fb950] !border-0 !w-2 !h-2" />
  </div>
));
StartNode.displayName = 'StartNode';

export const EndNode = memo(({ data }: NodeProps) => {
  const d = data as unknown as LayoutNodeData;
  const color = d.status === 'completed' ? '#3fb950' : '#30363d';
  return (
    <div className="w-8 h-8 rounded-full border-2 flex items-center justify-center" style={{ borderColor: color, backgroundColor: '#161b22' }}>
      <Square size={10} style={{ color }} />
      <Handle type="target" position={Position.Top} className="!border-0 !w-2 !h-2" style={{ background: color }} />
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
