export type Accountability = {
  status: 'unreviewed'|'pending_operator'|'confirmed'|'explained'|'not_applicable';
  awaiting_closure?:boolean; detection_ko: string | null; recovery_ko: string | null; recovery_observed_at: string | null;
  recovery_scope?: 'hpa_resource_metrics'; provider_recovered_at?: string | null; hpa_recovered_at?: string | null;
  limits_ko: string | string[] | null; evidence_refs: string[];
  decision_at: string | null; decision_by: string | null; decision_note: string | null; revision: number;
};
