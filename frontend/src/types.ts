export interface Site {
  id: number;
  code: string;
  name: string;
  timezone: string;
  diff_threshold_pct: string;
}

export interface Bill {
  id: number;
  bill_no: string;
  site_id: number;
  job_id: number;
  master_total_kwh: string;
  charging_kwh: string;
  estimated_kwh: string;
  pending_kwh: string;
  pending_order_count: number;
  public_kwh: string;
  unexplained_kwh: string;
  suspended_kwh: string;
  charging_amount: string;
  public_amount: string;
  status: string;
  confirmed_at: string | null;
  confirmed_by: string | null;
}

export interface Job {
  id: number;
  site_id: number;
  cycle_start_local: string;
  cycle_end_local: string;
  status: string;
  threshold_pct: string;
  rerun_of_job_id: number | null;
  message: string | null;
}

export interface Interval {
  id: number;
  start_ts_local: string;
  end_ts_local: string;
  master_reading_start: string | null;
  master_reading_end: string | null;
  master_energy_kwh: string | null;
  device_meter_kwh: string;
  charging_kwh: string;
  estimated_kwh: string;
  public_kwh: string;
  unexplained_kwh: string;
  order_meter_diff_kwh: string;
  basis_status: string;
  note: string | null;
}

export interface Split {
  id: number;
  order_id: number;
  start_ts_local: string;
  end_ts_local: string;
  segment_name: string;
  price_per_kwh: string;
  energy_kwh: string | null;
  amount: string | null;
  basis_status: string;
  note: string | null;
}

export interface ReviewTask {
  id: number;
  site_id: number;
  job_id: number | null;
  task_kind: string;
  title: string;
  detail: string;
  assignee: string;
  status: string;
  response: string | null;
  responded_by: string | null;
}

export interface Conflict {
  id: number;
  site_id: number;
  device_id: string;
  order_a_id: number;
  order_b_id: number;
  status: string;
  resolved_order_id: number | null;
  resolution_note: string | null;
}

export interface ImportFile {
  id: number;
  site_id: number;
  source_type: string;
  original_name: string;
  sha256: string;
  row_count: number;
  imported_by: string;
  imported_at: string;
}

export interface ImportRow {
  id: number;
  line_no: number;
  raw_line: string;
  outcome: string;
  source_record_no: string | null;
  note: string | null;
}

export interface Adjustment {
  id: number;
  component: string;
  original_value: string;
  corrected_value: string;
  delta: string;
  reason: string;
  case_id: number | null;
  residual_id: number | null;
  adoption_id: number | null;
  created_by: string;
  created_at: string;
}

export interface TopologyEdge {
  id: number;
  site_id: number;
  parent_meter_no: string;
  child_meter_no: string;
  parent_role: string;
  child_role: string;
  device_id: string | null;
  effective_ts_local: string;
  end_ts_local: string | null;
  evidence_note: string;
  status: string;
}

export interface TopologyIssue {
  id: number;
  site_id: number;
  issue_kind: string;
  subject: string;
  detail: string;
  detected_at_local: string;
  status: string;
}

export interface Residual {
  id: number;
  job_id: number;
  site_id: number;
  parent_meter_no: string;
  parent_role: string;
  subject: string;
  start_ts_local: string;
  end_ts_local: string;
  parent_kwh: string | null;
  known_children_kwh: string;
  residual_kwh: string | null;
  residual_pct: string | null;
  order_gap_kwh: string;
  uncovered_kwh: string;
  residual_kind: string;
  has_order_gap: boolean;
  has_reading_gap: boolean;
  suspect: boolean;
  continues_previous: boolean;
  case_id: number | null;
  note: string | null;
}

export interface OrderGap {
  id: number;
  job_id: number;
  site_id: number;
  device_id: string | null;
  start_ts_local: string;
  end_ts_local: string;
  gap_kind: string;
  missing_kwh: string | null;
  covered_seconds: number;
  total_seconds: number;
  note: string | null;
}

export interface ResidualSummary {
  id: number;
  job_id: number;
  site_id: number;
  status: string;
  master_total_kwh: string;
  known_children_kwh: string;
  branch_residual_kwh: string;
  uncovered_kwh: string;
  top_residual_kwh: string;
  order_gap_kwh: string;
  gap_windows: string | null;
  concentrated_branches: string | null;
  conclusion: string;
  blocked_reason: string | null;
  copied_from_job_id: number | null;
}

export interface FieldCase {
  id: number;
  site_id: number;
  job_id: number;
  residual_id: number | null;
  meter_no: string | null;
  device_id: string | null;
  branch_path: string;
  start_ts_local: string;
  end_ts_local: string;
  residual_kwh: string | null;
  reason: string;
  status: string;
  assignee: string;
  handling_note: string | null;
  handled_by: string | null;
  handled_at: string | null;
  superseded_by_job_id: number | null;
  superseded_reason: string | null;
}

export interface CaseEvidence {
  id: number;
  case_id: number;
  site_id: number;
  evidence_kind: string;
  claim_kwh: string | null;
  description: string;
  attachment_ref: string | null;
  submitted_by: string;
  submitted_at: string;
  would_reduce_payable: boolean;
}

export interface Adoption {
  id: number;
  case_id: number;
  evidence_id: number;
  site_id: number;
  bill_id: number | null;
  decision: string;
  adopted_kwh: string | null;
  allowed_to_reduce_payable: boolean;
  reason: string;
  decided_by: string | null;
  decided_at: string | null;
}
