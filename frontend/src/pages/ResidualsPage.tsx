import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type {
  Adoption, CaseEvidence, FieldCase, Job, OrderGap, Residual, ResidualSummary, Site,
} from "../types";

const KIND_LABEL: Record<string, string> = {
  branch_residual: "支路残差",
  uncovered_load: "未覆盖负荷（单列）",
  top_residual: "站级盲区",
  zero: "无残差",
  invalid_topology: "拓扑无效",
};

const GAP_LABEL: Record<string, string> = {
  order_gap: "缺订单",
  partial_coverage: "订单覆盖不足",
  reading_gap: "缺读数",
};

const CASE_STATUS: Record<string, string> = {
  open: "待现场复核",
  in_progress: "复核中",
  evidence_submitted: "已提交说明",
  closed: "已关闭（待财务判断）",
  superseded: "已失效（数据重算）",
};

const EVIDENCE_LABEL: Record<string, string> = {
  repair: "检修记录",
  public_load: "未覆盖公共负荷",
  meter_error: "计量错误",
};

const DECISION_LABEL: Record<string, string> = {
  adopted: "已采纳",
  pending: "待判断",
  rejected: "不采纳",
};

interface CaseDetail {
  case: FieldCase;
  residual: Residual | null;
  evidence: { evidence: CaseEvidence; adoption: Adoption | null }[];
  adopted_reduction_kwh: string;
}

