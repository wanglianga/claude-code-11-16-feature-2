"""SQLAlchemy 模型：原件索引 / 来源记录修订 / 计算依据 / 结算修订 分层存储。"""
from datetime import datetime, date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .database import Base

# ---- 枚举（字符串常量，避免方言枚举迁移问题）----
ROLE_FINANCE = "finance"          # 平台财务：全部站点、账单确认
ROLE_OPERATOR = "operator"        # 运营：冲突/换表确认
ROLE_PROPERTY = "property"        # 物业：仅分配给自己站点的核对事项

REV_DUPLICATE = "duplicate"            # 同号同内容
REV_APPLIED = "applied"                # 同号内容变化，已生效（原数据未被账单采用）
REV_BLOCKED_ADOPTED = "blocked_adopted"  # 同号内容变化但原数据已被采用，留待下一轮

ENTITY_ACTIVE = "active"
ENTITY_SUPERSEDED = "superseded"
ENTITY_PENDING = "pending_data"    # 缺起止读数等
ENTITY_SUSPENDED = "suspended"     # 回退无依据等原因挂起

JOB_RUNNING = "running"
JOB_COMPLETED = "completed"
JOB_SUSPENDED = "suspended"
JOB_FAILED = "failed"

BASIS_MEASURED = "measured"        # 读数差值实测
BASIS_ESTIMATED = "estimated"      # 跨时段无中间计量，明确标注的估算
BASIS_PENDING = "pending"          # 缺起止读数，待补
BASIS_SUSPENDED = "suspended"      # 回退无依据，整段挂起

BILL_DRAFT = "draft"
BILL_SUSPENDED = "suspended"
BILL_CONFIRMED = "confirmed"
BILL_ADJUSTED = "adjusted"

TASK_OPEN = "open"
TASK_SUBMITTED = "submitted"
TASK_RESOLVED = "resolved"

CONFLICT_OPEN = "open"
CONFLICT_RESOLVED = "resolved"

# ---- 站内差额定位：拓扑与残差 ----
TOPO_ACTIVE = "active"
TOPO_SUPERSEDED = "superseded"

# 连接关系校验问题的类别
TOPO_ISSUE_CYCLE = "cycle"                      # 连接图有环
TOPO_ISSUE_DUPLICATE_MOUNT = "duplicate_mount"  # 同一设备在同一时段重复挂接
TOPO_ISSUE_COVERAGE = "coverage_mismatch"       # 父子读数覆盖时段不一致
TOPO_ISSUE_PARENT_CHAIN = "parent_inconsistent" # 节点记录与连接关系声明的上级不一致

TOPO_ISSUE_OPEN = "open"
TOPO_ISSUE_CLEARED = "cleared"

# 支路残差：父节点电量减已知下级电量后的差额归属
RESIDUAL_TOP = "top_residual"           # 总表层扣除支路与未覆盖负荷后仍未解释的盲区
RESIDUAL_BRANCH = "branch_residual"     # 落在某个支路上，需继续定位
RESIDUAL_UNCOVERED = "uncovered_load"   # 落在未纳入连接的公共负荷/计量盲区，单列
RESIDUAL_ZERO = "zero"                  # 无残差
RESIDUAL_INVALID = "invalid_topology"   # 拓扑校验未通过，计算停止

# 缺报标记：该支路（设备）在该时窗内没有可用订单依据
GAP_ORDER = "order_gap"           # 完全缺订单
GAP_READING = "reading_gap"       # 订单有、设备读数缺
GAP_PARTIAL = "partial_coverage"  # 订单时段未覆盖整层计量区间

# 现场复核案件
CASE_OPEN = "open"
CASE_IN_PROGRESS = "in_progress"
CASE_EVIDENCE_SUBMITTED = "evidence_submitted"
CASE_CLOSED = "closed"          # 证据已提交，后续由财务独立判断采纳
CASE_SUPERSEDED = "superseded"  # 补报数据后重算，旧结论失效

# 现场证据类型
EVIDENCE_REPAIR = "repair"            # 检修记录
EVIDENCE_PUBLIC_LOAD = "public_load"  # 未覆盖公共负荷
EVIDENCE_METER_ERROR = "meter_error"  # 计量错误

# 财务采纳结论
ADOPTION_ADOPTED = "adopted"      # 可用于结算
ADOPTION_PENDING = "pending"      # 待财务判断
ADOPTION_REJECTED = "rejected"    # 不采纳，不得减少应付费用

RESIDUAL_JOB_RUNNING = "running"
RESIDUAL_JOB_COMPLETED = "completed"
RESIDUAL_JOB_BLOCKED = "blocked"  # 拓扑校验未通过，残差计算停止


