"""连接关系登记 / 校验 / 残差查询 / 现场复核 / 财务采纳 API。"""
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..database import get_db
from ..deps import ensure_site_access, require_roles
from .. import models, schemas
from ..services.importer import _get_or_create_meter, _get_or_create_source_record
from ..services.timeutil import local_to_utc, utc_to_local
from ..services.topology import edge_to_view, validate_topology
from ..services.review import case_detail, decide_adoption, submit_evidence

router = APIRouter(prefix="/api", tags=["差额定位与现场复核"])

PARENT_ROLES = {"master", "branch"}
CHILD_ROLES = {"branch", "device"}


def _site_or_404(db: Session, site_id: int):
    site = db.get(models.Site, site_id)
    if site is None:
        raise HTTPException(404, "站点不存在")
    return site


def _edge_out(edge: models.TopologyEdge, meters: dict[int, models.Meter],
              site: models.Site | None = None) -> schemas.TopologyEdgeOut:
    parent = meters.get(edge.parent_meter_id)
    child = meters.get(edge.child_meter_id)
    tz = site.timezone if site else "UTC"
    return schemas.TopologyEdgeOut(
        id=edge.id, site_id=edge.site_id,
        parent_meter_no=parent.meter_no if parent else f"#{edge.parent_meter_id}",
        child_meter_no=child.meter_no if child else f"#{edge.child_meter_id}",
        parent_role=edge.parent_role, child_role=edge.child_role,
        device_id=edge.device_id,
        effective_ts_local=utc_to_local(edge.effective_ts, tz),
        end_ts_local=utc_to_local(edge.end_ts, tz) if edge.end_ts else None,
        evidence_note=edge.evidence_note or "",
        status=edge.status,
    )


