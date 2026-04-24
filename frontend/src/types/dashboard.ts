/** Types matching the Python backend's /api/dashboard response. */

export interface DashboardData {
  active_runs: RunData[];
  abandoned_runs: RunData[];
  completed_runs: RunData[];
  failed_runs: RunData[];
  other_runs: RunData[];
  stats: DashboardStats;
  costs: CostSummary;
  errors: ErrorSummary;
  metrics: MetricsSummary;
  runs_raw: RawRun[];
}

export interface DashboardStats {
  total: number;
  completed: number;
  failed: number;
  active: number;
  gates_waiting: number;
  gates_abandoned: number;
  abandoned: number;
  total_cost: number;
  total_tokens: number;
  checkpoints: number;
}

export interface SystemMeta {
  pid?: number;
  platform?: string;
  python_version?: string;
  conductor_version?: string;
  cwd?: string;
  started_at?: string;
  run_id?: string;
  log_file?: string;
  bg_mode?: boolean;
  dashboard_port?: number;
  dashboard_url?: string;
  parent_pid?: number;
}

export interface RunData {
  log_file: string;
  name: string;
  started_at: number;
  started_at_str: string;
  ended_at: number;
  ended_at_str: string;
  elapsed: string;
  status: RunStatus;
  status_icon: string;
  error_type: string;
  error_message: string;
  failed_agent: string;
  total_cost: number;
  cost_str: string;
  total_tokens: number;
  tokens_str: string;
  agents: AgentData[];
  agent_count: number;
  current_agent: string;
  current_agent_type: AgentType;
  gate_waiting: boolean;
  gate_agent: string;
  iteration: number;
  purpose: string;
  work_item_id: string;
  work_item_title: string;
  work_item_type: string;
  work_item_url: string;
  run_id: string;
  metadata: Record<string, unknown>;
  system_meta: SystemMeta;
  dashboard_port: number;
  dashboard_url: string;
  replay_cmd: string;
  review_available: boolean;
  review_skill_path: string;
  cwd: string;
  worktree: WorktreeInfo;
  process_alive: boolean;
  hierarchy: HierarchyData | null;
  subworkflows: SubworkflowData[];
}

export type RunStatus = 'completed' | 'failed' | 'running' | 'timeout' | 'interrupted' | 'parse_error' | 'unknown';
export type AgentType = 'agent' | 'human_gate' | 'script' | 'workflow' | '';

export interface AgentData {
  name: string;
  model: string;
  elapsed: number;
  tokens: number;
  cost_usd: number;
}

export interface SubworkflowData {
  workflow: string;
  agent: string;
  item_key: string;
  iteration: number;
  status: string;
  elapsed: number;
}

export interface WorktreeInfo {
  branch?: string;
  name?: string;
  toplevel?: string;
}

export interface HierarchyData {
  focus: HierarchyFocus;
  levels: HierarchyLevel[];
  ancestors: HierarchyAncestor[];
  /** State definitions per work item type from twig DB process_types */
  type_defs?: Record<string, TypeStateDef[]>;
  /** Hex color per work item type name (e.g. Epic→"E06C00") */
  type_colors?: Record<string, string>;
}

export interface HierarchyFocus {
  id: number;
  type: string;
  title: string;
  state: string;
}

/** Per-level breakdown with raw state counts */
export interface HierarchyLevel {
  type: string;
  /** Raw state counts: e.g. {"To Do": 3, "Doing": 1, "Done": 5} */
  states: Record<string, number>;
  total: number;
}

export interface HierarchyAncestor {
  id: number;
  type: string;
  title: string;
  state: string;
}

/** State definition from twig DB process_types.states_json */
export interface TypeStateDef {
  name: string;
  category: 'Proposed' | 'InProgress' | 'Completed' | 'Removed';
  color: string; // hex without #
}

export interface CostSummary {
  total: number;
  total_tokens: number;
  by_workflow: Record<string, number>;
  by_model: Record<string, number>;
}

export interface ErrorSummary {
  error_types: Record<string, number>;
  agent_failures: Record<string, number>;
}

export interface MetricsSummary {
  by_workflow: Record<string, WorkflowMetric>;
  by_model: Record<string, ModelMetric>;
  by_agent: Record<string, AgentMetric>;
  top_agents_by_cost: TopAgent[];
  error_types: Record<string, number>;
  agent_failures: Record<string, number>;
  totals: MetricsTotals;
}

export interface WorkflowMetric {
  runs: number;
  completed: number;
  failed: number;
  total_cost: number;
  total_tokens: number;
  total_runtime_sec: number;
  avg_duration_sec: number;
  success_rate: number;
}

export interface ModelMetric {
  cost: number;
  tokens: number;
  invocations: number;
}

export interface AgentMetric {
  invocations: number;
  total_cost: number;
  total_tokens: number;
  total_elapsed: number;
  avg_elapsed: number;
}

export interface TopAgent {
  name: string;
  total_cost: number;
  invocations: number;
  total_tokens: number;
}

export interface MetricsTotals {
  cost: number;
  tokens: number;
  runs: number;
  completed: number;
  failed: number;
}

export interface RawRun {
  log_file: string;
  name: string;
  status: RunStatus;
  started_at: number;
  ended_at: number | null;
  run_id: string;
  total_cost: number;
  total_tokens: number;
  agents: AgentData[];
  failed_agent: string;
  error_type: string;
  duration_sec: number;
}

/** SSE event types from /api/events */
export type SSEEventType = 'snapshot' | 'update' | 'ping';