export default function ResidualsPage() {
  const { role } = useAuth();
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [jobs, setJobs] = useState<Job[]>([]);
  const [jobId, setJobId] = useState<number | "">("");
  const [summary, setSummary] = useState<ResidualSummary | null>(null);
  const [residuals, setResiduals] = useState<Residual[]>([]);
  const [gaps, setGaps] = useState<OrderGap[]>([]);
  const [cases, setCases] = useState<FieldCase[]>([]);
  const [openCase, setOpenCase] = useState<CaseDetail | null>(null);
  const [err, setErr] = useState("");
  const [msg, setMsg] = useState("");

  useEffect(() => {
    api.get<Site[]>("/api/sites").then(({ data }) => {
      setSites(data);
      if (data.length) setSiteId(data[0].id);
    });
  }, []);

  useEffect(() => {
    if (siteId === "") return;
    api.get<Job[]>("/api/jobs", { params: { site_id: siteId } }).then(({ data }) => {
      setJobs(data);
      if (data.length) setJobId(data[0].id); else setJobId("");
    });
    api.get<FieldCase[]>("/api/field-cases", { params: { site_id: siteId } })
      .then(({ data }) => setCases(data));
  }, [siteId]);

  useEffect(() => {
    if (jobId === "") { setSummary(null); setResiduals([]); setGaps([]); return; }
    api.get<ResidualSummary>(`/api/jobs/${jobId}/residual-summary`).then(({ data }) => setSummary(data))
      .catch(() => setSummary(null));
    api.get<Residual[]>(`/api/jobs/${jobId}/residuals`).then(({ data }) => setResiduals(data));
    api.get<OrderGap[]>(`/api/jobs/${jobId}/order-gaps`).then(({ data }) => setGaps(data));
  }, [jobId]);

  const openDetail = async (c: FieldCase) => {
    setErr(""); setMsg("");
    try {
      const { data } = await api.get<CaseDetail>(`/api/field-cases/${c.id}`);
      setOpenCase(data);
    } catch (e) { setErr(errText(e)); }
  };

  const refreshCases = () =>
    api.get<FieldCase[]>("/api/field-cases", { params: { site_id: siteId } })
      .then(({ data }) => setCases(data));

  if (openCase) {
    return (
      <div>
        <button className="secondary" onClick={() => setOpenCase(null)}>← 返回差额定位</button>
        <h2 className="page-title">
          现场复核工单 #{openCase.case.id} · {openCase.case.branch_path}
          <span className={`badge ${openCase.case.status === "open" ? "pending" : "draft"}`}>
            {" "}{CASE_STATUS[openCase.case.status] ?? openCase.case.status}
          </span>
        </h2>
        {msg && <div className="ok-msg">{msg}</div>}
        {err && <div className="error">{err}</div>}
        <div className="panel">
          <h2>定位依据（残差只缩小复核范围，不构成设备故障结论）</h2>
          <table>
            <tbody>
              <tr><th>计量时窗</th><td className="mono">{openCase.case.start_ts_local.slice(0, 16)} ~ {openCase.case.end_ts_local.slice(0, 16)}</td></tr>
              <tr><th>累计残差</th><td>{openCase.case.residual_kwh} kWh</td></tr>
              <tr><th>残差说明</th><td>{openCase.case.reason}</td></tr>
              {openCase.residual && (
                <tr><th>首个时窗</th><td className="mono">
                  {openCase.residual.parent_meter_no} 上级 {openCase.residual.parent_kwh} kWh −
                  已知下级 {openCase.residual.known_children_kwh} kWh =
                  残差 {openCase.residual.residual_kwh} kWh（{openCase.residual.residual_pct ?? "—"}）
                </td></tr>
              )}
              {openCase.case.status === "superseded" && (
                <tr><th>旧结论状态</th><td className="error">{openCase.case.superseded_reason}</td></tr>
              )}
            </tbody>
          </table>
        </div>

        <CaseEvidencePanel
          detail={openCase} role={role}
          onRefresh={async () => {
            const { data } = await api.get<CaseDetail>(`/api/field-cases/${openCase.case.id}`);
            setOpenCase(data); refreshCases();
          }}
        />
      </div>
    );
  }

  return (
    <div>
      <h2 className="page-title">站内差额定位与现场复核</h2>
      <div className="filter-bar">
        <label>站点</label>
        <select value={siteId} onChange={(e) => setSiteId(Number(e.target.value))}>
          {sites.map((s) => <option key={s.id} value={s.id}>{s.code} {s.name}</option>)}
        </select>
        <label>核算作业</label>
        <select value={jobId} onChange={(e) => setJobId(e.target.value === "" ? "" : Number(e.target.value))}>
          <option value="">请选择</option>
          {jobs.map((j) => <option key={j.id} value={j.id}>#{j.id} {j.cycle_start_local.slice(0, 10)}~{j.cycle_end_local.slice(0, 10)}</option>)}
        </select>
      </div>
      {err && <div className="error">{err}</div>}

      {summary && (
        <div className="panel">
          <h2>结论：差额集中在支路，还是只因一个时窗缺少订单</h2>
          {summary.status === "blocked" ? (
            <div className="error">连接关系校验未通过，计算已停止：{summary.blocked_reason}</div>
          ) : <div>{summary.conclusion}</div>}
          <div className="stat-grid" style={{ marginTop: 12 }}>
            <div className="stat"><div className="k">总表电量 kWh</div><div className="v">{summary.master_total_kwh}</div></div>
            <div className="stat"><div className="k">已挂接支路 kWh</div><div className="v">{summary.known_children_kwh}</div></div>
            <div className="stat"><div className="k">可疑支路残差 kWh</div><div className="v warn">{summary.branch_residual_kwh}</div></div>
            <div className="stat"><div className="k">未覆盖负荷 kWh（单列）</div><div className="v">{summary.uncovered_kwh}</div></div>
            <div className="stat"><div className="k">站级盲区 kWh</div><div className="v">{summary.top_residual_kwh}</div></div>
            <div className="stat"><div className="k">缺报缺口 kWh</div><div className="v warn">{summary.order_gap_kwh}</div></div>
          </div>
          {summary.gap_windows && (
            <div className="muted" style={{ marginTop: 8 }}>缺报时窗（先追数据，不据此定性故障）：{summary.gap_windows}</div>
          )}
        </div>
      )}

      <div className="panel">
        <h2>逐层残差（上级电量 − 已知下级电量）</h2>
        <table>
          <thead><tr><th>层级/对象</th><th>时窗（本地）</th><th className="num">上级 kWh</th>
            <th className="num">已知下级 kWh</th><th className="num">残差 kWh</th><th className="num">占比</th>
            <th>分类</th><th>标记</th></tr></thead>
          <tbody>
            {residuals.map((r) => (
              <tr key={r.id}>
                <td className="mono">{r.subject}</td>
                <td className="mono">{r.start_ts_local.slice(5, 16)} ~ {r.end_ts_local.slice(5, 16)}</td>
                <td className="num">{r.parent_kwh ?? "—"}</td>
                <td className="num">{r.known_children_kwh}</td>
                <td className={`num ${r.suspect ? "danger-text" : ""}`}>{r.residual_kwh ?? "—"}</td>
                <td className="num">{r.residual_pct ?? "—"}</td>
                <td>{KIND_LABEL[r.residual_kind] ?? r.residual_kind}</td>
                <td>
                  {r.suspect && <span className="badge suspended">持续偏离</span>}
                  {r.has_order_gap && <span className="badge pending"> 缺报</span>}
                  {r.has_reading_gap && <span className="badge pending"> 挂起段</span>}
                  {r.continues_previous && <span className="muted"> 承接上次</span>}
                  {r.note && <div className="muted">{r.note}</div>}
                </td>
              </tr>
            ))}
            {residuals.length === 0 && <tr><td colSpan={8} className="muted">暂无残差结果（需先登记连接关系并完成核算）</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>缺报区间（先追数据；读数齐全仍持续偏离才进入现场复核）</h2>
        <table>
          <thead><tr><th>设备</th><th>时窗（本地）</th><th>类型</th><th className="num">缺失 kWh</th><th className="num">订单覆盖</th><th>说明</th></tr></thead>
          <tbody>
            {gaps.map((g) => (
              <tr key={g.id}>
                <td className="mono">{g.device_id ?? "—"}</td>
                <td className="mono">{g.start_ts_local.slice(5, 16)} ~ {g.end_ts_local.slice(5, 16)}</td>
                <td><span className="badge estimated">{GAP_LABEL[g.gap_kind] ?? g.gap_kind}</span></td>
                <td className="num">{g.missing_kwh ?? "—"}</td>
                <td className="num">{g.total_seconds ? `${Math.round(g.covered_seconds / g.total_seconds * 100)}%` : "—"}</td>
                <td className="muted">{g.note}</td>
              </tr>
            ))}
            {gaps.length === 0 && <tr><td colSpan={6} className="muted">无缺报时窗</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="panel">
        <h2>现场复核工单</h2>
        <table>
          <thead><tr><th>#</th><th>支路</th><th>时窗</th><th className="num">残差 kWh</th>
            <th>状态</th><th></th></tr></thead>
          <tbody>
            {cases.map((c) => (
              <tr key={c.id}>
                <td>{c.id}</td>
                <td className="mono">{c.branch_path}</td>
                <td className="mono">{c.start_ts_local.slice(5, 16)} ~ {c.end_ts_local.slice(5, 16)}</td>
                <td className="num">{c.residual_kwh ?? "—"}</td>
                <td><span className={`badge ${c.status === "superseded" ? "duplicate" : "pending"}`}>
                  {CASE_STATUS[c.status] ?? c.status}</span></td>
                <td><button className="secondary" onClick={() => openDetail(c)}>查看/处理</button></td>
              </tr>
            ))}
            {cases.length === 0 && <tr><td colSpan={6} className="muted">暂无现场复核工单</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function CaseEvidencePanel({ detail, role, onRefresh }: {
  detail: CaseDetail; role: string; onRefresh: () => void;
}) {
  const [kind, setKind] = useState("repair");
  const [claim, setClaim] = useState("");
  const [desc, setDesc] = useState("");
  const [ref, setRef] = useState("");
  const [adoptKwh, setAdoptKwh] = useState("");
  const [adoptReason, setAdoptReason] = useState("");
  const [err, setErr] = useState("");
  const canHandle = role === "operator" || role === "property";
  const canAdopt = role === "finance";

  const submit = async () => {
    setErr("");
    try {
      await api.post(`/api/field-cases/${detail.case.id}/evidence`, {
        evidence_kind: kind, claim_kwh: claim || null,
        description: desc, attachment_ref: ref || null,
      });
      setDesc(""); setClaim(""); setRef(""); onRefresh();
    } catch (e) { setErr(errText(e)); }
  };

  const decide = async (evidenceId: number, decision: string) => {
    setErr("");
    try {
      await api.post(`/api/field-cases/evidence/${evidenceId}/adoption`, {
        decision,
        adopted_kwh: decision === "adopted" ? (adoptKwh || null) : null,
        reason: adoptReason || "财务判断",
      });
      setAdoptKwh(""); setAdoptReason(""); onRefresh();
    } catch (e) { setErr(errText(e)); }
  };

  return (
    <div className="panel">
      <h2>现场说明与财务采纳（相互引用；未采纳不得减少应付费用）</h2>
      {err && <div className="error">{err}</div>}
      <table>
        <thead><tr><th>类型</th><th className="num">申报 kWh</th><th>现场说明</th><th>工单/附件</th>
          <th>提交人</th><th>财务采纳</th></tr></thead>
        <tbody>
          {detail.evidence.map(({ evidence, adoption }) => (
            <tr key={evidence.id}>
              <td>{EVIDENCE_LABEL[evidence.evidence_kind] ?? evidence.evidence_kind}</td>
              <td className="num">{evidence.claim_kwh ?? "—"}</td>
              <td>{evidence.description}</td>
              <td className="mono muted">{evidence.attachment_ref ?? "—"}</td>
              <td>{evidence.submitted_by}</td>
              <td>
                {adoption ? (
                  <>
                    <span className={`badge ${adoption.decision === "adopted" ? "confirmed" : "draft"}`}>
                      {DECISION_LABEL[adoption.decision] ?? adoption.decision}
                    </span>
                    <div className="muted">
                      采纳 {adoption.adopted_kwh ?? 0} kWh；可减应付：
                      {adoption.allowed_to_reduce_payable ? "是" : "否"}；{adoption.reason}
                    </div>
                  </>
                ) : <span className="muted">待财务判断</span>}
                {canAdopt && (
                  <div className="inline" style={{ marginTop: 6 }}>
                    <input style={{ width: 90 }} placeholder="采纳 kWh" value={adoptKwh}
                      onChange={(e) => setAdoptKwh(e.target.value)} />
                    <input style={{ width: 160 }} placeholder="判断依据" value={adoptReason}
                      onChange={(e) => setAdoptReason(e.target.value)} />
                    <button className="secondary" onClick={() => decide(evidence.id, "adopted")}>采纳</button>
                    <button className="secondary" onClick={() => decide(evidence.id, "rejected")}>不采纳</button>
                  </div>
                )}
              </td>
            </tr>
          ))}
          {detail.evidence.length === 0 && (
            <tr><td colSpan={6} className="muted">尚未提交现场说明</td></tr>
          )}
        </tbody>
      </table>
      <div className="muted" style={{ marginTop: 8 }}>
        已采纳且允许减应付的说明合计 {detail.adopted_reduction_kwh} kWh；账单更正必须引用该采纳记录。
      </div>
      {canHandle && detail.case.status !== "superseded" && (
        <div className="row" style={{ marginTop: 12 }}>
          <div><label>说明类型</label>
            <select value={kind} onChange={(e) => setKind(e.target.value)}>
              <option value="repair">检修记录</option>
              <option value="public_load">未覆盖公共负荷</option>
              <option value="meter_error">计量错误</option>
            </select></div>
          <div><label>申报电量 kWh（可空）</label><input value={claim}
            onChange={(e) => setClaim(e.target.value)} /></div>
          <div style={{ flex: 1, minWidth: 260 }}><label>现场说明</label>
            <input style={{ width: "100%" }} value={desc} onChange={(e) => setDesc(e.target.value)} /></div>
          <div><label>工单/附件编号</label><input value={ref} onChange={(e) => setRef(e.target.value)} /></div>
          <button onClick={submit} disabled={!desc}>提交说明</button>
        </div>
      )}
    </div>
  );
}
