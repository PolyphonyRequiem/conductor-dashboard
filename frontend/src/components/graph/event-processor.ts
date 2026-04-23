/** Process conductor events into graph state for visualization */
import type { WorkflowEvent, GraphState } from '@/types/events';

export function createInitialGraphState(): GraphState {
  return {
    agents: [],
    routes: [],
    entryPoint: null,
    nodes: {},
    workflowStatus: 'pending',
    takenEdges: new Set(),
    failedEdges: new Set(),
  };
}

/** Process a sequence of events and return the resulting graph state */
export function processEvents(events: WorkflowEvent[]): GraphState {
  const state = createInitialGraphState();
  for (const event of events) {
    processEvent(state, event);
  }
  return state;
}

/** Process a single event, mutating state in place */
export function processEvent(state: GraphState, event: WorkflowEvent): void {
  const d = event.data;
  const name = (d.agent_name ?? d.name ?? '') as string;

  switch (event.type) {
    case 'workflow_started': {
      state.workflowStatus = 'running';
      state.entryPoint = (d.entry_point ?? '') as string;
      const agents = (d.agents ?? []) as Array<{ name: string; type?: string }>;
      state.agents = agents.map((a) => ({ name: a.name, type: a.type ?? 'agent' }));
      const routes = (d.routes ?? []) as Array<{ from: string; to: string; when?: string }>;
      state.routes = routes.map((r) => ({ from: r.from, to: r.to, condition: r.when }));
      // Initialize all nodes as pending
      for (const agent of state.agents) {
        state.nodes[agent.name] = {
          name: agent.name,
          status: 'pending',
          type: agent.type,
        };
      }
      break;
    }
    case 'agent_started':
    case 'script_started': {
      ensureNode(state, name, event.type === 'script_started' ? 'script' : 'agent');
      state.nodes[name]!.status = 'running';
      state.nodes[name]!.startedAt = event.timestamp;
      break;
    }
    case 'agent_completed':
    case 'script_completed': {
      ensureNode(state, name);
      const node = state.nodes[name]!;
      node.status = 'completed';
      node.elapsed = (d.elapsed ?? 0) as number;
      node.tokens = (d.tokens ?? 0) as number;
      node.cost_usd = (d.cost_usd ?? 0) as number;
      node.model = (d.model ?? '') as string;
      node.startedAt = undefined;
      break;
    }
    case 'agent_failed':
    case 'script_failed': {
      ensureNode(state, name);
      const node = state.nodes[name]!;
      node.status = 'failed';
      node.error_type = (d.error_type ?? '') as string;
      node.elapsed = (d.elapsed ?? 0) as number;
      node.startedAt = undefined;
      break;
    }
    case 'gate_presented': {
      ensureNode(state, name, 'human_gate');
      state.nodes[name]!.status = 'waiting';
      break;
    }
    case 'gate_resolved': {
      ensureNode(state, name, 'human_gate');
      state.nodes[name]!.status = 'completed';
      break;
    }
    case 'route_taken': {
      const from = (d.from ?? '') as string;
      const to = (d.to ?? '') as string;
      if (from && to) {
        state.takenEdges.add(`${from}->${to}`);
      }
      break;
    }
    case 'workflow_completed':
      state.workflowStatus = 'completed';
      break;
    case 'workflow_failed':
      state.workflowStatus = 'failed';
      break;
    case 'subworkflow_started': {
      ensureNode(state, name, 'workflow');
      state.nodes[name]!.status = 'running';
      state.nodes[name]!.startedAt = event.timestamp;
      break;
    }
    case 'subworkflow_completed': {
      ensureNode(state, name, 'workflow');
      state.nodes[name]!.status = 'completed';
      state.nodes[name]!.elapsed = (d.elapsed ?? 0) as number;
      state.nodes[name]!.startedAt = undefined;
      break;
    }
    case 'subworkflow_failed': {
      ensureNode(state, name, 'workflow');
      state.nodes[name]!.status = 'failed';
      state.nodes[name]!.startedAt = undefined;
      break;
    }
  }
}

function ensureNode(state: GraphState, name: string, type?: string) {
  if (!name) return;
  if (!state.nodes[name]) {
    state.nodes[name] = { name, status: 'pending', type: type ?? 'agent' };
  } else if (type) {
    state.nodes[name]!.type = type;
  }
}
