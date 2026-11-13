"""账单确认与差额更正。"""
from datetime import datetime

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from .timeutil import money, quantize


def check_new_revisions(db: Session, job: models.ReconciliationJob) -> list[models.RecordRevision]:
    """确认前检查：作业冻结之后依据是否出现新修订。

    包括两类：
    1. 来源记录当前生效版本已不同于快照版本（下一轮提升后的修订）；
    2. 作业采用后到达、被标记 blocked_adopted 的更正（留待下一轮，同样阻止本轮确认）。
    """
    snapshot = db.scalars(
        select(models.JobInput).where(models.JobInput.job_id == job.id)
    ).all()
    new_rev_ids: set[int] = set()
    for snap in snapshot:
        if snap.revision_id is None:
            continue
        base_rev = db.get(models.RecordRevision, snap.revision_id)
        if base_rev is None:
            continue
        sr = db.get(models.SourceRecord, base_rev.source_record_id)
        if sr.current_revision_id and sr.current_revision_id != snap.revision_id:
            new_rev_ids.add(sr.current_revision_id)
        later = db.scalars(
            select(models.RecordRevision).where(
                models.RecordRevision.source_record_id == sr.id,
                models.RecordRevision.id > snap.revision_id,
            )
        ).all()
        for rev in later:
            new_rev_ids.add(rev.id)
    return [db.get(models.RecordRevision, rid) for rid in sorted(new_rev_ids)]


def confirm_bill(db: Session, bill: models.Bill, user: models.User) -> models.Bill:
    if bill.status == models.BILL_CONFIRMED:
        raise HTTPException(status.HTTP_409_CONFLICT, "账单已确认")
    job = db.get(models.ReconciliationJob, bill.job_id)
    new_revs = check_new_revisions(db, job)
    if new_revs:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"依据在作业冻结后出现 {len(new_revs)} 条新修订，请先发起下一轮核算再确认",
        )
    if bill.status == models.BILL_SUSPENDED:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "账单处于挂起状态（未解释差额超阈值或存在挂起计量段），不得确认",
        )
    bill.status = models.BILL_CONFIRMED
    bill.confirmed_at = datetime.utcnow()
    bill.confirmed_by = user.username
    db.flush()
    return bill


def add_adjustment(
    db: Session,
    bill: models.Bill,
    component: str,
    corrected_value,
    reason: str,
    user: models.User,
    adoption_id: int | None = None,
) -> models.BillAdjustment:
    """已确认账单只允许差额更正：保留原值，以更正行体现 delta，不直接替换原字段。

    减少应付费用的更正必须引用财务已采纳的现场说明；未采纳的说明不得直接减费。
    """
    if bill.status not in (models.BILL_CONFIRMED, models.BILL_ADJUSTED):
        raise HTTPException(status.HTTP_409_CONFLICT, "只有已确认账单可以登记差额更正")
    field_map = {
        "charging_kwh": bill.charging_kwh,
        "estimated_kwh": bill.estimated_kwh,
        "public_kwh": bill.public_kwh,
        "unexplained_kwh": bill.unexplained_kwh,
        "charging_amount": bill.charging_amount,
        "public_amount": bill.public_amount,
    }
    if component not in field_map:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"未知更正成分 {component}")
    original = field_map[component]
    corrected = quantize(corrected_value, 4)
    delta = quantize(corrected - original, 4)

    adoption = None
    if adoption_id is not None:
        adoption = db.get(models.FinanceAdoption, adoption_id)
        if adoption is None:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "引用的财务采纳记录不存在")
        if adoption.site_id != bill.site_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "采纳记录与账单不在同一站点")
        if adoption.bill_id is not None and adoption.bill_id != bill.id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "该采纳记录已绑定其他账单")
        if not adoption.allowed_to_reduce_payable:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "引用的现场说明未被财务采纳，不能用于减少应付费用",
            )
        adoption.bill_id = bill.id

    if delta < 0 and adoption is None:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "减少应付费用的更正必须引用财务已采纳的现场说明（adoption_id）",
        )
    if adoption is not None and delta < 0:
        adoptable = abs(adoption.adopted_kwh or 0)
        if abs(delta) > adoptable:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                f"更正减少量超出该说明被采纳的电量（{adoptable}），不能多减",
            )

    adj = models.BillAdjustment(
        bill_id=bill.id,
        source_job_id=bill.job_id,
        component=component,
        original_value=original,
        corrected_value=corrected,
        delta=delta,
        reason=reason,
        case_id=adoption.case_id if adoption else None,
        residual_id=None,
        adoption_id=adoption.id if adoption else None,
        created_by=user.username,
    )
    if adoption is not None and adoption.case_id:
        case = db.get(models.FieldReviewCase, adoption.case_id)
        if case is not None and case.residual_id:
            adj.residual_id = case.residual_id
    db.add(adj)
    bill.status = models.BILL_ADJUSTED
    db.flush()
    return adj
