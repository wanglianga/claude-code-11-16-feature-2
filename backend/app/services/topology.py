"""站内计量差额定位：连接关系校验 + 逐层残差 + 缺报定位 + 现场复核工单。

约束：
- 连接关系按生效时间分段；同一时窗内先解析“当时生效”的接线，再做残差。
- 残差 = 上级电量 - 已知直接下级电量；只用于缩小可疑范围，**不定性为设备故障**。
- 连接图有环、设备重复挂接、父子读数覆盖时段不同 -> 残差计算停止并指出对象。
- 缺报区间先追数据；读数与订单齐全、残差持续同向偏离的支路才生成现场复核工单。
- 未纳入连接关系的公共负荷与设备电量单列，不从支路残差中扣减，也不为缩小差额改拓扑。
- 作业冻结连接关系快照；关系变更与核算同时发生时，旧作业用原输入，后续作业用新版本。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models
from ..config import get_settings
from .reconciliation import build_spans, clip_span_energy
from .timeutil import duration_seconds, quantize, utc_to_local

settings = get_settings()
ZERO = Decimal("0")
Q4 = settings.energy_scale


# ---------------- 连接关系视图 ----------------

@dataclass
class EdgeView:
    edge_id: int | None
    parent_meter_no: str
    child_meter_no: str
    parent_role: str          # master / branch
    child_role: str           # branch / device
    device_id: str | None
    effective_ts: datetime
    end_ts: datetime | None
    evidence_note: str = ""


def edge_to_view(edge: models.TopologyEdge, parent_no: str, child_no: str) -> EdgeView:
    return EdgeView(
        edge_id=edge.id,
        parent_meter_no=parent_no,
        child_meter_no=child_no,
        parent_role=edge.parent_role,
        child_role=edge.child_role,
        device_id=edge.device_id,
        effective_ts=edge.effective_ts,
        end_ts=edge.end_ts,
        evidence_note=edge.evidence_note or "",
    )


def snapshot_to_view(row: models.JobTopologySnapshot) -> EdgeView:
    return EdgeView(
        edge_id=row.edge_id,
        parent_meter_no=row.parent_meter_no,
        child_meter_no=row.child_meter_no,
        parent_role=row.parent_role,
        child_role=row.child_role,
        device_id=row.device_id,
        effective_ts=row.effective_ts,
        end_ts=row.end_ts,
        evidence_note=row.evidence_note or "",
    )


def load_job_edges(db: Session, job: models.ReconciliationJob) -> list[EdgeView]:
    rows = db.scalars(
        select(models.JobTopologySnapshot)
        .where(models.JobTopologySnapshot.job_id == job.id)
        .order_by(models.JobTopologySnapshot.effective_ts)
    ).all()
    return [snapshot_to_view(r) for r in rows]


def snapshot_topology(
    db: Session,
    site: models.Site,
    job: models.ReconciliationJob,
    source_job: models.ReconciliationJob | None,
) -> list[EdgeView]:
    """首轮快照当前生效连接；重跑复制源作业快照，不受期间关系变更影响。"""
    if source_job is not None:
        rows = db.scalars(
            select(models.JobTopologySnapshot).where(
                models.JobTopologySnapshot.job_id == source_job.id
            )
        ).all()
        for r in rows:
            db.add(models.JobTopologySnapshot(
                job_id=job.id, edge_id=r.edge_id,
                parent_meter_no=r.parent_meter_no, child_meter_no=r.child_meter_no,
                parent_role=r.parent_role, child_role=r.child_role,
                device_id=r.device_id, effective_ts=r.effective_ts,
                end_ts=r.end_ts, evidence_note=r.evidence_note,
            ))
        db.flush()
        return load_job_edges(db, job)

    meters = {m.id: m for m in db.scalars(
        select(models.Meter).where(models.Meter.site_id == site.id)
    ).all()}
    edges = db.scalars(
        select(models.TopologyEdge).where(
            models.TopologyEdge.site_id == site.id,
            models.TopologyEdge.status == models.TOPO_ACTIVE,
        ).order_by(models.TopologyEdge.effective_ts)
    ).all()
    for e in edges:
        parent = meters.get(e.parent_meter_id)
        child = meters.get(e.child_meter_id)
        if parent is None or child is None:
            continue
        db.add(models.JobTopologySnapshot(
            job_id=job.id, edge_id=e.id,
            parent_meter_no=parent.meter_no, child_meter_no=child.meter_no,
            parent_role=e.parent_role, child_role=e.child_role,
            device_id=e.device_id, effective_ts=e.effective_ts,
            end_ts=e.end_ts, evidence_note=e.evidence_note or "",
        ))
    db.flush()
    return load_job_edges(db, job)


def resolved_edges_at(edges: list[EdgeView], ts: datetime) -> dict[str, EdgeView]:
    """某时刻生效的接线：同一子节点取生效时间最新、且未终止的一条。"""
    chosen: dict[str, EdgeView] = {}
    for e in edges:
        if e.effective_ts > ts:
            continue
        if e.end_ts is not None and e.end_ts <= ts:
            continue
        cur = chosen.get(e.child_meter_no)
        if cur is None or e.effective_ts > cur.effective_ts:
            chosen[e.child_meter_no] = e
    return chosen


# ---------------- 拓扑校验 ----------------

def _coverage(readings: list[models.MeterReading], start: datetime, end: datetime):
    ts = [r.reading_ts for r in readings if start <= r.reading_ts <= end]
    if not ts:
        return None
    return min(ts), max(ts)


def validate_topology(
    db: Session,
    site: models.Site,
    job: models.ReconciliationJob,
    edges: list[EdgeView],
    meters_by_no: dict[str, models.Meter],
    readings_by_meter_id: dict[int, list[models.MeterReading]],
    start_utc: datetime,
    end_utc: datetime,
    tasks: list[models.ReviewTask],
) -> list[models.TopologyIssue]:
    """返回并落库校验问题；有问题时残差计算必须停止。"""
    issues: list[models.TopologyIssue] = []
    tz = site.timezone

    def add(kind: str, subject: str, detail: str):
        issues.append(models.TopologyIssue(
            site_id=site.id, issue_kind=kind, subject=subject, detail=detail,
            detected_at_local=utc_to_local(datetime.utcnow(), tz),
            detected_at_ts=datetime.utcnow(),
        ))

    # 1) 环检测（父 -> 子，带环路径以便直接指出对象）
    graph: dict[str, set[str]] = {}
    for e in edges:
        graph.setdefault(e.parent_meter_no, set()).add(e.child_meter_no)
    color: dict[str, int] = {}
    stack: list[str] = []

    def dfs(node: str) -> None:
        color[node] = 1
        stack.append(node)
        for nxt in sorted(graph.get(node, ())):
            if color.get(nxt, 0) == 0:
                dfs(nxt)
            elif color.get(nxt) == 1:
                idx = stack.index(nxt)
                cycle = stack[idx:] + [nxt]
                add(models.TOPO_ISSUE_CYCLE, " -> ".join(cycle),
                    "连接关系构成环，无法确定上下级，残差计算停止。")
        stack.pop()
        color[node] = 2

    for node in sorted(graph):
        if color.get(node, 0) == 0:
            dfs(node)

    # 2) 重复挂接：同一子节点在重叠时段存在多条接线
    by_child: dict[str, list[EdgeView]] = {}
    for e in edges:
        by_child.setdefault(e.child_meter_no, []).append(e)
    for child_no, items in by_child.items():
        items.sort(key=lambda x: (x.effective_ts, x.edge_id or 0))
        seen_ts: dict[datetime, int] = {}
        for e in items:
            seen_ts[e.effective_ts] = seen_ts.get(e.effective_ts, 0) + 1
        flagged: set[datetime] = set()
        for e in items:
            if seen_ts[e.effective_ts] > 1 and e.effective_ts not in flagged:
                flagged.add(e.effective_ts)
                same = [x for x in items if x.effective_ts == e.effective_ts]
                parents = sorted({x.parent_meter_no for x in same})
                add(models.TOPO_ISSUE_DUPLICATE_MOUNT, child_no,
                    f"设备/支路 {child_no} 在同一生效时刻 "
                    f"{utc_to_local(e.effective_ts, tz)} 同时挂在 "
                    f"{', '.join(parents)} 下，同一时段重复挂接，残差计算停止。")
        for idx, e in enumerate(items[:-1]):
            nxt = items[idx + 1]
            if e.end_ts is not None and e.end_ts > nxt.effective_ts:
                add(models.TOPO_ISSUE_DUPLICATE_MOUNT, child_no,
                    f"连接关系 #{e.edge_id}（{e.parent_meter_no}>{child_no}）终止时间 "
                    f"{utc_to_local(e.end_ts, tz)} 晚于下一条接线 #{nxt.edge_id}"
                    f"（{nxt.parent_meter_no}>{child_no}）的生效时间 "
                    f"{utc_to_local(nxt.effective_ts, tz)}，同一时段重复挂接。")

    # 3) 父子读数覆盖时段不同（整个核算周期比较）
    checked: set[tuple[str, str]] = set()
    for e in edges:
        key = (e.parent_meter_no, e.child_meter_no)
        if key in checked:
            continue
        checked.add(key)
        pm, cm = meters_by_no.get(e.parent_meter_no), meters_by_no.get(e.child_meter_no)
        if pm is None or cm is None:
            add(models.TOPO_ISSUE_PARENT_CHAIN, e.child_meter_no,
                f"连接关系引用的表不存在（上级 {e.parent_meter_no} / 下级 {e.child_meter_no}）。")
            continue
        pc = _coverage(readings_by_meter_id.get(pm.id, []), start_utc, end_utc)
        cc = _coverage(readings_by_meter_id.get(cm.id, []), start_utc, end_utc)
        if pc is None or cc is None or pc != cc:
            def fmt(c):
                return "无读数" if c is None else (
                    f"{utc_to_local(c[0], tz)} ~ {utc_to_local(c[1], tz)}")
            add(models.TOPO_ISSUE_COVERAGE, f"{e.parent_meter_no} / {e.child_meter_no}",
                f"上级 {e.parent_meter_no} 读数覆盖 {fmt(pc)}，下级 {e.child_meter_no} "
                f"覆盖 {fmt(cc)}，覆盖时段不一致，残差计算停止；先补齐读数或确认接线。")

    for issue in issues:
        db.add(issue)
    db.flush()
    if issues:
        subject_list = "；".join(
            f"[{i.issue_kind}] {i.subject}" for i in issues[:6]
        )
        tasks.append(models.ReviewTask(
            site_id=site.id, job_id=job.id,
            task_kind="topology_issue",
            title="连接关系校验未通过，差额定位计算已停止",
            detail=f"请先处理以下对象后重跑：{subject_list}",
            assignee=models.ROLE_OPERATOR,
        ))
    return issues


# ---------------- 电量与订单工具 ----------------

def meter_energy(
    db: Session, site: models.Site, meter: models.Meter,
    readings: list[models.MeterReading], t1: datetime, t2: datetime,
    task_list: list[models.ReviewTask],
) -> tuple[Decimal, bool]:
    """返回 (窗口内电量, 是否存在无法量化的挂起段)。"""
    spans = build_spans(db, site.id, site.timezone, meter, readings, t1, t2, task_list)
    total = ZERO
    suspended = False
    for span, _, _ in spans:
        if span.energy is None:
            if span.start < t2 and span.end > t1:
                suspended = True
            continue
        clip = clip_span_energy(span, t1, t2)
        if clip:
            total += clip
    return quantize(total, Q4), suspended


def _order_sums(orders: list[models.ChargingOrder], t1: datetime, t2: datetime):
    """窗口内订单覆盖秒数、已知订单电量（实测+上报估算）。"""
    covered = 0.0
    energy = ZERO
    for o in orders:
        lo, hi = max(o.start_ts, t1), min(o.end_ts, t2)
        if lo >= hi:
            continue
        covered += duration_seconds(lo, hi)
        full = duration_seconds(o.start_ts, o.end_ts)
        if full <= 0:
            continue
        if o.start_reading_kwh is not None and o.end_reading_kwh is not None:
            total = o.end_reading_kwh - o.start_reading_kwh
        else:
            total = o.reported_energy_kwh or ZERO
        if total and total > 0:
            ratio = Decimal(str(duration_seconds(lo, hi) / full))
            energy += total * ratio
    return covered, quantize(energy, Q4)


def detect_order_gap(
    job_id: int,
    site: models.Site,
    device_id: str,
    device_kwh: Decimal,
    orders: list[models.ChargingOrder],
    t1: datetime,
    t2: datetime,
) -> tuple[models.OrderGap | None, Decimal]:
    """设备在该窗口有电量但订单覆盖不足时返回缺报记录与缺失电量。"""
    if device_kwh <= 0:
        return None, ZERO
    covered, order_kwh = _order_sums(orders, t1, t2)
    total = duration_seconds(t1, t2)
    ratio = covered / total if total > 0 else 1.0
    missing = quantize(max(ZERO, device_kwh - order_kwh), Q4)
    tol = Decimal(settings.residual_tolerance_pct)
    if ratio >= 0.95 and missing <= tol * device_kwh:
        return None, ZERO
    if covered <= 0:
        kind = models.GAP_ORDER
        note = "该时窗完全没有订单依据，先追数据，不能据此判定设备故障"
    elif ratio < 0.95:
        kind = models.GAP_PARTIAL
        note = "订单时段未覆盖整层计量区间，缺报部分先追数据"
    else:
        kind = models.GAP_ORDER
        note = "订单电量明显低于设备计量电量，先核查是否漏报"
    gap = models.OrderGap(
        job_id=job_id, site_id=site.id, device_id=device_id,
        start_ts=t1, end_ts=t2,
        start_ts_local=utc_to_local(t1, site.timezone),
        end_ts_local=utc_to_local(t2, site.timezone),
        gap_kind=kind, missing_kwh=missing,
        covered_seconds=int(covered), total_seconds=int(total), note=note,
    )
    return gap, missing


def _supersede_prior_cases(db: Session, site: models.Site, job: models.ReconciliationJob) -> None:
    """补报/新数据重算时，旧工单的故障判断失效，不得自动成为新账依据。"""
    prior = db.scalars(
        select(models.FieldReviewCase).where(
            models.FieldReviewCase.site_id == site.id,
            models.FieldReviewCase.job_id != job.id,
            models.FieldReviewCase.status.in_([
                models.CASE_OPEN, models.CASE_IN_PROGRESS,
                models.CASE_EVIDENCE_SUBMITTED,
            ]),
        )
    ).all()
    for case in prior:
        if case.end_ts <= job.cycle_start or case.start_ts >= job.cycle_end:
            continue
        case.status = models.CASE_SUPERSEDED
        case.superseded_by_job_id = job.id
        case.superseded_reason = (
            f"作业 #{job.id} 使用补报/新数据重新计算残差，"
            f"原现场结论不再作为新账依据，需按新残差重新判断。"
        )


# ---------------- 残差分析主流程 ----------------

def analyze_job(
    db: Session,
    job: models.ReconciliationJob,
    site: models.Site,
    adopted_readings: list[models.MeterReading],
    adopted_orders: list[models.ChargingOrder],
    adopted_public: list[models.PublicLoad],
    master_intervals: list[dict],
    start_utc: datetime,
    end_utc: datetime,
    tasks: list[models.ReviewTask],
    supersede_prior: bool,
) -> models.JobResidualSummary:
    """逐层计算残差并落库。master_intervals 为核算引擎已算好的总表区间汇总。"""
    edges = load_job_edges(db, job)
    summary = models.JobResidualSummary(job_id=job.id, site_id=site.id)

    if not edges:
        summary.status = models.RESIDUAL_JOB_COMPLETED
        summary.conclusion = (
            "尚未登记总表-支路-设备连接关系，无法进行站内定位；"
            "差额仍按账单未解释项整体列示，不能直接摊给设备。"
        )
        db.add(summary)
        db.flush()
        return summary

    meters = db.scalars(select(models.Meter).where(models.Meter.site_id == site.id)).all()
    meters_by_no = {m.meter_no: m for m in meters}
    readings_by_meter_id: dict[int, list[models.MeterReading]] = {m.id: [] for m in meters}
    for r in adopted_readings:
        readings_by_meter_id.setdefault(r.meter_id, []).append(r)
    for pid in readings_by_meter_id:
        readings_by_meter_id[pid].sort(key=lambda r: r.reading_ts)

    issues = validate_topology(
        db, site, job, edges, meters_by_no, readings_by_meter_id,
        start_utc, end_utc, tasks,
    )
    if issues:
        summary.status = models.RESIDUAL_JOB_BLOCKED
        summary.blocked_reason = "；".join(
            f"[{i.issue_kind}] {i.subject}" for i in issues
        )[:2000]
        summary.conclusion = "连接关系校验未通过，差额定位计算停止；请先处理上述对象。"
        db.add(summary)
        db.flush()
        return summary

    if supersede_prior:
        _supersede_prior_cases(db, site, job)

    # 现场改线：接线变更时刻并入残差窗口边界，历史差额按当时接线解释
    change_points = sorted({
        ts for e in edges for ts in (e.effective_ts, e.end_ts)
        if ts is not None and start_utc < ts < end_utc
    })

    residual_rows: list[models.BranchResidual] = []
    gap_rows: list[models.OrderGap] = []
    scratch_tasks: list[models.ReviewTask] = []  # 逐层电量只取数，不重复生成核对事项
    order_by_device: dict[str, list[models.ChargingOrder]] = {}
    for o in adopted_orders:
        order_by_device.setdefault(o.device_id, []).append(o)
    device_meters = [m for m in meters if m.meter_kind == "device"]

    total_master = ZERO
    total_top_children = ZERO
    total_uncovered = ZERO
    total_top_residual = ZERO

    for mi in master_intervals:
        master_no = mi["meter_no"]
        t1, t2 = mi["t1"], mi["t2"]
        master_energy = mi["energy"]
        if master_energy is None:
            continue
        total_master += master_energy
        interval_seconds = duration_seconds(t1, t2)

        # 区间内已确认公共负荷（按时间比例裁剪）
        public_interval = ZERO
        for pl in adopted_public:
            if not pl.confirmed or pl.end_ts <= t1 or pl.start_ts >= t2:
                continue
            full = duration_seconds(pl.start_ts, pl.end_ts)
            part = duration_seconds(max(pl.start_ts, t1), min(pl.end_ts, t2))
            if full > 0 and part > 0:
                public_interval += quantize(pl.energy_kwh * Decimal(str(part / full)), Q4)

        cuts = sorted({t1, t2, *[c for c in change_points if t1 < c < t2]})
        windows = [(cuts[i], cuts[i + 1]) for i in range(len(cuts) - 1)]
        for w1, w2 in windows:
            w_seconds = duration_seconds(w1, w2)
            share = Decimal(str(w_seconds / interval_seconds)) if interval_seconds else ZERO
            master_share = quantize(master_energy * share, Q4)
            public_share = quantize(public_interval * share, Q4)

            resolved = resolved_edges_at(edges, w1)
            top_edges = [e for e in resolved.values() if e.parent_meter_no == master_no]

            energy_cache: dict[str, tuple[Decimal, bool]] = {}

            def energy_of(no: str) -> tuple[Decimal, bool]:
                if no not in energy_cache:
                    m = meters_by_no.get(no)
                    if m is None:
                        energy_cache[no] = (ZERO, False)
                    else:
                        energy_cache[no] = meter_energy(
                            db, site, m, readings_by_meter_id.get(m.id, []),
                            w1, w2, scratch_tasks,
                        )
                return energy_cache[no]

            # ---- 支路层：支路电量 - 已知直接下级 ----
            for be in top_edges:
                branch_no = be.child_meter_no
                branch_energy, branch_suspended = energy_of(branch_no)
                child_edges = [
                    e for e in resolved.values() if e.parent_meter_no == branch_no
                ]
                children_kwh = ZERO
                child_gap = False
                child_suspend = False
                gap_missing = ZERO
                for ce in child_edges:
                    de, susp = energy_of(ce.child_meter_no)
                    children_kwh += de
                    child_suspend = child_suspend or susp
                    if ce.device_id:
                        gap, missing = detect_order_gap(
                            job.id, site, ce.device_id, de,
                            order_by_device.get(ce.device_id, []), w1, w2,
                        )
                        if gap is not None:
                            child_gap = True
                            gap_missing += missing
                            gap_rows.append(gap)
                children_kwh = quantize(children_kwh, Q4)
                residual = quantize(branch_energy - children_kwh, Q4)
                pct = (
                    quantize(abs(residual) / branch_energy, 4)
                    if branch_energy > 0 else None
                )
                note = None
                if child_suspend or branch_suspended:
                    note = ("窗口内存在无法量化的挂起计量段，残差仅供缩小范围，"
                            "不作为设备故障判断")
                row = models.BranchResidual(
                    job_id=job.id, site_id=site.id,
                    parent_meter_no=branch_no, parent_role="branch",
                    subject=f"{master_no} > {branch_no}",
                    start_ts=w1, end_ts=w2,
                    start_ts_local=utc_to_local(w1, site.timezone),
                    end_ts_local=utc_to_local(w2, site.timezone),
                    parent_kwh=branch_energy, known_children_kwh=children_kwh,
                    residual_kwh=residual, residual_pct=pct,
                    order_gap_kwh=quantize(gap_missing, Q4),
                    has_order_gap=child_gap,
                    has_reading_gap=child_suspend or branch_suspended,
                    residual_kind=(
                        models.RESIDUAL_ZERO if residual == 0 else models.RESIDUAL_BRANCH
                    ),
                    note=note,
                )
                db.add(row)
                residual_rows.append(row)

            # ---- 总表层：总表 - 已挂接支路 - 未纳入连接的已知负荷 ----
            top_children_kwh = ZERO
            for e in top_edges:
                de, _ = energy_of(e.child_meter_no)
                top_children_kwh += de
            top_children_kwh = quantize(top_children_kwh, Q4)
            total_top_children += top_children_kwh

            mounted_device_ids = {e.device_id for e in resolved.values() if e.device_id}
            unmounted_kwh = ZERO
            unmounted_names: list[str] = []
            for m in device_meters:
                if not m.device_id or m.device_id in mounted_device_ids:
                    continue
                de, _ = meter_energy(
                    db, site, m, readings_by_meter_id.get(m.id, []),
                    w1, w2, scratch_tasks,
                )
                if de <= 0:
                    continue
                unmounted_kwh += de
                unmounted_names.append(m.meter_no)
                gap, missing = detect_order_gap(
                    job.id, site, m.device_id, de,
                    order_by_device.get(m.device_id, []), w1, w2,
                )
                if gap is not None:
                    gap_rows.append(gap)
            unmounted_kwh = quantize(unmounted_kwh, Q4)

            uncovered = quantize(unmounted_kwh + public_share, Q4)
            total_uncovered += uncovered
            top_residual = quantize(master_share - top_children_kwh - uncovered, Q4)
            total_top_residual += top_residual

            subject_bits = []
            if unmounted_names:
                subject_bits.append("未挂接设备 " + ", ".join(unmounted_names))
            if public_share > 0:
                subject_bits.append(f"公共负荷 {public_share} kWh")
            if uncovered != 0:
                db.add(models.BranchResidual(
                    job_id=job.id, site_id=site.id,
                    parent_meter_no=master_no, parent_role="master",
                    subject="；".join(subject_bits) or "未覆盖负荷",
                    start_ts=w1, end_ts=w2,
                    start_ts_local=utc_to_local(w1, site.timezone),
                    end_ts_local=utc_to_local(w2, site.timezone),
                    parent_kwh=master_share, known_children_kwh=top_children_kwh,
                    residual_kwh=uncovered, residual_pct=None,
                    uncovered_kwh=uncovered,
                    residual_kind=models.RESIDUAL_UNCOVERED,
                    note="未纳入连接关系的已知负荷，单列，不摊给支路",
                ))
            if top_residual != 0:
                db.add(models.BranchResidual(
                    job_id=job.id, site_id=site.id,
                    parent_meter_no=master_no, parent_role="master",
                    subject=f"{master_no} 站级盲区",
                    start_ts=w1, end_ts=w2,
                    start_ts_local=utc_to_local(w1, site.timezone),
                    end_ts_local=utc_to_local(w2, site.timezone),
                    parent_kwh=master_share, known_children_kwh=top_children_kwh,
                    residual_kwh=top_residual, residual_pct=None,
                    uncovered_kwh=uncovered,
                    residual_kind=models.RESIDUAL_TOP,
                    note="总表扣除已知支路与未覆盖负荷后的盲区，需继续补充连接关系或排查",
                ))

    db.flush()
    for gap in gap_rows:
        db.add(gap)
    db.flush()

    # ---- 持续偏离：读数与订单齐全、连续时窗同向超过容差才进入现场复核 ----
    tol = Decimal(settings.residual_tolerance_pct)
    grouped: dict[str, list[models.BranchResidual]] = {}
    for row in residual_rows:
        grouped.setdefault(row.subject, []).append(row)

    concentrated: list[str] = []
    case_count = 0
    for subject, rows in grouped.items():
        rows.sort(key=lambda r: r.start_ts)
        # 缺报/挂起时窗先追数据，不参与持续偏离判定；剩余的读数齐全时窗仍需连续同向偏离
        usable = [
            r for r in rows
            if not r.has_order_gap and not r.has_reading_gap and r.residual_kwh is not None
        ]
        if len(usable) < 2:
            continue
        signs = {1 if (r.residual_kwh or 0) > 0 else -1 for r in usable if r.residual_kwh}
        over = [
            r for r in usable
            if r.parent_kwh and r.parent_kwh > 0
            and abs(r.residual_kwh) > tol * r.parent_kwh
        ]
        if len(signs) != 1 or len(over) != len(usable):
            continue
        total_res = quantize(sum((r.residual_kwh for r in usable), ZERO), Q4)
        total_parent = sum((r.parent_kwh for r in usable), ZERO)
        if total_parent <= 0 or abs(total_res) <= tol * total_parent:
            continue
        rows = usable
        for i, r in enumerate(rows):
            r.suspect = True
            r.continues_previous = i > 0 and (
                (rows[i - 1].residual_kwh or 0) * (r.residual_kwh or 0)
            ) > 0
        branch_no = rows[0].parent_meter_no
        case = models.FieldReviewCase(
            site_id=site.id, job_id=job.id,
            residual_id=rows[0].id, meter_no=branch_no,
            branch_path=subject,
            start_ts=rows[0].start_ts, end_ts=rows[-1].end_ts,
            start_ts_local=rows[0].start_ts_local,
            end_ts_local=rows[-1].end_ts_local,
            residual_kwh=total_res,
            reason=(
                f"支路 {branch_no} 在 {len(rows)} 个连续时窗读数与订单齐全，"
                f"残差持续同向偏离，累计 {total_res} kWh"
                f"（占该支路 {quantize(abs(total_res) / total_parent * 100, 2)}%）。"
                f"残差只用于缩小复核范围，不构成设备故障结论。"
            ),
            status=models.CASE_OPEN,
        )
        db.add(case)
        db.flush()
        for r in rows:
            r.case_id = case.id
        for r in rows:
            residual_kwh = r.residual_kwh or ZERO
            if residual_kwh > 0:
                concentrated.append(f"{branch_no}({total_res} kWh)")
                break
        case_count += 1

    # ---- 缺报窗口汇总（同一设备同一窗口只列一次） ----
    gap_windows: list[str] = []
    seen_gap: set[tuple[str, datetime]] = set()
    for g in gap_rows:
        key = (g.device_id or g.meter_no or "", g.start_ts)
        if key in seen_gap:
            continue
        seen_gap.add(key)
        gap_windows.append(
            f"{utc_to_local(g.start_ts, site.timezone):%m-%d %H:%M}~"
            f"{utc_to_local(g.end_ts, site.timezone):%m-%d %H:%M} "
            f"{g.device_id or g.meter_no}:{g.gap_kind}"
            f"({g.missing_kwh if g.missing_kwh is not None else '—'} kWh)"
        )

    summary.master_total_kwh = quantize(total_master, Q4)
    summary.known_children_kwh = quantize(total_top_children, Q4)
    summary.branch_residual_kwh = quantize(
        sum((r.residual_kwh or ZERO for r in residual_rows if r.suspect), ZERO), Q4,
    )
    summary.uncovered_kwh = quantize(total_uncovered, Q4)
    summary.top_residual_kwh = quantize(total_top_residual, Q4)
    summary.order_gap_kwh = quantize(
        sum((g.missing_kwh or ZERO for g in gap_rows), ZERO), Q4,
    )
    summary.gap_windows = "；".join(gap_windows) or None
    summary.concentrated_branches = "、".join(concentrated) or None
    summary.status = models.RESIDUAL_JOB_COMPLETED

    parts = [
        f"总表 {summary.master_total_kwh} kWh，已挂接支路 {summary.known_children_kwh} kWh，"
        f"未覆盖负荷 {summary.uncovered_kwh} kWh（单列），"
        f"站级盲区 {summary.top_residual_kwh} kWh。"
    ]
    if concentrated:
        parts.append(
            f"可疑差额集中在支路 {'、'.join(concentrated)}；读数齐全仍持续偏离，"
            f"已生成现场复核工单，计算本身不认定设备故障。"
        )
    if gap_windows:
        parts.append(
            f"{len(gap_windows)} 个时窗存在订单缺报/覆盖不足（合计 {summary.order_gap_kwh} kWh），"
            f"先追数据；补到数据后重算残差。"
        )
    if not concentrated and not gap_windows:
        parts.append("各支路残差在容差内，未发现持续偏离，无需现场复核。")
    summary.conclusion = " ".join(parts)
    db.add(summary)
    db.flush()
    return summary
