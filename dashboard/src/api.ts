import type { ContractEdge, ObservedEdge, Topology, TopoNode } from './mapModel';
import type { CostStatus, IncidentOrigin } from './cost';
export { costKo, money, ORIGIN_KO, type CostStatus, type IncidentOrigin } from './cost';

export type Status = 'recovery_review' | 'open' | 'analyzing' | 'action_pending' | 'acting' | 'resolved' | 'handed_off' | 'failed' | 'closed';
export type Level = 'SIMPLE' | 'NORMAL' | 'COMPLEX' | 'HIGH_RISK';
export type Citation = { chunk_id: number; doc: string; source: string; chunk_ix: number; score: number; content: string; source_uri?: string | null; locator?: string | null };
export type Incident = { id: number; title: string; title_ko?: string | null; namespace: string; system?: string | null; environment?: string | null; workload: string; status: Status; kubernetes_status?: Status | null; severity_level: Level | null; signal_count: number; created_at: string; updated_at: string; closed_at: string | null; total_cost_usd: number | null; agent_run_count: number; summary?: string | null; status_reason?: string | null; signal_reasons?: string[]; handoff?: Record<string, unknown> | null; family_hint?: string | null; stage?: Stage | null;
  /** 정돈-07: shared cost contract. Absent on older servers → fall back to total_cost_usd only. */
  known_cost_usd?: number | null; cost_missing_runs?: number | null; cost_status?: CostStatus | null;
  /** 정돈-04: provenance from an explicit eval/ marker only; everything else stays 'unconfirmed'. */
  origin?: IncidentOrigin | null; recovery_kind?: 'observed' | null };
export type IncidentPage = { items: Incident[]; total: number; as_of?: number; newer_since_as_of?: number };

// ---------------------------------------------------------------- P15 incident war room (GET /incidents/{id}/narrative)
export type Stage = { key: string; label: string };
export type RawItem = {
  id: string; ref: string; kind: 'event' | 'log' | 'status' | 'metric' | 'config' | 'node' | 'agent' | string;
  source: string; at: string | null; raw: string; severity: 'info' | 'warn' | 'error' | string;
  reading_state?: string; ko: string | null; ko_source: 'rules' | 'llm' | null; cited_by: string[];
};
export type FlowNodeType = 'signal' | 'evidence' | 'router' | 'group' | 'agent' | 'verify' | 'action' | 'outcome' | 'steer' | 'command' | 'interpret';
export type FlowNodeStatus = 'planned' | 'running' | 'done' | 'failed' | 'skipped' | 'escalated';
export type Dispatch = { version: 'dispatch/v1'; role_ko: string; task_ko: string; evidence_refs: string[]; rag_refs: string[]; operator_message_ids: number[]; prompt_sha256: string; prompt_chars: number };
export type FlowNode = {
  dispatch?: Dispatch;
  source_activity?: {at:number; phase:string; file:string|null; attempt:number; job_id:string; seq:number}[];
  id: string; type: FlowNodeType | string; label: string; role: string | null; role_ko: string; job_ko: string;
  status: FlowNodeStatus | string; model: string | null; effort: string | null; tokens: { in: number | null; out: number | null } | null;
  cost_usd: number | null; duration_ms: number | null; inputs: string[]; conclusion_ko: string | null; conclusion_raw: string | null;
  lane: number; depth: number; group: Level | null;
  /** P18 W6: optional until the backend ships them. Absent means 미기록, never a fabricated value. */
  at?: string | null; card?: FlowCard | null; aliases?: string[]; call_id?: string | null; agent_run_id?: number | null;
  /** P20: why this run was replaced by a higher model (on the escalated node) / what it replaced (on the successor). */
  escalation?: Escalation | null; escalated_from?: Escalation | null;
};
export type Escalation = { reason_raw: string; reason_ko: string; from_model: string | null; from_effort: string | null; to_model: string | null; to_effort: string | null };
/** P20: an agent's own cited excerpt. `ref_known` = the id resolves to evidence collected for this incident or a cited reference document. */
export type AgentQuote = { ref: string; ref_ko: string | null; ref_known: boolean; quote: string; supports_raw: string; candidate_index: number | null };
export type CounterEvidence = { text_raw: string; text_ko: string | null };
export type CandidateVerdict = { index: number | null; verdict: string | null; verdict_ko: string | null; reason_raw: string; reason_ko: string | null };
export type CardCitation = { chunk_id: number; doc: string; source: string; chunk_ix: number; score: number | null; section: string | null; locator: string | null; source_uri: string | null; content: string };
// ---------------------------------------------------------------- P18 W6 structured step cards (FlowNode.card)
export type SignalCard = { kind: 'signal'; what_ko: string; place_ko: string; source: string | null; reason: string | null; source_ko?: string | null; reason_ko?: string | null; signal_count: number; first_at: string | null };
export type EvidenceItem = { id: number; source: string; source_ko: string; target: string; ok: boolean; error: string | null; summary_ko: string | null; ref: string | null; collected_at: string | null; raw_items?: RawItem[] };
export type EvidenceCard = { kind: 'evidence'; total: number; ok: number; failed: number; items: EvidenceItem[] };
export type DecisionStep = { key: 'features' | 'rule' | 'llm' | 'final' | 'model' | string; title_ko: string; detail_ko: string; items_ko: string[]; source: 'rules' | 'llm' | 'policy' | string };
export type RoutingCard = { kind: 'routing'; routing_id: number; level: string; level_ko: string; decided_by: string; decided_by_ko: string; confidence: number | null;
  steps: DecisionStep[]; final_reason_ko: string | null; reason_raw: string;
  llm: { level: string; confidence: number; rationale_raw: string; rationale_ko: string | null } | null;
  model_policy: { selected_model: string | null; reason_ko: string; candidates: { model: string; eligible: boolean; score: number | null; reason_ko: string }[] } | null;
  interpreting: boolean };
