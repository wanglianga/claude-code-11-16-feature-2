from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class LoginIn(BaseModel):
    username: str
    password: str


class TokenOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    role: str
    display_name: str


class SiteOut(ORMModel):
    id: int
    code: str
    name: str
    timezone: str
    diff_threshold_pct: Decimal


class ImportResultOut(BaseModel):
    import_file_id: int
    row_count: int
    summary: dict[str, int]


class ImportFileOut(ORMModel):
    id: int
    site_id: int
    source_type: str
    original_name: str
    sha256: str
    row_count: int
    imported_by: str
    imported_at: datetime


class ImportRowOut(ORMModel):
    id: int
    line_no: int
    raw_line: str
    outcome: str
    source_record_no: str | None
    note: str | None


class JobIn(BaseModel):
    site_id: int
    cycle_start: datetime  # 站点本地时间
    cycle_end: datetime
    rerun_of_job_id: int | None = None


class JobOut(ORMModel):
    id: int
    site_id: int
    cycle_start_local: datetime
    cycle_end_local: datetime
    status: str
    threshold_pct: Decimal
    rerun_of_job_id: int | None
    message: str | None
    started_at: datetime
    finished_at: datetime | None


class IntervalOut(ORMModel):
    id: int
    start_ts_local: datetime
    end_ts_local: datetime
    master_reading_start: Decimal | None
    master_reading_end: Decimal | None
    master_energy_kwh: Decimal | None
    device_meter_kwh: Decimal
    charging_kwh: Decimal
    estimated_kwh: Decimal
    public_kwh: Decimal
    unexplained_kwh: Decimal
    order_meter_diff_kwh: Decimal
    basis_status: str
    note: str | None


class SplitOut(ORMModel):
    id: int
    order_id: int
    start_ts_local: datetime
    end_ts_local: datetime
    segment_name: str
    price_per_kwh: Decimal
    energy_kwh: Decimal | None
    amount: Decimal | None
    basis_status: str
    note: str | None


class BillOut(ORMModel):
    id: int
    bill_no: str
    site_id: int
    job_id: int
    master_total_kwh: Decimal
    charging_kwh: Decimal
    estimated_kwh: Decimal
    pending_kwh: Decimal
    pending_order_count: int
    public_kwh: Decimal
    unexplained_kwh: Decimal
    suspended_kwh: Decimal
    charging_amount: Decimal
    public_amount: Decimal
    status: str
    confirmed_at: datetime | None
    confirmed_by: str | None


class AdjustmentIn(BaseModel):
    component: str
    corrected_value: Decimal
    reason: str
    adoption_id: int | None = None  # 减少应付费用时必须引用财务已采纳的现场说明


class AdjustmentOut(ORMModel):
    id: int
    bill_id: int
    source_job_id: int
    component: str
    original_value: Decimal
    corrected_value: Decimal
    delta: Decimal
    reason: str
    case_id: int | None = None
    residual_id: int | None = None
    adoption_id: int | None = None
    created_by: str
    created_at: datetime


class TaskOut(ORMModel):
    id: int
    site_id: int
    job_id: int | None
    task_kind: str
    title: str
    detail: str
    assignee: str
    status: str
    response: str | None
    responded_by: str | None
    responded_at: datetime | None


class TaskRespondIn(BaseModel):
    response: str


class ConflictOut(ORMModel):
    id: int
    site_id: int
    device_id: str
    order_a_id: int
    order_b_id: int
    status: str
    resolved_order_id: int | None
    resolution_note: str | None


class ConflictResolveIn(BaseModel):
    resolved_order_id: int
    resolution_note: str


class RevisionOut(ORMModel):
    id: int
    source_record_id: int
    revision_no: int
    status: str
    content_hash: str
    created_at: datetime


class SourceRecordOut(ORMModel):
    id: int
    source_type: str
    source_record_no: str
    current_revision_id: int | None
    revisions: list[RevisionOut] = []


# ---------------- 站内差额定位与现场复核 ----------------

class TopologyEdgeOut(BaseModel):
    id: int
    site_id: int
    parent_meter_no: str
    child_meter_no: str
    parent_role: str
    child_role: str
    device_id: str | None
    effective_ts_local: datetime
    end_ts_local: datetime | None
    evidence_note: str
    status: str


class TopologyEdgeIn(BaseModel):
    parent_meter_no: str
    child_meter_no: str
    parent_role: str = "branch"
    child_role: str = "device"
    device_id: str | None = None
    effective_time: datetime          # 站点本地时间
    end_time: datetime | None = None
    evidence_note: str = ""


class TopologyIssueOut(ORMModel):
    id: int
    site_id: int
    issue_kind: str
    subject: str
    detail: str
    detected_at_local: datetime
    status: str


class TopologyCheckOut(BaseModel):
    site_id: int
    cycle_start: datetime
    cycle_end: datetime
    issues: list[TopologyIssueOut]
    edges: list[TopologyEdgeOut]


class ResidualOut(ORMModel):
    id: int
    job_id: int
    site_id: int
    parent_meter_no: str
    parent_role: str
    subject: str
    start_ts_local: datetime
    end_ts_local: datetime
    parent_kwh: Decimal | None
    known_children_kwh: Decimal
    residual_kwh: Decimal | None
    residual_pct: Decimal | None
    order_gap_kwh: Decimal
    uncovered_kwh: Decimal
    residual_kind: str
    has_order_gap: bool
    has_reading_gap: bool
    suspect: bool
    continues_previous: bool
    case_id: int | None
    note: str | None


class OrderGapOut(ORMModel):
    id: int
    job_id: int
    site_id: int
    device_id: str | None
    start_ts_local: datetime
    end_ts_local: datetime
    gap_kind: str
    missing_kwh: Decimal | None
    covered_seconds: int
    total_seconds: int
    note: str | None


class ResidualSummaryOut(ORMModel):
    id: int
    job_id: int
    site_id: int
    status: str
    master_total_kwh: Decimal
    known_children_kwh: Decimal
    branch_residual_kwh: Decimal
    uncovered_kwh: Decimal
    top_residual_kwh: Decimal
    order_gap_kwh: Decimal
    gap_windows: str | None
    concentrated_branches: str | None
    conclusion: str
    blocked_reason: str | None
    copied_from_job_id: int | None


class CaseEvidenceOut(ORMModel):
    id: int
    case_id: int
    site_id: int
    evidence_kind: str
    claim_kwh: Decimal | None
    description: str
    attachment_ref: str | None
    submitted_by: str
    submitted_at: datetime
    would_reduce_payable: bool


class AdoptionOut(ORMModel):
    id: int
    case_id: int
    evidence_id: int
    site_id: int
    bill_id: int | None
    decision: str
    adopted_kwh: Decimal | None
    allowed_to_reduce_payable: bool
    reason: str
    decided_by: str | None
    decided_at: datetime | None


class FieldCaseOut(ORMModel):
    id: int
    site_id: int
    job_id: int
    residual_id: int | None
    meter_no: str | None
    device_id: str | None
    branch_path: str
    start_ts_local: datetime
    end_ts_local: datetime
    residual_kwh: Decimal | None
    reason: str
    status: str
    assignee: str
    handling_note: str | None
    handled_by: str | None
    handled_at: datetime | None
    superseded_by_job_id: int | None
    superseded_reason: str | None


class EvidenceIn(BaseModel):
    evidence_kind: str
    claim_kwh: Decimal | None = None
    description: str
    attachment_ref: str | None = None


class AdoptionIn(BaseModel):
    decision: str
    adopted_kwh: Decimal | None = None
    reason: str
    bill_id: int | None = None


