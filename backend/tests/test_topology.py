"""站内计量差额定位：逐层残差、缺报定位、拓扑校验、改线分段、
现场复核与财务采纳闭环、冻结快照。"""
from datetime import datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.database import SessionLocal
from app import models
from app.services.reconciliation import run_reconciliation
from app.services.topology import load_job_edges, resolved_edges_at

CYCLE = (datetime(2026, 10, 1), datetime(2026, 11, 1))


def _site(db, code="S03"):
    return db.scalar(select(models.Site).where(models.Site.code == code))


def _job(db, site, rerun_of=None):
    job = run_reconciliation(db, site, *CYCLE, "pytest", rerun_of_job_id=rerun_of)
    db.commit()
    return job


def _summary(db, job_id):
    return db.scalar(select(models.JobResidualSummary).where(
        models.JobResidualSummary.job_id == job_id))


def _residuals(db, job_id):
    return db.scalars(select(models.BranchResidual).where(
        models.BranchResidual.job_id == job_id
    ).order_by(models.BranchResidual.start_ts, models.BranchResidual.id)).all()


def test_branch_concentration_vs_order_gap_are_separated():
    """S03：BR-3 持续偏离 -> 现场复核；DEV-H3 单时窗缺订单 -> 先追数据，不定性故障。"""
    db = SessionLocal()
    try:
        site = _site(db)
        job = _job(db, site)
        summary = _summary(db, job.id)
        assert summary.status == models.RESIDUAL_JOB_COMPLETED
        # 差额集中在 BR-3，且没有被摊到 BR-1/BR-2
        assert "BR-3" in (summary.concentrated_branches or "")
        assert "BR-1" not in (summary.concentrated_branches or "")
        branch_rows = [r for r in _residuals(db, job.id) if r.parent_role == "branch"]
        br3 = [r for r in branch_rows if r.parent_meter_no == "BR-3"]
        assert len(br3) == 4
        assert all(r.suspect for r in br3), "读数齐全仍持续同向偏离才标记可疑"
        assert all(r.case_id is not None for r in br3)
        assert sum(r.residual_kwh for r in br3) == Decimal("385.0000")
        for no in ("BR-1", "BR-2"):
            for r in (x for x in branch_rows if x.parent_meter_no == no):
                assert not r.suspect and r.residual_kwh == 0
        # 缺报时段不计入持续偏离，也不会为它生成现场工单
        gaps = db.scalars(select(models.OrderGap).where(
            models.OrderGap.job_id == job.id)).all()
        assert len(gaps) == 1
        # 缺报只标记在 DEV-H3 所在时窗，BR-3 其他时窗仍参与持续偏离判定
        gap_rows = [r for r in branch_rows if r.has_order_gap]
        assert all(r.parent_meter_no == "BR-2" for r in gap_rows)
        assert gaps[0].device_id == "DEV-H3"
        assert gaps[0].gap_kind == models.GAP_ORDER
        assert gaps[0].missing_kwh == Decimal("500.0000")
        assert "DEV-H3" in (summary.gap_windows or "")
        # 结论里明确“不定性设备故障”
        assert "不认定设备故障" in summary.conclusion
        assert "先追数据" in summary.conclusion
    finally:
        db.close()


def test_uncovered_load_is_listed_not_allocated():
    """未挂接设备与已确认公共负荷单列，不摊给支路，也不为缩小差额改拓扑。"""
    db = SessionLocal()
    try:
        site = _site(db)
        job = _job(db, site)
        uncovered = [r for r in _residuals(db, job.id)
                     if r.residual_kind == models.RESIDUAL_UNCOVERED]
        assert uncovered
        assert all(r.residual_kwh > 0 for r in uncovered)
        assert any("M-H5" in r.subject for r in uncovered)
        assert any("公共负荷" in r.subject for r in uncovered)
        # 未确认公共负荷不计入任何残差（PL-HZ-002 未确认）
        assert all("PL-HZ-002" not in r.subject for r in uncovered)
        # 总表层扣除后无站级盲区：未覆盖部分如实单列，而不是塞进支路
        summary = _summary(db, job.id)
        assert summary.top_residual_kwh == 0
        assert summary.uncovered_kwh == Decimal("672.0000")
    finally:
        db.close()