@router.get("/topology/edges", response_model=list[schemas.TopologyEdgeOut])
def list_edges(site_id: int, active_only: bool = False,
               db: Session = Depends(get_db),
               user: models.User = Depends(require_roles(
                   models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    ensure_site_access(db, user, site_id)
    site = _site_or_404(db, site_id)
    stmt = select(models.TopologyEdge).where(models.TopologyEdge.site_id == site_id)
    if active_only:
        stmt = stmt.where(models.TopologyEdge.status == models.TOPO_ACTIVE)
    edges = db.scalars(stmt.order_by(models.TopologyEdge.effective_ts)).all()
    meters = {m.id: m for m in db.scalars(
        select(models.Meter).where(models.Meter.site_id == site_id)
    ).all()}
    return [_edge_out(e, meters, site) for e in edges]


@router.post("/topology/edges", response_model=schemas.TopologyEdgeOut)
def create_edge(site_id: int, body: schemas.TopologyEdgeIn,
                db: Session = Depends(get_db),
                user: models.User = Depends(require_roles(
                    models.ROLE_FINANCE, models.ROLE_OPERATOR))):
    ensure_site_access(db, user, site_id)
    site = _site_or_404(db, site_id)
    if body.parent_role not in PARENT_ROLES:
        raise HTTPException(400, f"上级角色只能是 {sorted(PARENT_ROLES)}")
    if body.child_role not in CHILD_ROLES:
        raise HTTPException(400, f"下级角色只能是 {sorted(CHILD_ROLES)}")
    if body.child_role == "branch" and body.parent_role != "master":
        raise HTTPException(400, "支路只能挂在总表下；设备挂在支路或总表下")
    if body.parent_role == "master" and body.child_role == "device" and not body.device_id:
        raise HTTPException(400, "总表直挂设备必须登记 device_id")

    parent = _get_or_create_meter(db, site_id, body.parent_meter_no, body.parent_role, None)
    parent.meter_kind = body.parent_role
    child = _get_or_create_meter(
        db, site_id, body.child_meter_no, body.child_role,
        None if body.child_role == "branch" else body.device_id,
    )
    child.meter_kind = body.child_role
    if body.child_role == "branch":
        child.device_id = None
    elif body.device_id:
        # 先前按读数建表可能未绑定设备号，登记连接时补齐，便于与订单比对
        child.device_id = body.device_id

    eff = local_to_utc(body.effective_time, site.timezone)
    end = local_to_utc(body.end_time, site.timezone) if body.end_time else None
    if end is not None and end <= eff:
        raise HTTPException(400, "终止时间必须晚于生效时间")

    previous = db.scalars(
        select(models.TopologyEdge).where(
            models.TopologyEdge.site_id == site_id,
            models.TopologyEdge.child_meter_id == child.id,
            models.TopologyEdge.status == models.TOPO_ACTIVE,
            models.TopologyEdge.effective_ts < eff,
        )
    ).all()
    for old in previous:
        if old.end_ts is None or old.end_ts > eff:
            old.end_ts = eff
            old.end_ts_local = body.effective_time

    sr = _get_or_create_source_record(
        db, site_id, "topology",
        f"API-{body.parent_meter_no}-{body.child_meter_no}-{body.effective_time.isoformat()}",
    )
    edge = models.TopologyEdge(
        site_id=site_id,
        parent_meter_id=parent.id, child_meter_id=child.id,
        parent_role=body.parent_role, child_role=body.child_role,
        device_id=body.device_id if body.child_role == "device" else None,
        effective_ts=eff, effective_ts_local=body.effective_time,
        end_ts=end, end_ts_local=body.end_time,
        evidence_note=body.evidence_note,
        status=models.TOPO_ACTIVE, source_record_id=sr.id,
    )
    db.add(edge)
    db.commit()
    db.refresh(edge)
    return _edge_out(edge, {parent.id: parent, child.id: child}, site)


@router.delete("/topology/edges/{edge_id}")
def deactivate_edge(edge_id: int, db: Session = Depends(get_db),
                    user: models.User = Depends(require_roles(
                        models.ROLE_FINANCE, models.ROLE_OPERATOR))):
    edge = db.get(models.TopologyEdge, edge_id)
    if edge is None:
        raise HTTPException(404, "连接关系不存在")
    ensure_site_access(db, user, edge.site_id)
    edge.status = models.TOPO_SUPERSEDED
    db.commit()
    return {"id": edge.id, "status": edge.status,
            "note": "已停用；已冻结作业仍按原快照解释历史差额，后续作业使用新版本"}


@router.get("/topology/check", response_model=schemas.TopologyCheckOut)
def check_topology(site_id: int, cycle_start: datetime, cycle_end: datetime,
                   db: Session = Depends(get_db),
                   user: models.User = Depends(require_roles(
                       models.ROLE_FINANCE, models.ROLE_OPERATOR))):
    """独立预检：环 / 重复挂接 / 父子覆盖不一致；不写入核算作业。"""
    ensure_site_access(db, user, site_id)
    site = _site_or_404(db, site_id)
    if cycle_end <= cycle_start:
        raise HTTPException(400, "周期结束必须晚于开始")
    start_utc = local_to_utc(cycle_start, site.timezone)
    end_utc = local_to_utc(cycle_end, site.timezone)

    meters = db.scalars(select(models.Meter).where(models.Meter.site_id == site_id)).all()
    meters_by_no = {m.meter_no: m for m in meters}
    meters_by_id = {m.id: m for m in meters}
    readings = db.scalars(
        select(models.MeterReading).where(
            models.MeterReading.site_id == site_id,
            models.MeterReading.status == models.ENTITY_ACTIVE,
        )
    ).all()
    readings_by_meter_id: dict[int, list[models.MeterReading]] = {m.id: [] for m in meters}
    for r in readings:
        readings_by_meter_id.setdefault(r.meter_id, []).append(r)

    active = db.scalars(
        select(models.TopologyEdge).where(
            models.TopologyEdge.site_id == site_id,
            models.TopologyEdge.status == models.TOPO_ACTIVE,
        ).order_by(models.TopologyEdge.effective_ts)
    ).all()
    views = [
        edge_to_view(e, meters_by_id[e.parent_meter_id].meter_no,
                     meters_by_id[e.child_meter_id].meter_no)
        for e in active
        if e.parent_meter_id in meters_by_id and e.child_meter_id in meters_by_id
    ]

    # 预检使用临时作业对象，校验问题只回显、不落库
    probe = models.ReconciliationJob(
        site_id=site_id, cycle_start=start_utc, cycle_end=end_utc,
        cycle_start_local=cycle_start, cycle_end_local=cycle_end,
        threshold_pct=site.diff_threshold_pct,
    )
    issues = validate_topology(
        db, site, probe, views, meters_by_no, readings_by_meter_id,
        start_utc, end_utc, [],
    )
    payload = [schemas.TopologyIssueOut(
        id=i.id or 0, site_id=i.site_id, issue_kind=i.issue_kind,
        subject=i.subject, detail=i.detail,
        detected_at_local=i.detected_at_local, status=i.status,
    ) for i in issues]
    for issue in issues:
        db.expunge(issue)
    db.rollback()
    return schemas.TopologyCheckOut(
        site_id=site_id, cycle_start=cycle_start, cycle_end=cycle_end,
        issues=payload,
        edges=[_edge_out(e, meters_by_no, site) for e in active],
    )


def _job_or_404(db: Session, job_id: int, user) -> models.ReconciliationJob:
    job = db.get(models.ReconciliationJob, job_id)
    if job is None:
        raise HTTPException(404, "作业不存在")
    ensure_site_access(db, user, job.site_id)
    return job


@router.get("/jobs/{job_id}/residuals", response_model=list[schemas.ResidualOut])
def job_residuals(job_id: int, db: Session = Depends(get_db),
                  user: models.User = Depends(require_roles(
                      models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    _job_or_404(db, job_id, user)
    return db.scalars(
        select(models.BranchResidual)
        .where(models.BranchResidual.job_id == job_id)
        .order_by(models.BranchResidual.start_ts, models.BranchResidual.id)
    ).all()


@router.get("/jobs/{job_id}/order-gaps", response_model=list[schemas.OrderGapOut])
def job_order_gaps(job_id: int, db: Session = Depends(get_db),
                   user: models.User = Depends(require_roles(
                       models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    _job_or_404(db, job_id, user)
    return db.scalars(
        select(models.OrderGap)
        .where(models.OrderGap.job_id == job_id)
        .order_by(models.OrderGap.start_ts)
    ).all()


@router.get("/jobs/{job_id}/residual-summary", response_model=schemas.ResidualSummaryOut)
def job_summary(job_id: int, db: Session = Depends(get_db),
                user: models.User = Depends(require_roles(
                    models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    _job_or_404(db, job_id, user)
    summary = db.scalar(
        select(models.JobResidualSummary).where(
            models.JobResidualSummary.job_id == job_id
        )
    )
    if summary is None:
        raise HTTPException(404, "该作业没有差额定位结果")
    return summary


@router.get("/field-cases", response_model=list[schemas.FieldCaseOut])
def list_cases(site_id: int | None = None,
               status_filter: str | None = Query(default=None, alias="status"),
               db: Session = Depends(get_db),
               user: models.User = Depends(require_roles(
                   models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    stmt = select(models.FieldReviewCase).order_by(models.FieldReviewCase.id.desc())
    if site_id is not None:
        ensure_site_access(db, user, site_id)
        stmt = stmt.where(models.FieldReviewCase.site_id == site_id)
    if status_filter:
        stmt = stmt.where(models.FieldReviewCase.status == status_filter)
    cases = db.scalars(stmt).all()
    if user.role == models.ROLE_PROPERTY:
        allowed = set(db.scalars(
            select(models.UserSite.site_id).where(models.UserSite.user_id == user.id)
        ).all())
        cases = [c for c in cases if c.site_id in allowed]
    return cases


@router.get("/field-cases/{case_id}")
def get_case(case_id: int, db: Session = Depends(get_db),
             user: models.User = Depends(require_roles(
                 models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    case = db.get(models.FieldReviewCase, case_id)
    if case is None:
        raise HTTPException(404, "现场复核工单不存在")
    ensure_site_access(db, user, case.site_id)
    detail = case_detail(db, case)
    return {
        "case": schemas.FieldCaseOut.model_validate(detail["case"]).model_dump(mode="json"),
        "residual": (
            schemas.ResidualOut.model_validate(detail["residual"]).model_dump(mode="json")
            if detail["residual"] else None
        ),
        "evidence": [
            {
                "evidence": schemas.CaseEvidenceOut.model_validate(
                    item["evidence"]).model_dump(mode="json"),
                "adoption": (
                    schemas.AdoptionOut.model_validate(item["adoption"]).model_dump(mode="json")
                    if item["adoption"] else None
                ),
            }
            for item in detail["evidence"]
        ],
        "adopted_reduction_kwh": str(detail["adopted_reduction_kwh"]),
    }


@router.post("/field-cases/{case_id}/evidence", response_model=schemas.CaseEvidenceOut)
def add_evidence(case_id: int, body: schemas.EvidenceIn,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(
                     models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    case = db.get(models.FieldReviewCase, case_id)
    if case is None:
        raise HTTPException(404, "现场复核工单不存在")
    ensure_site_access(db, user, case.site_id)
    if user.role == models.ROLE_PROPERTY and case.assignee != models.ROLE_PROPERTY:
        raise HTTPException(403, "该工单未分配给你处理")
    evidence = submit_evidence(
        db, case,
        evidence_kind=body.evidence_kind, description=body.description,
        claim_kwh=body.claim_kwh, attachment_ref=body.attachment_ref,
        submitted_by=user.username,
    )
    db.commit()
    db.refresh(evidence)
    return evidence


@router.post("/field-cases/evidence/{evidence_id}/adoption",
             response_model=schemas.AdoptionOut)
def set_adoption(evidence_id: int, body: schemas.AdoptionIn,
                 db: Session = Depends(get_db),
                 user: models.User = Depends(require_roles(models.ROLE_FINANCE))):
    evidence = db.get(models.CaseEvidence, evidence_id)
    if evidence is None:
        raise HTTPException(404, "现场说明不存在")
    ensure_site_access(db, user, evidence.site_id)
    bill = None
    if body.bill_id is not None:
        bill = db.get(models.Bill, body.bill_id)
        if bill is None or bill.site_id != evidence.site_id:
            raise HTTPException(400, "账单不存在或与说明不在同一站点")
    adoption = decide_adoption(
        db, evidence,
        decision=body.decision, adopted_kwh=body.adopted_kwh,
        reason=body.reason, user=user, bill=bill,
    )
    db.commit()
    db.refresh(adoption)
    return adoption


@router.get("/field-cases/evidence/{evidence_id}/adoptions",
            response_model=list[schemas.AdoptionOut])
def list_adoptions(evidence_id: int, db: Session = Depends(get_db),
                   user: models.User = Depends(require_roles(
                       models.ROLE_FINANCE, models.ROLE_OPERATOR, models.ROLE_PROPERTY))):
    evidence = db.get(models.CaseEvidence, evidence_id)
    if evidence is None:
        raise HTTPException(404, "现场说明不存在")
    ensure_site_access(db, user, evidence.site_id)
    return db.scalars(
        select(models.FinanceAdoption).where(
            models.FinanceAdoption.evidence_id == evidence_id
        ).order_by(models.FinanceAdoption.id)
    ).all()