class Site(Base):
    __tablename__ = "sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    name: Mapped[str] = mapped_column(String(128))
    timezone: Mapped[str] = mapped_column(String(64))  # IANA 时区，如 Asia/Shanghai
    diff_threshold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4), default=Decimal("0.02"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sites: Mapped[list["UserSite"]] = relationship(back_populates="user")


class UserSite(Base):
    """物业账号与站点的授权范围。"""
    __tablename__ = "user_sites"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"))
    user: Mapped[User] = relationship(back_populates="sites")
    __table_args__ = (UniqueConstraint("user_id", "site_id"),)


class Meter(Base):
    __tablename__ = "meters"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_no: Mapped[str] = mapped_column(String(64))
    meter_kind: Mapped[str] = mapped_column(String(16))  # master=物业总表 / branch=配电支路 / device=设备表
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_cumulative: Mapped[bool] = mapped_column(Boolean, default=True)  # 累计/增量
    __table_args__ = (UniqueConstraint("site_id", "meter_no"),)


class ImportFile(Base):
    """原件索引：原始文件本身落盘并留痕。"""
    __tablename__ = "import_files"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32))  # reading/order/tariff/public_load/replacement
    original_name: Mapped[str] = mapped_column(String(255))
    storage_path: Mapped[str] = mapped_column(String(512))
    sha256: Mapped[str] = mapped_column(String(64))
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    imported_by: Mapped[str] = mapped_column(String(64))
    imported_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ImportRow(Base):
    """原件逐行留存：文件 id + 行号 + 原始行文本 + 处理结果。"""
    __tablename__ = "import_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    import_file_id: Mapped[int] = mapped_column(ForeignKey("import_files.id"), index=True)
    line_no: Mapped[int] = mapped_column(Integer)
    raw_line: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str] = mapped_column(String(32))  # inserted/revised/duplicate/blocked_adopted/rejected
    source_record_no: Mapped[str | None] = mapped_column(String(96), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(255), nullable=True)


class SourceRecord(Base):
    """按“来源+来源记录号+站点”识别同一条业务记录的所有修订版本。"""
    __tablename__ = "source_records"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    source_type: Mapped[str] = mapped_column(String(32))
    source_record_no: Mapped[str] = mapped_column(String(96))
    current_revision_id: Mapped[int | None] = mapped_column(
        ForeignKey("record_revisions.id", use_alter=True, name="fk_source_current_revision"),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    revisions: Mapped[list["RecordRevision"]] = relationship(
        order_by="RecordRevision.revision_no",
        foreign_keys="RecordRevision.source_record_id",
    )
    __table_args__ = (UniqueConstraint("site_id", "source_type", "source_record_no"),)


class RecordRevision(Base):
    __tablename__ = "record_revisions"
    id: Mapped[int] = mapped_column(primary_key=True)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"), index=True)
    revision_no: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(32))  # applied / duplicate / blocked_adopted
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    import_row_id: Mapped[int] = mapped_column(ForeignKey("import_rows.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DeviceReplacement(Base):
    """设备/电表更换依据；回退处理必须先查到本记录。"""
    __tablename__ = "device_replacements"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"))
    effective_ts: Mapped[datetime] = mapped_column(DateTime)  # UTC
    old_final_reading: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    new_initial_reading: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    evidence_note: Mapped[str] = mapped_column(Text)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class MeterReading(Base):
    """设备/总表累计读数；每次修订产生新行，旧行不可变、不被覆盖。"""
    __tablename__ = "meter_readings"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    reading_ts: Mapped[datetime] = mapped_column(DateTime, index=True)  # UTC
    reading_ts_local: Mapped[datetime] = mapped_column(DateTime)
    reading_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    is_correction: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("record_revisions.id"))
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index("ix_reading_meter_ts", "meter_id", "reading_ts"),)


class ChargingOrder(Base):
    __tablename__ = "charging_orders"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    order_no: Mapped[str] = mapped_column(String(96), index=True)
    device_id: Mapped[str] = mapped_column(String(64), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)       # UTC
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    start_reading_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    end_reading_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    reported_energy_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))  # 设备上报电量，仅作比对
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    conflict_id: Mapped[int | None] = mapped_column(
        ForeignKey("order_conflicts.id", use_alter=True, name="fk_order_conflict"),
        nullable=True,
    )
    source_record_id: Mapped[int] = mapped_column(ForeignKey("source_records.id"))
    revision_id: Mapped[int] = mapped_column(ForeignKey("record_revisions.id"))
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class Tariff(Base):
    __tablename__ = "tariffs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    effective_date: Mapped[date] = mapped_column(Date)   # 站点本地日期起生效
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    segments: Mapped[list["TariffSegment"]] = relationship(
        back_populates="tariff", cascade="all, delete-orphan", order_by="TariffSegment.start_hour"
    )