def test_reconnection_splits_by_effective_time():
    """DEV-H2 于 10-15 由 BR-1 改挂 BR-2：改线前后按各自接线解释，历史差额不变。"""
    db = SessionLocal()
    try:
        site = _site(db)
        job = _job(db, site)
        edges = load_job_edges(db, job)
        before = resolved_edges_at(edges, datetime(2026, 10, 8))
        after = resolved_edges_at(edges, datetime(2026, 10, 20))
        assert before["M-H2"].parent_meter_no == "BR-1"
        assert after["M-H2"].parent_meter_no == "BR-2"
        br1 = [r for r in _residuals(db, job.id) if r.parent_meter_no == "BR-1"]
        # 改线时窗带来 4 个分段窗口（3 个周读数区间 + 10-15 切分），BR-1 在两个改线前窗口均为 0
        assert [float(r.residual_kwh) for r in br1[:2]] == [0.0, 0.0]
    finally:
        db.close()


def test_rerun_keeps_topology_snapshot_despite_relation_change():
    """关系变更与核算同时发生：旧作业保留原输入，后续作业改用新版本。"""
    from app.services.timeutil import parse_local_datetime

    db = SessionLocal()
    try:
        site = _site(db)
        job1 = _job(db, site)
        snap1 = {(s.edge_id, s.parent_meter_no, s.child_meter_no, s.effective_ts)
                 for s in db.scalars(select(models.JobTopologySnapshot).where(
                     models.JobTopologySnapshot.job_id == job1.id))}

        # 作业后新增一条改线关系（DEV-H3 从 BR-2 改挂 BR-1，10-20 生效）
        meters = {m.meter_no: m for m in db.scalars(
            select(models.Meter).where(models.Meter.site_id == site.id)).all()}
        eff = parse_local_datetime("2026-10-20 00:00", site.timezone)
        db.add(models.TopologyEdge(
            site_id=site.id,
            parent_meter_id=meters["BR-1"].id, child_meter_id=meters["M-H3"].id,
            parent_role="branch", child_role="device", device_id="DEV-H3",
            effective_ts=eff, effective_ts_local=datetime(2026, 10, 20),
            status=models.TOPO_ACTIVE, evidence_note="作业后改线",
        ))
        db.flush()
        # 自动收口旧关系（模拟导入/接口行为）
        for old in db.scalars(select(models.TopologyEdge).where(
                models.TopologyEdge.site_id == site.id,
                models.TopologyEdge.child_meter_id == meters["M-H3"].id,
                models.TopologyEdge.status == models.TOPO_ACTIVE,
                models.TopologyEdge.effective_ts < eff)).all():
            if old.end_ts is None or old.end_ts > eff:
                old.end_ts = eff
        db.commit()

        # 重跑沿用原快照，不受作业后改线影响
        job2 = _job(db, site, rerun_of=job1.id)
        snap2 = {(s.edge_id, s.parent_meter_no, s.child_meter_no, s.effective_ts)
                 for s in db.scalars(select(models.JobTopologySnapshot).where(
                     models.JobTopologySnapshot.job_id == job2.id))}
        assert snap1 == snap2

        # 新一轮（非重跑）改用新版本：DEV-H3 在 10-20 之后挂到 BR-1
        job3 = _job(db, site)
        edges3 = load_job_edges(db, job3)
        assert resolved_edges_at(edges3, datetime(2026, 10, 20))["M-H3"].parent_meter_no == "BR-1"
    finally:
        db.close()


def _add_edge(db, site, parent_no, child_no, child_role, device_id, eff_local, end_local=None):
    from app.services.timeutil import parse_local_datetime
    meters = {m.meter_no: m for m in db.scalars(
        select(models.Meter).where(models.Meter.site_id == site.id)).all()}
    db.add(models.TopologyEdge(
        site_id=site.id,
        parent_meter_id=meters[parent_no].id, child_meter_id=meters[child_no].id,
        parent_role="branch" if meters[parent_no].meter_kind != "master" else "master",
        child_role=child_role, device_id=device_id,
        effective_ts=parse_local_datetime(eff_local, site.timezone),
        effective_ts_local=datetime.fromisoformat(eff_local),
        end_ts=parse_local_datetime(end_local, site.timezone) if end_local else None,
        status=models.TOPO_ACTIVE,
    ))
    db.commit()


