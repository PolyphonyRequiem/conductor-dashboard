/** Embedded workflow DAG graph for active run drill-down */
import { useMemo, useCallback } from 'react';
import { ReactFlow, Background, type NodeMouseHandler } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { nodeTypes } from './GraphNodes';
import { buildGraphElements } from './graph-layout';
import { processEvents } from './event-processor';
import type { WorkflowEvent } from '@/types/events';

interface Props {
  events: WorkflowEvent[];
  height?: number;
}

export function EmbeddedWorkflowGraph({ events, height = 350 }: Props) {
  const graphState = useMemo(() => processEvents(events), [events]);
  const { nodes, edges } = useMemo(() => buildGraphElements(graphState), [graphState]);

  const onNodeClick: NodeMouseHandler = useCallback(() => {
    // Future: show node detail tooltip
  }, []);

  if (nodes.length === 0) {
    return (
      <div className="flex items-center justify-center text-[--color-text2] text-xs" style={{ height }}>
        No workflow events available
      </div>
    );
  }

  return (
    <div style={{ height }} className="rounded-lg border border-[--color-border] overflow-hidden">
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodeClick={onNodeClick}
        fitView
        fitViewOptions={{ padding: 0.2 }}
        minZoom={0.3}
        maxZoom={1.5}
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        panOnDrag
        zoomOnScroll
        className="bg-[#0d1117]"
      >
        <Background color="#21262d" gap={20} size={1} />
      </ReactFlow>
    </div>
  );
}
