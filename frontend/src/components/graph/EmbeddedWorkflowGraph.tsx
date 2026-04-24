/** Embedded workflow DAG graph for active run drill-down */
import { useMemo, useCallback } from 'react';
import { ReactFlow, Background, type NodeMouseHandler } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { nodeTypes } from './GraphNodes';
import { buildGraphElements } from './graph-layout';
import { processEvents } from './event-processor';
import type { WorkflowEvent, GraphState } from '@/types/events';

interface Props {
  events: WorkflowEvent[];
  height?: number;
  /** When set, only show this agent + its predecessors + successors */
  focusAgent?: string;
}

/** Filter graph state to only show the focused agent's neighborhood */
function filterToNeighborhood(state: GraphState, focus: string): GraphState {
  // Find predecessors and successors from routes
  const predecessors = new Set<string>();
  const successors = new Set<string>();
  for (const route of state.routes) {
    if (route.to === focus) predecessors.add(route.from);
    if (route.from === focus) successors.add(route.to);
  }

  const visible = new Set([focus, ...predecessors, ...successors]);

  return {
    ...state,
    agents: state.agents.filter((a) => visible.has(a.name)),
    routes: state.routes.filter((r) => visible.has(r.from) && visible.has(r.to)),
    entryPoint: visible.has(state.entryPoint ?? '') ? state.entryPoint : null,
  };
}

export function EmbeddedWorkflowGraph({ events, height, focusAgent }: Props) {
  const fullState = useMemo(() => processEvents(events), [events]);
  const graphState = useMemo(
    () => (focusAgent && fullState.nodes[focusAgent] ? filterToNeighborhood(fullState, focusAgent) : fullState),
    [fullState, focusAgent],
  );
  const { nodes, edges } = useMemo(() => buildGraphElements(graphState), [graphState]);

  // Dynamic height: base 120px + 56px per node row, clamped to reasonable range
  const computedHeight = height ?? Math.max(160, Math.min(500, 120 + nodes.length * 56));

  const onNodeClick: NodeMouseHandler = useCallback(() => {}, []);

  if (nodes.length === 0) {
    return (
      <div className="flex items-center justify-center text-[--color-text2] text-xs" style={{ height: computedHeight }}>
        No workflow events available
      </div>
    );
  }

  return (
    <div style={{ height: computedHeight }} className="rounded-lg border border-[--color-border] overflow-hidden">
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