def test_cycle_stops_residual_and_names_objects():
    db = SessionLocal()
    try:
        site = _site(db)
        # 制造环：BR-1 -> BR-3 与 BR-3 -> BR-1 互相指为下级
        _add_edge(db, site, "BR-1", "BR-3", "branch", None, "2026-09-01 00:00")
        _add_edge(db, site, "BR-3", "BR-1", "branch", None, "2026-09-01 00:00")
        job = _job(db, site)
        summary = _summary(db, job.id)
        assert summary.status == models.RESIDUAL_JOB_BLOCKED
        assert "cycle" in (summary.blocked_reason or "")
        assert "BR-1" in (summary.blocked_reason or "")
        assert "BR-3" in (summary.blocked_reason or "")
        issues = db.scalars(select(models.TopologyIssue).where(
            models.TopologyIssue.site_id == site.id,
            models.TopologyIssue.issue_kind == models.TOPO_ISSUE_CYCLE)).all()
        assert issues
        task = db.scalar(select(models.ReviewTask).where(
            models.ReviewTask.job_id == job.id,
            models.ReviewTask.task_kind == "topology_issue"))
        assert task is not None
    finally:
        db.close()


def test_duplicate_mount_stops_residual():
    db = SessionLocal()
    try:
        site = _site(db)
        # DEV-H1 同时挂在 BR-1 与 BR-2（同一生效时刻）
        _add_edge(db, site, "BR-2", "M-H1", "device", "DEV-H1", "2026-10-01 00:00")
        job = _job(db, site)
        summary = _summary(db, job.id)
        assert summary.status == models.RESIDUAL_JOB_BLOCKED
        assert "duplicate_mount" in (summary.blocked_reason or "")
        assert "M-H1" in (summary.blocked_reason or "")
    finally:
        db.close()


def test_coverage_mismatch_stops_residual():
    db = SessionLocal()
    try:
        site = _site(db)
        # 把 M-H1 的 11-01 末读数停用，使父子覆盖时段不一致
        h1_id = db.scalar(select(models.Meter.id).where(
            models.Meter.site_id == site.id, models.Meter.meter_no == "M-H1"))
        last = db.scalars(select(models.MeterReading).where(
            models.MeterReading.site_id == site.id,
            models.MeterReading.meter_id == h1_id,
            models.MeterReading.reading_ts >= datetime(2026, 10, 31),
        )).all()
        assert last
        for r in last:
            r.status = models.ENTITY_SUPERSEDED
        db.commit()
        job = _job(db, site)
        summary = _summary(db, job.id)
        assert summary.status == models.RESIDUAL_JOB_BLOCKED
        assert "coverage_mismatch" in (summary.blocked_reason or "")
        assert "BR-1" in (summary.blocked_reason or "")
    finally:
        db.close()


