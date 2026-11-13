"""API 端到端：登录、角色权限、物业站点隔离、账单确认前检查、差额更正、下钻。"""
from datetime import datetime
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.main import app
from app.database import SessionLocal
from app import models
from app.services.reconciliation import run_reconciliation

client = TestClient(app)


def _login(username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def _auth(token):
    return {"Authorization": f"Bearer {token}"}


def test_login_and_wrong_password():
    assert client.post("/api/auth/login",
                       json={"username": "finance", "password": "bad"}).status_code == 401
    token = _login("finance", "Finance@123")
    r = client.get("/api/sites", headers=_auth(token))
    assert r.status_code == 200 and len(r.json()) == 3


def _seed_jobs():
    """为两个站点各生成一轮核算，产生核对事项与账单。"""
    db = SessionLocal()
    try:
        for code in ("S01", "S02"):
            site = db.scalar(select(models.Site).where(models.Site.code == code))
            run_reconciliation(db, site, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
    finally:
        db.close()


def test_property_user_scoped_to_assigned_site():
    _seed_jobs()
    token = _login("property_sh", "Property@123")
    sites = client.get("/api/sites", headers=_auth(token)).json()
    assert {s["code"] for s in sites} == {"S01"}
    # 访问未授权站点被拒
    r = client.get("/api/sites/2", headers=_auth(token))
    assert r.status_code == 403
    # 物业看不到运营/财务的核对事项，只能看 assignee=property 且在授权站点内的条目
    tasks = client.get("/api/tasks", headers=_auth(token)).json()
    assert all(t["assignee"] == "property" for t in tasks)  # S01 无物业事项时为空列表
    token_xj = _login("property_xj", "Property@123")
    tasks_xj = client.get("/api/tasks", headers=_auth(token_xj)).json()
    assert tasks_xj and all(t["assignee"] == "property" for t in tasks_xj)
    kinds = {t["task_kind"] for t in tasks_xj}
    assert "public_confirmation" in kinds and "unexplained_diff" in kinds
    # 物业不能发起核算
    r = client.post("/api/jobs", headers=_auth(token),
                    json={"site_id": 1,
                          "cycle_start": "2026-10-01T00:00:00",
                          "cycle_end": "2026-11-01T00:00:00"})
    assert r.status_code == 403


def test_property_respond_to_own_task_and_cannot_touch_others():
    _seed_jobs()
    db = SessionLocal()
    try:
        task = db.scalar(select(models.ReviewTask).where(
            models.ReviewTask.assignee == models.ROLE_PROPERTY,
            models.ReviewTask.site_id == 2))
        operator_task = db.scalar(select(models.ReviewTask).where(
            models.ReviewTask.assignee == models.ROLE_OPERATOR))
    finally:
        db.close()
    token = _login("property_xj", "Property@123")
    r = client.post(f"/api/tasks/{task.id}/respond", headers=_auth(token),
                    json={"response": "物业确认公共照明 320kWh 属实"})
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    r = client.post(f"/api/tasks/{operator_task.id}/respond", headers=_auth(token),
                    json={"response": "越权尝试"})
    assert r.status_code == 403


def test_bill_confirm_precheck_block_and_adjustment_keeps_original():
    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        job = run_reconciliation(db, site, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        bill_id = bill.id
        original_unexplained = bill.unexplained_kwh
        original_public = bill.public_kwh
        site2 = db.scalar(select(models.Site).where(models.Site.code == "S02"))
        job2 = run_reconciliation(db, site2, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
        suspended_bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job2.id))
        suspended_id = suspended_bill.id
    finally:
        db.close()

    fin = _auth(_login("finance", "Finance@123"))
    # 挂起账单不允许确认
    r = client.post(f"/api/bills/{suspended_id}/confirm", headers=fin)
    assert r.status_code == 409
    # 正常账单：确认前检查无新修订
    pre = client.get(f"/api/bills/{bill_id}/precheck", headers=fin).json()
    assert pre["new_revision_count"] == 0
    r = client.post(f"/api/bills/{bill_id}/confirm", headers=fin)
    assert r.status_code == 200 and r.json()["status"] == "confirmed"
    # 重复确认被拒
    assert client.post(f"/api/bills/{bill_id}/confirm", headers=fin).status_code == 409

    # 已确认账单只能差额更正，原值保留；减少应付费用必须引用财务采纳（未引用则拒绝）
    r = client.post(f"/api/bills/{bill_id}/adjustments", headers=fin, json={
        "component": "unexplained_kwh",
        "corrected_value": "25.0000",
        "reason": "物业补充线损依据，财务复核后登记差额更正",
    })
    assert r.status_code == 409 and "采纳" in r.json()["detail"]
    r = client.post(f"/api/bills/{bill_id}/adjustments", headers=fin, json={
        "component": "public_kwh",
        "corrected_value": "330.0000",
        "reason": "补充公共负荷读数依据，财务复核后上调",
    })
    assert r.status_code == 200
    adj = r.json()
    assert adj["original_value"] == str(original_public)
    assert adj["delta"] == str((Decimal("330") - original_public).quantize(Decimal("0.0001")))
    assert adj["adoption_id"] is None
    db = SessionLocal()
    try:
        bill = db.get(models.Bill, bill_id)
        # 原字段不被替换
        assert bill.unexplained_kwh == original_unexplained
        assert bill.status == models.BILL_ADJUSTED
    finally:
        db.close()


def test_confirm_blocked_when_new_revision_arrives():
    from pathlib import Path
    from app.services.importer import import_file

    db = SessionLocal()
    try:
        site = db.scalar(select(models.Site).where(models.Site.code == "S01"))
        job = run_reconciliation(db, site, datetime(2026, 10, 1), datetime(2026, 11, 1), "tester")
        db.commit()
        bill = db.scalar(select(models.Bill).where(models.Bill.job_id == job.id))
        bill_id = bill.id
        content = (Path(__file__).resolve().parent.parent
                   / "sample_data" / "S01" / "orders_v3_after_adopt.csv").read_bytes()
        import_file(db, site, "order", "orders_v3_after_adopt.csv", content,
                    "tester", "data/test_originals")
        db.commit()
    finally:
        db.close()
    fin = _auth(_login("finance", "Finance@123"))
    pre = client.get(f"/api/bills/{bill_id}/precheck", headers=fin).json()
    assert pre["new_revision_count"] == 1
    r = client.post(f"/api/bills/{bill_id}/confirm", headers=fin)
    assert r.status_code == 409 and "新修订" in r.json()["detail"]


def test_drill_down_from_interval_to_splits_and_entities():
    _seed_jobs()
    fin = _auth(_login("finance", "Finance@123"))
    jobs = client.get("/api/jobs?site_id=1", headers=fin).json()
    job_id = jobs[0]["id"]
    intervals = client.get(f"/api/jobs/{job_id}/intervals", headers=fin).json()
    assert intervals and "master_reading_start" in intervals[0]
    splits = client.get(f"/api/jobs/{job_id}/splits", headers=fin).json()
    assert splits and all("segment_name" in s for s in splits)
    readings = client.get("/api/entities/readings?site_id=1", headers=fin).json()
    assert readings and "meter_no" in readings[0]
    orders = client.get("/api/entities/orders?site_id=1", headers=fin).json()
    assert len(orders) >= 9
    files = client.get("/api/imports/files?site_id=1", headers=fin).json()
    rows = client.get(f"/api/imports/files/{files[0]['id']}/rows", headers=fin).json()
    assert rows and "raw_line" in rows[0] and "line_no" in rows[0]


def test_operator_resolves_conflict():
    op = _auth(_login("operator", "Operator@123"))
    conflicts = client.get("/api/conflicts?site_id=2", headers=op).json()
    open_conflicts = [c for c in conflicts if c["status"] == "open"]
    assert open_conflicts
    c = open_conflicts[0]
    r = client.post(f"/api/conflicts/{c['id']}/resolve", headers=op, json={
        "resolved_order_id": c["order_a_id"],
        "resolution_note": "核对设备日志确认 A 订单真实",
    })
    assert r.status_code == 200 and r.json()["status"] == "resolved"
    # 物业无权处理冲突
    prop = _auth(_login("property_xj", "Property@123"))
    r2 = client.post(f"/api/conflicts/{c['id']}/resolve", headers=prop, json={
        "resolved_order_id": c["order_a_id"], "resolution_note": "x"})
    assert r2.status_code == 403


def test_topology_and_field_case_flow():
    """拓扑登记/预检/残差下钻/现场说明/财务采纳 API 全链路与权限。"""
    import csv
    import io

    fin = _auth(_login("finance", "Finance@123"))
    op = _auth(_login("operator", "Operator@123"))
    prop = _auth(_login("property_hz", "Property@123"))
    site3 = client.get("/api/sites", headers=fin).json()
    site3_id = next(s["id"] for s in site3 if s["code"] == "S03")

    edges = client.get(f"/api/topology/edges?site_id={site3_id}", headers=fin).json()
    assert len(edges) == 8
    assert any(e["child_meter_no"] == "BR-1" and e["parent_meter_no"] == "M-HZ-01"
               for e in edges)

    check = client.get(
        "/api/topology/check", headers=op,
        params={"site_id": site3_id, "cycle_start": "2026-10-01T00:00:00",
                "cycle_end": "2026-11-01T00:00:00"},
    ).json()
    assert check["issues"] == []

    # 发起 S03 核算后下钻残差与缺报
    job = client.post("/api/jobs", headers=fin, json={
        "site_id": site3_id,
        "cycle_start": "2026-10-01T00:00:00",
        "cycle_end": "2026-11-01T00:00:00",
    }).json()
    summary = client.get(f"/api/jobs/{job['id']}/residual-summary", headers=fin).json()
    assert summary["status"] == "completed"
    assert "BR-3" in summary["concentrated_branches"]
    residuals = client.get(f"/api/jobs/{job['id']}/residuals", headers=fin).json()
    assert any(r["suspect"] for r in residuals)
    gaps = client.get(f"/api/jobs/{job['id']}/order-gaps", headers=fin).json()
    assert [g["device_id"] for g in gaps] == ["DEV-H3"]

    cases = client.get(f"/api/field-cases?site_id={site3_id}", headers=op).json()
    assert len(cases) == 1 and cases[0]["meter_no"] == "BR-3"
    case_id = cases[0]["id"]

    # 现场人员提交检修说明
    ev = client.post(f"/api/field-cases/{case_id}/evidence", headers=op, json={
        "evidence_kind": "repair",
        "claim_kwh": "60",
        "description": "现场检修 BR-3 出线端子并复测，60 kWh 可能为接触不良损耗。",
        "attachment_ref": "WO-HZ-20261026-11",
    })
    assert ev.status_code == 200, ev.text
    evidence_id = ev.json()["id"]

    # 财务未采纳前：采纳接口只有财务可调用，物业/运营无权
    r = client.post(f"/api/field-cases/evidence/{evidence_id}/adoption", headers=op, json={
        "decision": "adopted", "adopted_kwh": "60", "reason": "同意"})
    assert r.status_code == 403

    pending = client.post(f"/api/field-cases/evidence/{evidence_id}/adoption", headers=fin, json={
        "decision": "pending", "reason": "缺少复测原始记录，暂不采纳"})
    assert pending.status_code == 200
    assert pending.json()["allowed_to_reduce_payable"] is False

    bill = client.get(f"/api/bills?site_id={site3_id}", headers=fin).json()[0]
    assert bill["status"] == "draft"
    confirmed = client.post(f"/api/bills/{bill['id']}/confirm", headers=fin)
    assert confirmed.status_code == 200

    # 未采纳说明：减费更正被拒
    r = client.post(f"/api/bills/{bill['id']}/adjustments", headers=fin, json={
        "component": "unexplained_kwh",
        "corrected_value": str(float(bill["unexplained_kwh"]) - 60),
        "reason": "按检修说明减费",
    })
    assert r.status_code == 409 and "采纳" in r.json()["detail"]

    # 财务采纳 40 kWh 后，引用采纳记录可减费；但不得超过采纳电量
    adoption = client.post(f"/api/field-cases/evidence/{evidence_id}/adoption", headers=fin, json={
        "decision": "adopted", "adopted_kwh": "40", "bill_id": bill["id"],
        "reason": "复测支持 40 kWh 为计量错误，予以采纳"})
    assert adoption.status_code == 200
    adoption_id = adoption.json()["id"]
    view = client.get(f"/api/field-cases/{case_id}", headers=fin).json()
    assert any(item["adoption"] and item["adoption"]["id"] == adoption_id
               for item in view["evidence"])
    assert view["adopted_reduction_kwh"] == "40.0000"

    r = client.post(f"/api/bills/{bill['id']}/adjustments", headers=fin, json={
        "component": "unexplained_kwh",
        "corrected_value": str(float(bill["unexplained_kwh"]) - 40),
        "reason": "引用已采纳说明",
        "adoption_id": adoption_id,
    })
    assert r.status_code == 200, r.text
    assert r.json()["adoption_id"] == adoption_id
    assert r.json()["case_id"] == case_id

    # 新链接关系：上级角色非法被拒；物业不能登记连接关系
    r = client.post(f"/api/topology/edges?site_id={site3_id}", headers=prop, json={
        "parent_meter_no": "M-HZ-01", "child_meter_no": "BR-9",
        "parent_role": "master", "child_role": "branch",
        "effective_time": "2026-10-01T00:00:00"})
    assert r.status_code == 403