export type GroupRole = { role: string; role_ko: string; job_ko: string; model: string | null; effort: string | null; status: string };
export type GroupCard = { kind: 'group'; routing_id: number; level: string; group_ko: string;
  decision: { level_ko: string; decided_by_ko: string; confidence: number | null; final_reason_ko: string | null; steps: DecisionStep[] };
  roles: GroupRole[]; budget: { max_agent_calls: number | null; max_cost_usd: number | null; deadline_s: number | null } | null; progress_ko: string[]; status_ko: string };
export type AgentCard = { kind: 'agent'; agent_run_id: number | null; summary_raw: string | null; summary_ko: string | null;
  candidates: { cause_raw: string; cause_ko?: string | null; confidence: number | null; verifier: string | null; evidence_refs?: string[] }[];
  /** P20 explainability fields; absent on older servers (shown as 미기록, never invented). */
  proposed_action?:{type_ko:string;target:string;impact_ko:string|null;success_condition_ko:string|null;rollback_condition_ko:string|null};
  recommended_steps_ko?: (string|null)[]; confidence?: number | null; quotes?: AgentQuote[]; counter_evidence?: CounterEvidence[]; candidate_verdicts?: CandidateVerdict[];
  citations?: CardCitation[]; acceptance?: { rejected: boolean; reason_raw: string; reason_ko: string } | null;
  verdict: string | null; verdict_ko: string | null; next_action: string | null; next_action_ko: string | null;
  counter_evidence_count: number; error: string | null; error_ko?: string | null; accepts_steering: boolean; steer_target_role: string };
