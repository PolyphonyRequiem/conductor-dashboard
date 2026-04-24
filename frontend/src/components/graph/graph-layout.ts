/** Dagre-based graph layout for workflow DAG visualization */
import dagre from '@dagrejs/dagre';
import type { Node, Edge } from '@xyflow/react';
import type { GraphState } from '@/types/events';

export interface LayoutNodeData {
  [key: string]: unknown;
  label: string;
  type: string;
  status: string;
  elapsed?: number;
  tokens?: number;
  cost_usd?: number;
  model?: string;
  error_type?: string;
  startedAt?: number;
}

const NODE_WIDTH = 180;
const NODE_HEIGHT = 48;

export function buildGraphElements(state: GraphState): { nodes: Node<LayoutNodeData>[]; edges: Edge[] } {
  const g = new dagre.graphlib.Graph();
  g.setDefaultEdgeLabel(() => ({}));
  g.setGraph({ rankdir: 'LR', nodesep: 30, ranksep: 80, marginx: 20, marginy: 20 });

  // Add start node
  if (state.entryPoint) {
    g.setNode('$start', { width: 40, height: 40 });
    g.setEdge('$start', state.entryPoint);
  }

  // Add agent nodes
  for (const agent of state.agents) {
    g.setNode(agent.name, { width: NODE_WIDTH, height: NODE_HEIGHT });
  }
  // Also add nodes that appeared in events but not in agents list
  for (const [name] of Object.entries(state.nodes)) {
    if (!g.hasNode(name)) {
      g.setNode(name, { width: NODE_WIDTH, height: NODE_HEIGHT });
    }
  }

  // Add route edges
  for (const route of state.routes) {
    if (route.to === '$end') {
      if (!g.hasNode('$end')) {
        g.setNode('$end', { width: 40, height: 40 });
      }
    }
    g.setEdge(route.from, route.to);
  }

  dagre.layout(g);

  // Build ReactFlow nodes
  const nodes: Node<LayoutNodeData>[] = [];

  if (state.entryPoint && g.hasNode('$start')) {
    const pos = g.node('$start');
    nodes.push({
      id: '$start',
      type: 'startNode',
      position: { x: pos.x - 20, y: pos.y - 20 },
      data: { label: 'Start', type: 'start', status: 'completed' },
    });
  }

  for (const agent of state.agents) {
    const pos = g.node(agent.name);
    if (!pos) continue;
    const nodeState = state.nodes[agent.name];
    nodes.push({
      id: agent.name,
      type: agentNodeType(agent.type),
      position: { x: pos.x - NODE_WIDTH / 2, y: pos.y - NODE_HEIGHT / 2 },
      data: {
        label: agent.name,
        type: agent.type,
        status: nodeState?.status ?? 'pending',
        elapsed: nodeState?.elapsed,
        tokens: nodeState?.tokens,
        cost_usd: nodeState?.cost_usd,
        model: nodeState?.model,
        error_type: nodeState?.error_type,
        startedAt: nodeState?.startedAt,
      },
    });
  }

  // Add $end node if referenced
  if (g.hasNode('$end')) {
    const pos = g.node('$end');
    nodes.push({
      id: '$end',
      type: 'endNode',
      position: { x: pos.x - 20, y: pos.y - 20 },
      data: { label: 'End', type: 'end', status: state.workflowStatus === 'completed' ? 'completed' : 'pending' },
    });
  }

  // Build edges
  const edges: Edge[] = [];
  if (state.entryPoint) {
    edges.push({
      id: `$start->${state.entryPoint}`,
      source: '$start',
      target: state.entryPoint,
      animated: state.workflowStatus === 'running',
      style: { stroke: '#58a6ff' },
    });
  }
  for (const route of state.routes) {
    const edgeKey = `${route.from}->${route.to}`;
    const isTaken = state.takenEdges.has(edgeKey);
    const isFailed = state.failedEdges.has(edgeKey);
    edges.push({
      id: edgeKey,
      source: route.from,
      target: route.to,
      animated: isTaken && state.workflowStatus === 'running',
      style: {
        stroke: isFailed ? '#f85149' : isTaken ? '#3fb950' : '#30363d',
        strokeWidth: isTaken ? 2 : 1,
        opacity: isTaken ? 1 : 0.5,
      },
      label: route.condition ? route.condition : undefined,
      labelStyle: { fill: '#8b949e', fontSize: 10 },
    });
  }

  return { nodes, edges };
}

function agentNodeType(type: string): string {
  switch (type) {
    case 'human_gate': return 'gateNode';
    case 'script': return 'scriptNode';
    case 'workflow': return 'workflowNode';
    default: return 'agentNode';
  }
}
