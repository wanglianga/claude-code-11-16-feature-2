"""现场复核、证据与财务采纳闭环。

职责边界（严格区分）：
- 运维/现场人员：登记检修记录、未覆盖公共负荷、计量错误等**说明与证据**；
  可以建议，但不能决定这些说明能否用于结算。
- 财务：独立判断哪条说明可用于结算；只有带“财务采纳”的说明才可减少应付费用。
- 残差计算：只给出可疑范围与工单，不把差额直接定性为设备故障。
- 补报/新数据重算后，旧工单转入 superseded，其故障判断不得自动成为新账依据。
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from .timeutil import quantize

settings = get_settings()
ZERO = Decimal("0")

VALID_EVIDENCE_KINDS = {
    models.EVIDENCE_REPAIR,
    models.EVIDENCE_PUBLIC_LOAD,
    models.EVIDENCE_METER_ERROR,
}


def _case_or_404(db: Session, case_id: int) -> models.FieldReviewCase:
    case = db.get(models.FieldReviewCase, case_id)
    if case is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "现场复核工单不存在")
    return case


def submit_evidence(
    db: Session,
    case: models.FieldReviewCase,
    *,
    evidence_kind: str,
    description: str,
    claim_kwh,
    attachment_ref: str | None,
    submitted_by: str,
) -> models.CaseEvidence:
    if evidence_kind not in VALID_EVIDENCE_KINDS:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"未知证据类型 {evidence_kind}，仅支持 {sorted(VALID_EVIDENCE_KINDS)}",
        )
    if case.status == models.CASE_SUPERSEDED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "该现场工单已被补报数据重算取代，旧结论不得作为新账依据；请处理新工单",
        )
    if case.status == models.CASE_CLOSED:
        raise HTTPException(status.HTTP_409_CONFLICT, "工单已关闭，补充说明请新建工单")
    claim = quantize(claim_kwh, 4) if claim_kwh not in (None, "") else None
    if claim is not None and claim < 0:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "说明电量不能为负")
    if not description.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请填写现场说明")

    ev = models.CaseEvidence(
        case_id=case.id, site_id=case.site_id,
        evidence_kind=evidence_kind, claim_kwh=claim,
        description=description.strip(),
        attachment_ref=attachment_ref,
        submitted_by=submitted_by,
        would_reduce_payable=claim is not None and claim > 0,
    )
    db.add(ev)
    case.status = models.CASE_EVIDENCE_SUBMITTED
    case.handling_note = description.strip()
    case.handled_by = submitted_by
    case.handled_at = datetime.utcnow()
    db.flush()
    return ev


def decide_adoption(
    db: Session,
    evidence: models.CaseEvidence,
    *,
    decision: str,
    adopted_kwh,
    reason: str,
    user: models.User,
    bill: models.Bill | None = None,
) -> models.FinanceAdoption:
    """财务独立判断；未采纳说明不得减少应付费用。"""
    if decision not in (
        models.ADOPTION_ADOPTED, models.ADOPTION_PENDING, models.ADOPTION_REJECTED,
    ):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知采纳结论 {decision}")
    if not reason.strip():
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "请填写财务判断依据")

    existing = db.scalar(
        select(models.FinanceAdoption).where(
            models.FinanceAdoption.evidence_id == evidence.id
        )
    )
    if decision == models.ADOPTION_ADOPTED:
        if evidence.claim_kwh is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "该说明未申报电量，无法作为减少应付费用的依据",
            )
        value = quantize(adopted_kwh if adopted_kwh not in (None, "") else evidence.claim_kwh, 4)
        if value < 0 or value > evidence.claim_kwh:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "采纳电量必须介于 0 与现场说明申报电量之间",
            )
    else:
        value = ZERO

    allowed = decision == models.ADOPTION_ADOPTED
    if existing is None:
        existing = models.FinanceAdoption(
            case_id=evidence.case_id, evidence_id=evidence.id,
            site_id=evidence.site_id, bill_id=bill.id if bill else None,
            decision=decision, adopted_kwh=value,
            allowed_to_reduce_payable=allowed, reason=reason.strip(),
            decided_by=user.username, decided_at=datetime.utcnow(),
        )
        db.add(existing)
    else:
        existing.decision = decision
        existing.adopted_kwh = value
        existing.allowed_to_reduce_payable = allowed
        existing.reason = reason.strip()
        existing.decided_by = user.username
        existing.decided_at = datetime.utcnow()
        if bill is not None:
            existing.bill_id = bill.id
    db.flush()
    return existing


def payable_reduction_for_case(db: Session, case_id: int) -> Decimal:
    """可减少应付费用的电量：仅统计财务已采纳的说明。"""
    rows = db.scalars(
        select(models.FinanceAdoption).where(
            models.FinanceAdoption.case_id == case_id,
            models.FinanceAdoption.decision == models.ADOPTION_ADOPTED,
            models.FinanceAdoption.allowed_to_reduce_payable.is_(True),
        )
    ).all()
    return quantize(sum((r.adopted_kwh or ZERO for r in rows), ZERO), 4)


def case_detail(db: Session, case: models.FieldReviewCase) -> dict:
    evidence = db.scalars(
        select(models.CaseEvidence)
        .where(models.CaseEvidence.case_id == case.id)
        .order_by(models.CaseEvidence.id)
    ).all()
    adoptions = {
        a.evidence_id: a for a in db.scalars(
            select(models.FinanceAdoption).where(
                models.FinanceAdoption.case_id == case.id
            )
        ).all()
    }
    residual = db.get(models.BranchResidual, case.residual_id) if case.residual_id else None
    return {
        "case": case,
        "residual": residual,
        "evidence": [
            {
                "evidence": ev,
                "adoption": adoptions.get(ev.id),
            }
            for ev in evidence
        ],
        "adopted_reduction_kwh": payable_reduction_for_case(db, case.id),
    }