export type ActionCard = { kind: 'action'; action_id: number; type: string; type_ko: string; target: string; status: string; status_ko: string; changes: string[] };
export type HandoffDecision = { key: 'outcome' | 'cause' | 'auto_action' | 'operator' | string; label_ko: string; value_ko: string };
export type HandoffView = { summary_ko: string | null; summary_raw: string | null; summary_source?: 'interpret' | 'rules' | string; decisions: HandoffDecision[]; next_steps_ko: string[]; next_steps_raw: string[]; interpreting: boolean };
export type OutcomeCard = { kind: 'outcome'; status: string; status_ko: string; handoff: HandoffView | null; handoff_recorded_at?:string|null; handoff_claimed?:boolean; closed_from?:string|null };
export type FlowCard = SignalCard | EvidenceCard | RoutingCard | GroupCard | AgentCard | ActionCard | OutcomeCard;
/** POST /incidents/{id}/messages response (target fields are P18 W6 and may be absent on older servers). */
export type MessageResponse = { message_id: number; intent?: string; target_level?: string | null; ack_ko?: string | null; applies?: string; target_role?: string | null; target_node?: string | null; hold?: boolean };
export type FlowEdge = { id: string; source: string; target: string; kind: 'flow' | 'escalate' | 'reroute' | 'steer' | 'verify' | string; label: string | null };
export type Narrative = {
  accountability?: import('./accountability').Accountability;
  /** View metadata only: stored incident and execution statuses are preserved. */
  view_mode?: 'live' | 'snapshot';
  incident: Incident;
  headline: { what: string; impact: string; stage: Stage; next: string; source: string };
  raw: RawItem[];
  flow: { nodes: FlowNode[]; edges: FlowEdge[] };
  interpreting: boolean;
  /** 정돈-02: reading never schedules interpretation. pipeline = an active incident's own pipeline will interpret;
   *  not_requested = an explicit POST /incidents/{id}/interpretations is needed (model call, cost). */
  interpret_state?: { status: 'pending' | 'unavailable' | 'disabled' | 'pipeline' | 'not_requested' | string; reason_ko: string } | null;
  interpret_pending_count?: number;
  handoff_view?: HandoffView | null;
};
export type InterpretRequestResult = { status: 'accepted' | 'running' | 'queued' | 'nothing_to_interpret' | 'unavailable' | string; message_ko?: string | null };

// ---------------------------------------------------------------- P15 operator commands
export type CommandStatus = 'planning' | 'awaiting_confirmation' | 'awaiting_secret' | 'executing' | 'succeeded' | 'failed' | 'cancelled' | 'rejected' | 'rolling_back' | 'rolled_back';
export type Operation = { op: string; apiVersion?: string; kind: string; namespace?: string | null; name: string; manifest?: unknown; patch?: unknown; replicas?: number; keys?: string[] };
export type DryRun = { op_index: number; ok: boolean; diff: { path: string; before: unknown; after: unknown }[] | null; error: string | null };
export type SideEffects = { items_ko: string[]; targets: string[]; restarts: boolean; downtime: string; reversible: boolean; rollback_ko: string; cluster_wide: boolean; warnings_ko: string[] };
export type Command = {
  id: number; incident_id: number; instruction: string; status: CommandStatus | string;
  plan: { summary_ko: string; operations: Operation[] } | null; plan_sha256: string | null; dry_run: DryRun[] | null;
  side_effects: SideEffects | null; confirm_question: string; result: unknown; verification: unknown; rollback: unknown;
  error: string | null; created_at: string; updated_at: string;
};

// ---------------------------------------------------------------- P15 operations overview (GET /overview)
export type ClusterNode = { name: string; ready: boolean | null; cpu_cores: number | null; cpu_used_cores: number | null; mem_bytes: number | null; mem_used_bytes: number | null; pods: number | null };
export type EnvironmentStat = { name: string; order: number; workloads: number | null; pods_ready: number | null; pods_total: number | null; open_incidents: number | null; missing?: true };
export type NamespaceStat = { name: string; pods_running: number | null; pods_pending: number | null; pods_failed: number | null; restarts_1h: number | null; cpu_cores: number | null; mem_bytes: number | null; workloads: number | null; workloads_unhealthy: number | null };
export type WorkloadIncident = { id: number; level: Level | string | null; status: string; stage: Stage | null; system?: string | null; environment?: string | null };
export type Workload = { metric_at?:number|null; id: string; namespace: string; system?: string | null; environment?: string | null; name: string; kind: string; ready: number | null; desired: number | null; restarts_1h: number | null; cpu: number | null; mem: number | null; req_rate: number | null; err_rate: number | null; p95_ms: number | null; incident: WorkloadIncident | null };
export type ClusterEvent = { at: string | null; namespace: string | null; object: string; reason: string; type: string; message: string; count: number | null };
export type OpenIncident = { id: number; title: string; namespace: string; system?: string | null; environment?: string | null; workload: string; severity_level: Level | string | null; status: string; stage: Stage | null; created_at: string };
export type ActiveIncident = Omit<OpenIncident, 'stage'> & {
  title_ko?: string | null;
  claimed?: boolean;
  handoff?: Record<string, unknown> | null;
  code_action?: { state?: string };
};
/** 정돈-05: one bucket definition shared with the header and incident list (server: app/incident_counts.py). */
export type IncidentCounts = { automatic: number; awaiting_approval: number; unclaimed_handoff: number; in_progress: number; closed_history: number;
  map_handoff_window_hours: number; by_environment: Record<string, { automatic: number; awaiting_approval: number; unclaimed_handoff: number; in_progress: number }> };
