/** Conductor workflow event types (subset needed for graph rendering) */

export type EventType =
  | 'workflow_started' | 'workflow_completed' | 'workflow_failed'
  | 'agent_started' | 'agent_completed' | 'agent_failed'
  | 'script_started' | 'script_completed' | 'script_failed'
  | 'gate_presented' | 'gate_resolved'
  | 'route_taken'
  | 'parallel_started' | 'parallel_completed'
  | 'subworkflow_started' | 'subworkflow_completed' | 'subworkflow_failed';

export interface WorkflowEvent {
  type: EventType;
  timestamp: number;
  data: Record<string, unknown>;
}

export type NodeStatus = 'pending' | 'running' | 'completed' | 'failed' | 'waiting';

export interface GraphNodeState {
  name: string;
  status: NodeStatus;
  type: string; // agent, script, human_gate, workflow
  elapsed?: number;
  model?: string;
  tokens?: number;
  cost_usd?: number;
  error_type?: string;
  startedAt?: number;
}

export interface WorkflowAgent {
  name: string;
  type: string;
}

export interface RouteEdge {
  from: string;
  to: string;
  condition?: string;
}

export interface GraphState {
  agents: WorkflowAgent[];
  routes: RouteEdge[];
  entryPoint: string | null;
  nodes: Record<string, GraphNodeState>;
  workflowStatus: 'pending' | 'running' | 'completed' | 'failed';
  takenEdges: Set<string>;
  failedEdges: Set<string>;
}
