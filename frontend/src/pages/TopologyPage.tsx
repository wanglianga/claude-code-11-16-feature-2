import { useEffect, useState } from "react";
import { api, errText } from "../api";
import { useAuth } from "../auth";
import type { Site, TopologyEdge, TopologyIssue } from "../types";

const ISSUE_LABEL: Record<string, string> = {
  cycle: "连接图有环",
  duplicate_mount: "重复挂接",
  coverage_mismatch: "父子覆盖不一致",
  parent_inconsistent: "上级声明不一致",
};

export default function TopologyPage() {
  const { role } = useAuth();
  const canEdit = role === "finance" || role === "operator";
  const [sites, setSites] = useState<Site[]>([]);
  const [siteId, setSiteId] = useState<number | "">("");
  const [edges, setEdges] = useState<TopologyEdge[]>([]);
  const [issues, setIssues] = useState<TopologyIssue[]>([]);
  const [msg, setMsg] = useState("");
  const [err, setErr] = useState("");
  const [form, setForm] = useState({
    parent_meter_no: "",
    child_meter_no: "",
    parent_role: "branch",
    child_role: "device",
    device_id: "",
    effective_time: "2026-10-01T00:00:00",
    evidence_note: "",
  });

  const load = async () => {
    const { data: s } = await api.get<Site[]>("/api/sites");
    setSites(s);
    if (siteId === "") return;
    const { data } = await api.get<TopologyEdge[]>("/api/topology/edges", {
      params: { site_id: siteId },
    });
    setEdges(data);
  };
  useEffect(() => { load(); /* eslint-disable-next-line */ }, [siteId]);

  const check = async () => {
    if (siteId === "") return;
    setErr(""); setMsg("");
    try {
      const { data } = await api.get("/api/topology/check", {
        params: { site_id: siteId, cycle_start: "2026-10-01T00:00:00",
          cycle_end: "2026-11-01T00:00:00" },
      });
      setIssues(data.issues);
      setMsg(data.issues.length === 0 ? "校验通过：无环、无重复挂接、父子覆盖一致"
        : `校验未通过，共 ${data.issues.length} 项，相关计算会停止`);
    } catch (e) { setErr(errText(e)); }
  };

  const create = async () => {
    if (siteId === "") { setErr("请选择站点"); return; }
    setErr(""); setMsg("");
    try {
      await api.post("/api/topology/edges", {
        ...form,
        device_id: form.child_role === "device" ? (form.device_id || null) : null,
        effective_time: form.effective_time,
      }, { params: { site_id: siteId } });
      setMsg("连接关系已登记；同一子节点的旧关系会自动按生效时间收口");
      load();
    } catch (e) { setErr(errText(e)); }
  };

  const deactivate = async (edge: TopologyEdge) => {
    setErr(""); setMsg("");
    try {
      await api.delete(`/api/topology/edges/${edge.id}`);
      setMsg(`已停用 ${edge.parent_meter_no} → ${edge.child_meter_no}；已冻结作业仍按原快照解释历史差额`);
      load();
    } catch (e) { setErr(errText(e)); }
  };

  return (
    <div>
      <h2 className="page-title">连接关系（总表 → 配电支路 → 充电设备）</h2>
      <div className="filter-bar">
        <label>站点</label>
        <select value={siteId} onChange={(e) => setSiteId(e.target.value === "" ? "" : Number(e.target.value))}>
          <option value="">请选择</option>
          {sites.map((s) => <option key={s.id} value={s.id}>{s.code} {s.name}</option>)}
        </select>
        <button className="secondary" onClick={check} disabled={siteId === ""}>预检连接关系</button>
      </div>
      {msg && <div className="ok-msg">{msg}</div>}
      {err && <div className="error">{err}</div>}

      {issues.length > 0 && (
        <div className="panel">
          <h2>校验问题（计算遇到未清除问题即停止，并指出对象）</h2>
          <table>
            <thead><tr><th>类别</th><th>对象</th><th>说明</th></tr></thead>
            <tbody>
              {issues.map((i) => (
                <tr key={i.id}>
                  <td><span className="badge suspended">{ISSUE_LABEL[i.issue_kind] ?? i.issue_kind}</span></td>
                  <td className="mono">{i.subject}</td>
                  <td className="muted">{i.detail}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {canEdit && (
        <div className="panel">
          <h2>登记一条有效连接关系</h2>
          <div className="row">
            <div><label>上级表号</label><input value={form.parent_meter_no}
              onChange={(e) => setForm({ ...form, parent_meter_no: e.target.value })} /></div>
            <div><label>上级角色</label>
              <select value={form.parent_role} onChange={(e) => setForm({ ...form, parent_role: e.target.value })}>
                <option value="master">总表</option><option value="branch">支路</option>
              </select></div>
            <div><label>下级表号</label><input value={form.child_meter_no}
              onChange={(e) => setForm({ ...form, child_meter_no: e.target.value })} /></div>
            <div><label>下级角色</label>
              <select value={form.child_role} onChange={(e) => setForm({ ...form, child_role: e.target.value })}>
                <option value="branch">支路</option><option value="device">设备</option>
              </select></div>
            {form.child_role === "device" && (
              <div><label>设备号</label><input value={form.device_id}
                onChange={(e) => setForm({ ...form, device_id: e.target.value })} /></div>
            )}
            <div><label>生效时间（本地）</label><input value={form.effective_time}
              onChange={(e) => setForm({ ...form, effective_time: e.target.value })} /></div>
            <div style={{ flex: 1, minWidth: 220 }}><label>依据说明</label>
              <input style={{ width: "100%" }} value={form.evidence_note}
                onChange={(e) => setForm({ ...form, evidence_note: e.target.value })} /></div>
            <button onClick={create}>登记</button>
          </div>
          <div className="muted" style={{ marginTop: 8 }}>
            现场改线后按生效时间分段，历史差额仍按当时接线解释；变更同时发生核算时，已冻结作业保留原输入。
            不能为缩小差额随意改拓扑。
          </div>
        </div>
      )}

      <div className="panel">
        <h2>连接关系版本（按生效时间）</h2>
        <table>
          <thead><tr><th>上级</th><th>下级</th><th>角色</th><th>设备</th>
            <th>生效（本地）</th><th>终止（本地）</th><th>说明</th><th>状态</th><th></th></tr></thead>
          <tbody>
            {edges.map((e) => (
              <tr key={e.id}>
                <td className="mono">{e.parent_meter_no}</td>
                <td className="mono">{e.child_meter_no}</td>
                <td>{e.parent_role}→{e.child_role}</td>
                <td>{e.device_id ?? "—"}</td>
                <td className="mono">{e.effective_ts_local.slice(0, 16)}</td>
                <td className="mono">{e.end_ts_local ? e.end_ts_local.slice(0, 16) : "—"}</td>
                <td className="muted">{e.evidence_note}</td>
                <td><span className={`badge ${e.status === "active" ? "confirmed" : "duplicate"}`}>{e.status}</span></td>
                <td>{canEdit && e.status === "active" && (
                  <button className="secondary" onClick={() => deactivate(e)}>停用</button>
                )}</td>
              </tr>
            ))}
            {edges.length === 0 && <tr><td colSpan={9} className="muted">该站点尚未登记连接关系</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}