export type Overview = {
  inventory_observed_at?:string|null;
  preview_notice?:{source_ko:string;observed_at:string;message_ko:string};
  generated_at: string | null;
  cluster: { nodes: ClusterNode[]; namespaces: NamespaceStat[]; environments?: EnvironmentStat[] };
  workloads: Workload[];
  events: ClusterEvent[];
  incidents: { open: OpenIncident[]; today: { total: number | null; resolved: number | null; handed_off: number | null; failed: number | null; closed?: number | null }; counts?: IncidentCounts };
  topology: Topology | null;
  sources: Record<string, string>;
};
type AtlasResource = Pick<TopoNode, 'id' | 'kind' | 'name' | 'namespace' | 'system' | 'environment' | 'lifecycle' |
  'status' | 'role' | 'endpoints_ready' | 'network_warnings' | 'no_endpoints'> & {
  services?: Pick<NonNullable<TopoNode['services']>[number], 'name' | 'endpoints_ready'>[];
  warning?: boolean;
};
export type AtlasOverview = Pick<Overview, 'generated_at' | 'inventory_observed_at' | 'preview_notice' | 'sources'> & {
  workloads: Pick<Workload, 'id' | 'name' | 'system' | 'environment' | 'namespace' | 'kind' | 'ready' | 'desired' |
    'req_rate' | 'err_rate' | 'metric_at'>[];
  incidents: Pick<Overview['incidents'], 'open' | 'counts'>;
  topology: {
    nodes: AtlasResource[];
    contract_edges?: Pick<ContractEdge, 'from' | 'to'>[];
    observed_edges: Pick<ObservedEdge, 'from' | 'to' | 'last_seen' | 'req_rate' | 'err_rate' | 'metric_at'>[];
  } | null;
};

export class ApiError extends Error { constructor(public status: number, message: string) { super(message); } }
/** Non-secret CSRF challenge issued by the loopback gateway on document navigation. */
export function localOperatorCsrf(): string | null {
  if (typeof document === 'undefined') return null;
  return document.cookie.match(/(?:^|;\s*)lp_operator_csrf=([a-f0-9]{64}\.[a-f0-9]{64})(?:;|$)/)?.[1] ?? null;
}
export async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const csrf = options?.method && !['GET', 'HEAD'].includes(options.method.toUpperCase()) ? localOperatorCsrf() : null;
  const response = await fetch(`/api${path}`, { ...options, credentials: 'same-origin', headers: { 'X-LiveProbe-Client': 'dashboard', ...(options?.body ? { 'Content-Type': 'application/json' } : {}), ...options?.headers, ...(csrf ? { 'X-LiveProbe-Operator-CSRF': csrf } : {}) } });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(response.status, typeof body.detail === 'string' ? body.detail : `HTTP ${response.status}`);
  }
  if (response.status === 204) return undefined as T;
  return response.json() as Promise<T>;
}
// Existing unattended-engine actions (GET /incidents/{id}); approval goes through POST /actions/{id}/approve|reject.
export type Action = { id: number; type: string; target: string; params: unknown; policy_mode: string; status: string; reason: string | null; executed: boolean; created_at: string | null; updated_at: string | null };
export type IncidentDetail = { incident: Incident; actions: Action[] };
export const dateTime = (value: string | null | undefined) => value ? new Date(value).toLocaleString('ko-KR') : '시각 미기록';
export const clock = (value: string | null | undefined) => value ? new Date(value).toLocaleTimeString('ko-KR', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '시각 미기록';
/** Elapsed time in Korean; `until` freezes it for finished incidents. */
export const age = (value: string, until?: string | null, now: number = Date.now()) => {
  const end = until ? new Date(until).getTime() : now;
  const mins = Math.max(0, Math.floor((end - new Date(value).getTime()) / 60000));
  return mins < 60 ? `${mins}분` : mins < 1440 ? `${Math.floor(mins / 60)}시간 ${mins % 60}분` : `${Math.floor(mins / 1440)}일`;
};
export const display = (value: unknown) => typeof value === 'string' ? value : JSON.stringify(value, null, 2);