class TariffSegment(Base):
    """分时段：本地时间 start_hour:00 起、到 end_hour:00（不含），价格 price_per_kwh。"""
    __tablename__ = "tariff_segments"
    id: Mapped[int] = mapped_column(primary_key=True)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariffs.id"), index=True)
    segment_name: Mapped[str] = mapped_column(String(32))  # peak/flat/valley
    start_hour: Mapped[int] = mapped_column(Integer)
    end_hour: Mapped[int] = mapped_column(Integer)
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    tariff: Mapped[Tariff] = relationship(back_populates="segments")


class PublicLoad(Base):
    """经确认的公共负荷（如公共照明）；未经确认不得计入。"""
    __tablename__ = "public_loads"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    energy_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    load_name: Mapped[str] = mapped_column(String(128))
    confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(16), default=ENTITY_ACTIVE)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    adopted_by_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class ReconciliationJob(Base):
    """核算作业：冻结所用读数、电价、订单与分摊规则。"""
    __tablename__ = "reconciliation_jobs"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    cycle_start: Mapped[datetime] = mapped_column(DateTime)  # UTC
    cycle_end: Mapped[datetime] = mapped_column(DateTime)
    cycle_start_local: Mapped[datetime] = mapped_column(DateTime)
    cycle_end_local: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=JOB_RUNNING)
    threshold_pct: Mapped[Decimal] = mapped_column(Numeric(6, 4))
    rerun_of_job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    rule_version: Mapped[str] = mapped_column(String(16), default="1.0")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    bill: Mapped["Bill | None"] = relationship(back_populates="job", uselist=False)


class JobInput(Base):
    """作业输入快照：重跑沿用相同输入，执行期间的更正不进入本轮。"""
    __tablename__ = "job_inputs"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    input_kind: Mapped[str] = mapped_column(String(32))  # reading/order/tariff/public_load/replacement
    entity_id: Mapped[int] = mapped_column(Integer)
    revision_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    __table_args__ = (UniqueConstraint("job_id", "input_kind", "entity_id"),)


class ReconciliationInterval(Base):
    """计算依据：总表计量区间内各成分电量，财务可逐段下钻。"""
    __tablename__ = "reconciliation_intervals"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    master_reading_start: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    master_reading_end: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    master_energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    device_meter_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    charging_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))       # 实测订单电量
    estimated_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))      # 标注估算
    pending_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))        # 待补（不计金额）
    public_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    unexplained_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    suspended_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    order_meter_diff_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    basis_status: Mapped[str] = mapped_column(String(16), default=BASIS_MEASURED)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderIntervalSplit(Base):
    """订单按电价边界拆分后的最小计费段。"""
    __tablename__ = "order_interval_splits"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"), index=True)
    reconciliation_interval_id: Mapped[int | None] = mapped_column(
        ForeignKey("reconciliation_intervals.id"), nullable=True
    )
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    segment_name: Mapped[str] = mapped_column(String(32))
    price_per_kwh: Mapped[Decimal] = mapped_column(Numeric(12, 6))
    energy_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    basis_status: Mapped[str] = mapped_column(String(16))
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class Bill(Base):
    __tablename__ = "bills"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), unique=True)
    bill_no: Mapped[str] = mapped_column(String(64), unique=True)
    master_total_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    charging_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    estimated_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    pending_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    pending_order_count: Mapped[int] = mapped_column(Integer, default=0)
    public_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    unexplained_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    suspended_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    charging_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    public_amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    status: Mapped[str] = mapped_column(String(16), default=BILL_DRAFT)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    job: Mapped[ReconciliationJob] = relationship(back_populates="bill")
    adjustments: Mapped[list["BillAdjustment"]] = relationship(back_populates="bill")


class BillAdjustment(Base):
    """已确认账单的差额更正：保留原值，以更正行体现差异，不直接替换。"""
    __tablename__ = "bill_adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    bill_id: Mapped[int] = mapped_column(ForeignKey("bills.id"), index=True)
    source_job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"))
    component: Mapped[str] = mapped_column(String(32))  # charging/public/unexplained/...
    original_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    corrected_value: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    delta: Mapped[Decimal] = mapped_column(Numeric(14, 4))
    reason: Mapped[str] = mapped_column(Text)
    # 与现场复核的相互引用：说明 -> 残差 -> 财务采纳 -> 账单更正
    case_id: Mapped[int | None] = mapped_column(ForeignKey("field_review_cases.id"), nullable=True)
    residual_id: Mapped[int | None] = mapped_column(ForeignKey("branch_residuals.id"), nullable=True)
    adoption_id: Mapped[int | None] = mapped_column(ForeignKey("finance_adoptions.id"), nullable=True)
    created_by: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    bill: Mapped[Bill] = relationship(back_populates="adjustments")