def test_field_evidence_requires_finance_adoption_to_reduce_payable():
    """现场说明 -> 财务独立采纳 -> 账单更正；未采纳不得减少应付费用。"""
    db = SessionLocal()
    try:
        site = _site(db)
        job = _job(db, site)
        case = db.scalar(select(models.FieldReviewCase).where(
            models.FieldReviewCase.job_id == job.id))
        assert case is not None and case.status == models.CASE_OPEN
        case_id = case.id
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        assert bill.status == models.BILL_DRAFT
    finally:
        db.close()

    from app.services.review import submit_evidence, decide_adoption
    from app.services.billing import add_adjustment, confirm_bill
    from app.security import verify_password

    db = SessionLocal()
    try:
        site = _site(db)
        case = db.get(models.FieldReviewCase, case_id)
        op = db.scalar(select(models.User).where(models.User.username == "operator"))
        ev = submit_evidence(
            db, case, evidence_kind=models.EVIDENCE_REPAIR,
            description="现场检修发现 BR-3 出线端子氧化，已更换并复测；差额可能来自接触电阻发热损耗。",
            claim_kwh=Decimal("80"), attachment_ref="WO-HZ-20261026-11",
            submitted_by=op.username,
        )
        db.commit()
        assert case.status == models.CASE_EVIDENCE_SUBMITTED

        fin = db.scalar(select(models.User).where(models.User.username == "finance"))
        # 财务先不采纳：不得减少应付费用
        pending = decide_adoption(
            db, ev, decision=models.ADOPTION_PENDING, adopted_kwh=None,
            reason="检修记录未附热成像/复测原始数据，暂不作为结算依据", user=fin,
        )
        db.commit()
        assert pending.allowed_to_reduce_payable is False

        bill = db.scalar(select(models.Bill).where(models.Bill.site_id == site.id,
                                                  models.Bill.job_id == case.job_id))
        confirm_bill(db, bill, fin)
        db.commit()
        original = bill.unexplained_kwh
        # 未采纳说明不能减费：减少 delta 必须引用采纳，否则 409
        try:
            add_adjustment(db, bill, "unexplained_kwh", original - 80,
                           "按检修记录减少", fin, adoption_id=None)
            raise AssertionError("未采纳说明不应允许减少应付费用")
        except Exception as exc:
            assert getattr(exc, "status_code", None) == 409
        db.rollback()

        # 财务采纳 50 kWh 后，可引用该采纳记录登记减费更正
        adoption = decide_adoption(
            db, ev, decision=models.ADOPTION_ADOPTED, adopted_kwh=Decimal("50"),
            reason="复测数据确认 50 kWh 属计量错误，采纳为结算依据；其余差额仍待查",
            user=fin, bill=bill,
        )
        db.commit()
        assert adoption.allowed_to_reduce_payable is True
        adj = add_adjustment(db, bill, "unexplained_kwh", original - 50,
                             "依据已采纳的检修说明与复测数据", fin,
                             adoption_id=adoption.id)
        db.commit()
        assert adj.adoption_id == adoption.id
        assert adj.case_id == case.id
        assert adj.delta == Decimal("-50.0000")
        # 仍不允许超出采纳电量多减
        bill = db.get(models.Bill, bill.id)
        with pytest.raises(Exception) as excinfo:
            add_adjustment(db, bill, "unexplained_kwh", original - 80,
                           "多减", fin, adoption_id=adoption.id)
        assert getattr(excinfo.value, "status_code", None) == 400
        db.rollback()
    finally:
        db.close()


def test_backfill_supersedes_old_field_conclusion():
    """复核期间补到数据后重算：旧工单转 superseded，不能自动成为新账依据。"""
    from pathlib import Path
    from app.services.importer import import_file

    db = SessionLocal()
    try:
        site = _site(db)
        job1 = _job(db, site)
        case = db.scalar(select(models.FieldReviewCase).where(
            models.FieldReviewCase.job_id == job1.id))
        assert case.status == models.CASE_OPEN
        case_id = case.id

        # 补报 DEV-H3 第 2 周订单
        content = (Path(__file__).resolve().parent.parent
                   / "sample_data" / "S03" / "orders_v2_backfill.csv").read_bytes()
        import_file(db, site, "order", "orders_v2_backfill.csv", content,
                    "pytest", "data/test_originals")
        db.commit()

        job2 = _job(db, site)
        old = db.get(models.FieldReviewCase, case_id)
        assert old.status == models.CASE_SUPERSEDED
        assert old.superseded_by_job_id == job2.id
        assert "补报" in (old.superseded_reason or "")
        summary2 = _summary(db, job2.id)
        # 缺报窗口消失，BR-3 仍持续偏离 -> 新工单替代旧工单
        assert summary2.gap_windows is None
        new_case = db.scalar(select(models.FieldReviewCase).where(
            models.FieldReviewCase.job_id == job2.id))
        assert new_case is not None and new_case.id != case_id
    finally:
        db.close()