class OrderConflict(Base):
    """两个不同订单声称占用同一设备时段。"""
    __tablename__ = "order_conflicts"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    device_id: Mapped[str] = mapped_column(String(64))
    order_a_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"))
    order_b_id: Mapped[int] = mapped_column(ForeignKey("charging_orders.id"))
    status: Mapped[str] = mapped_column(String(16), default=CONFLICT_OPEN)
    resolved_order_id: Mapped[int | None] = mapped_column(ForeignKey("charging_orders.id"), nullable=True)
    resolution_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class ReviewTask(Base):
    """核对事项：物业只能看到并处理分配给自己站点的条目。"""
    __tablename__ = "review_tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    job_id: Mapped[int | None] = mapped_column(ForeignKey("reconciliation_jobs.id"), nullable=True)
    task_kind: Mapped[str] = mapped_column(String(40))
    # meter_rollback / missing_reading / order_conflict / unexplained_diff / public_confirmation / estimated_item
    title: Mapped[str] = mapped_column(String(255))
    detail: Mapped[str] = mapped_column(Text)
    assignee: Mapped[str] = mapped_column(String(16))  # property / operator / finance
    status: Mapped[str] = mapped_column(String(16), default=TASK_OPEN)
    response: Mapped[str | None] = mapped_column(Text, nullable=True)
    responded_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


# ================= 站内计量差额定位与现场复核 =================
#
# 层次：总表(master) -> 配电支路(branch) -> 设备表(device)。
# 连接关系按生效时间存版本；同一时段内「上级电量 - 已知下级电量」逐层计算残差，
# 残差只用于缩小可疑范围，不定性为设备故障。

class TopologyEdge(Base):
    """一条有效连接关系：上级节点供电给下级节点，自 effective_ts 起生效。"""
    __tablename__ = "topology_edges"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    parent_meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    child_meter_id: Mapped[int] = mapped_column(ForeignKey("meters.id"), index=True)
    parent_role: Mapped[str] = mapped_column(String(16))  # master / branch
    child_role: Mapped[str] = mapped_column(String(16))   # branch / device
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)  # 子节点为设备时
    effective_ts: Mapped[datetime] = mapped_column(DateTime)   # UTC
    effective_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)  # 改线后旧关系的终止时间
    evidence_note: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default=TOPO_ACTIVE)
    source_record_id: Mapped[int | None] = mapped_column(ForeignKey("source_records.id"), nullable=True)
    revision_id: Mapped[int | None] = mapped_column(ForeignKey("record_revisions.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    __table_args__ = (Index("ix_topo_edge_site_eff", "site_id", "effective_ts"),)


class TopologyIssue(Base):
    """拓扑校验问题：环、重复挂接、覆盖不一致。计算遇到未清除问题即停止并指出对象。"""
    __tablename__ = "topology_issues"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    issue_kind: Mapped[str] = mapped_column(String(24))
    subject: Mapped[str] = mapped_column(String(255))  # 涉及的表号/边 id，便于直接指出对象
    detail: Mapped[str] = mapped_column(Text)
    detected_at_local: Mapped[datetime] = mapped_column(DateTime)
    detected_at_ts: Mapped[datetime] = mapped_column(DateTime)
    status: Mapped[str] = mapped_column(String(16), default=TOPO_ISSUE_OPEN)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class JobTopologySnapshot(Base):
    """作业冻结的连接关系版本：关系变更与核算同时发生时保留原输入。"""
    __tablename__ = "job_topology_snapshots"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    edge_id: Mapped[int] = mapped_column(ForeignKey("topology_edges.id"))
    parent_meter_no: Mapped[str] = mapped_column(String(64))
    child_meter_no: Mapped[str] = mapped_column(String(64))
    parent_role: Mapped[str] = mapped_column(String(16))
    child_role: Mapped[str] = mapped_column(String(16))
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    effective_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    evidence_note: Mapped[str] = mapped_column(Text, default="")
    __table_args__ = (UniqueConstraint("job_id", "edge_id"),)


class JobResidualSummary(Base):
    """站级差额定位结论：差额集中支路 / 缺报时段 / 未覆盖负荷分列，供运维判断。"""
    __tablename__ = "job_residual_summaries"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), unique=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default=RESIDUAL_JOB_RUNNING)
    master_total_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    known_children_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    branch_residual_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    uncovered_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    top_residual_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    order_gap_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    gap_windows: Mapped[str | None] = mapped_column(Text, nullable=True)
    concentrated_branches: Mapped[str | None] = mapped_column(Text, nullable=True)
    conclusion: Mapped[str] = mapped_column(Text, default="")
    blocked_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    copied_from_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class BranchResidual(Base):
    """一层一档：某上级节点在其计量区间内，扣掉已知下级电量后的残差。"""
    __tablename__ = "branch_residuals"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    parent_meter_no: Mapped[str] = mapped_column(String(64))
    parent_role: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(255), default="")  # 展示对象：支路/负荷/盲区
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    parent_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    known_children_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    residual_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    residual_pct: Mapped[Decimal | None] = mapped_column(Numeric(8, 4), nullable=True)
    order_gap_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    uncovered_kwh: Mapped[Decimal] = mapped_column(Numeric(14, 4), default=Decimal("0"))
    unexplained_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    residual_kind: Mapped[str] = mapped_column(String(20), default=RESIDUAL_BRANCH)
    has_order_gap: Mapped[bool] = mapped_column(Boolean, default=False)
    has_reading_gap: Mapped[bool] = mapped_column(Boolean, default=False)
    suspect: Mapped[bool] = mapped_column(Boolean, default=False)  # 读数齐全仍持续偏离，才进入现场复核
    continues_previous: Mapped[bool] = mapped_column(Boolean, default=False)  # 与上一个时窗同向偏离
    case_id: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 生成/关联的现场复核工单
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class OrderGap(Base):
    """缺报区间：某设备在某时窗内订单覆盖不足，先追数据。"""
    __tablename__ = "order_gaps"
    id: Mapped[int] = mapped_column(primary_key=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    meter_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    gap_kind: Mapped[str] = mapped_column(String(24))
    missing_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    covered_seconds: Mapped[int] = mapped_column(Integer, default=0)
    total_seconds: Mapped[int] = mapped_column(Integer, default=0)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class FieldReviewCase(Base):
    """现场复核工单：只对读数齐全仍持续偏离的支路开启；改线/补报后旧结论失效。"""
    __tablename__ = "field_review_cases"
    id: Mapped[int] = mapped_column(primary_key=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    job_id: Mapped[int] = mapped_column(ForeignKey("reconciliation_jobs.id"), index=True)
    residual_id: Mapped[int | None] = mapped_column(ForeignKey("branch_residuals.id"), nullable=True)
    meter_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    branch_path: Mapped[str] = mapped_column(String(255))
    start_ts: Mapped[datetime] = mapped_column(DateTime)
    end_ts: Mapped[datetime] = mapped_column(DateTime)
    start_ts_local: Mapped[datetime] = mapped_column(DateTime)
    end_ts_local: Mapped[datetime] = mapped_column(DateTime)
    residual_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    reason: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=CASE_OPEN)
    assignee: Mapped[str] = mapped_column(String(16), default=ROLE_OPERATOR)
    handling_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    handled_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    superseded_by_job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    superseded_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class CaseEvidence(Base):
    """现场说明：检修 / 未覆盖公共负荷 / 计量错误。与残差、采纳记录相互引用。"""
    __tablename__ = "case_evidence"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("field_review_cases.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    evidence_kind: Mapped[str] = mapped_column(String(24))
    claim_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    description: Mapped[str] = mapped_column(Text)
    attachment_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)  # 工单号/照片编号等
    submitted_by: Mapped[str] = mapped_column(String(64))
    submitted_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    would_reduce_payable: Mapped[bool] = mapped_column(Boolean, default=False)


class FinanceAdoption(Base):
    """财务独立判断哪些现场说明可用于结算；未采纳说明不得减少应付费用。"""
    __tablename__ = "finance_adoptions"
    id: Mapped[int] = mapped_column(primary_key=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("field_review_cases.id"), index=True)
    evidence_id: Mapped[int] = mapped_column(ForeignKey("case_evidence.id"), index=True)
    site_id: Mapped[int] = mapped_column(ForeignKey("sites.id"), index=True)
    bill_id: Mapped[int | None] = mapped_column(ForeignKey("bills.id"), nullable=True)
    decision: Mapped[str] = mapped_column(String(16), default=ADOPTION_PENDING)
    adopted_kwh: Mapped[Decimal | None] = mapped_column(Numeric(14, 4), nullable=True)
    allowed_to_reduce_payable: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
