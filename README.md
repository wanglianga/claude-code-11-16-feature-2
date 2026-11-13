# 充电站月度核算平台（FastAPI + PostgreSQL + React）

把**物业总表读数、配电支路读数、设备累计读数、充电订单、分时电价**接起来，按站点时区与电价生效边界
逐段拆分、同范围核对，生成物业与站方可以逐段对账的月度账单。在账单之外，再按「总表 → 配电支路 →
充电设备」的有效连接关系逐层计算残差，帮助运维判断差额是**集中在某个支路**，还是**只因某个时窗缺少订单**，
并为读数齐全仍持续偏离的支路生成现场复核工单。系统**不接真实电表、不发起扣款**，只做数据核对、
差额定位、现场说明与财务采纳留痕。

## 一、业务规则与实现位置

| 规则（题目要求） | 实现方式 |
| --- | --- |
| 导入保留原始文件与行号，按来源记录号识别重复 | `import_files` 落盘原件+sha256；`import_rows` 保存物理行号与原始行文本；`source_records`+`record_revisions` 按 `(站点, 来源类型, record_no)` 管理版本 |
| 同号内容变化进修订，不覆盖已被账单采用的数据 | 未采用：新版本 applied、旧版本 superseded；已采用：新版本 `blocked_adopted`，下一轮核算前才提升（`services/importer.py`） |
| 时间先转站点时区，再按电价边界拆区间 | 存储统一 UTC，展示用站点本地时间；`tariff_svc.tariff_boundaries` 生成本地午夜/分时切换/电价版本边界 |
| 读数差值与订单用电在相同范围比较 | 以总表相邻读数构成计量区间，设备表/订单/公共负荷按时长裁剪到同一区间后比较（`reconciliation.py`） |
| 跨电价时段缺中间计量值只能出估算项 | 有边界中间读数→实测拆分；否则把**已知总电量**按持续时间拆分并标注 `estimated`；缺起止读数且无上报值→`pending` 待补，绝不按时间编电量 |
| 保留充电、经确认公共负荷、未解释差额，不按订单摊平 | 账单三类成分分列；未确认公共负荷生成物业核对事项且不计入；未解释差额超站点阈值→账单挂起，不自动归零 |
| 读数回退先查设备更换，无依据整段挂起、不产生负电量 | `build_spans`：有换表记录且首尾读数齐全→桥接计算；否则 `suspended`，并生成运营核对事项 |
| 同设备重复上报只计一次；不同订单抢同一时段交运营确认 | 完全同时段→停用重复单+核对事项；部分重叠→`order_conflicts`，确认前两张单都不计入 |
| 作业冻结读数/电价/订单/规则，重跑沿用相同输入，执行期更正留下一轮 | `job_inputs` 快照 + 实体 `adopted_by_job_id`；重跑复制快照且电价查询限定冻结版本；期间更正自动 blocked |
| 确认前检查新修订；已确认账单只做差额更正 | `billing.check_new_revisions`（含 blocked 修订）阻止确认；`bill_adjustments` 保留原值、以 delta 更正，不替换原字段 |
| 物业只能处理分配给自己的核对事项 | JWT 角色 + `user_sites` 授权范围，后端每个接口强制校验（`deps.py`） |
| 金额小数精度 | 金额按 4 位小数 `ROUND_HALF_UP` 计算（展示 2 位），电量 4 位；拆分尾差并入末段保证合计守恒 |
| 先登记总表-支路-设备有效连接，按时段逐层计算上级减下级 | `topology_edges` 按 `effective_ts`/`end_ts` 存有效版本；`services/topology.py` 在每个时窗解析「当时生效」的接线，逐层求残差（`branch_residuals`） |
| 差额定位只缩小可疑范围，不得直接定性为设备故障 | 残差仅作可疑范围；现场复核工单 `reason` 明确「不构成设备故障结论」，账单未解释差额不被改写 |
| 缺报区间先追数据；读数齐全仍持续偏离才进现场复核 | `order_gaps` 标记缺订单/覆盖不足；只有连续时窗、订单与读数齐全、同向超 2% 容差的支路才生成 `field_review_cases` |
| 未覆盖公共负荷单列，不能为缩小差额改拓扑 | 未挂接设备与已确认公共负荷记为 `uncovered_load` 单列；改拓扑仅能由登记关系接口调整，并有校验拦截 |
| 连接图有环/重复挂接/父子覆盖不同 -> 计算停止并指出对象 | `validate_topology` 产出 `topology_issues`（含 `subject`），残差状态置 `blocked`，并生成运营核对事项 |
| 现场改线按生效时间分段，历史差额按当时接线解释 | 作业冻结 `job_topology_snapshots`；改线变更时刻并入残差窗口边界，重跑沿用快照；总表读数区间被改线切开时，上级电量按时间比例分摊**仅用于定位**，不改写账单金额 |
| 补到数据要重算，旧工单故障判断不能自动成为新账依据 | 非重跑作业先把重叠期旧工单置 `superseded`（记录 `superseded_by_job_id`），重算生成新残差与新工单 |
| 关系变更与核算同时发生时保留原计算输入 | 作业发起即快照连接关系；重跑完全沿用原快照，后续新作业才用新版本 |
| 现场说明 / 残差 / 财务采纳相互引用；未采纳不得减费 | `case_evidence` → `finance_adoptions`（财务独立判断）→ `bill_adjustments.adoption_id/case_id/residual_id`；无采纳引用的减费更正返回 409 |

## 二、目录结构

```
16/
├── docker-compose.yml          # 一键启动 pg + backend + frontend
├── backend/
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py             # FastAPI 入口（启动自动建表，/docs Swagger UI）
│   │   ├── models.py           # 原件/修订/计算依据/结算修订 + 拓扑/残差/复核/采纳 表
│   │   ├── services/           # importer / reconciliation / topology / review / tariff / billing
│   │   ├── routers/            # auth sites imports jobs bills tasks conflicts source-records entities topology
│   │   └── seed.py             # 账号、站点、样例数据幂等播种
│   ├── sample_data/            # 三站点样例 CSV（含生成器 generate_samples.py）
│   └── tests/                  # pytest 29 个用例
└── frontend/
    ├── Dockerfile + nginx.conf # 静态构建 + SPA/API 反代
    └── src/pages/              # 登录、账单下钻、差额定位/现场复核、连接关系、作业、导入、核对事项、冲突、实体查询
```

## 三、一键启动（Docker Compose）

```bash
cd /workspace/16-feature-2
docker compose up --build
```

- 前端：http://localhost:8080
- 后端 API：http://localhost:8077 ，Swagger UI：http://localhost:8077/docs
- PostgreSQL：localhost:5432（库/用户/密码 `ccdev`/`ccdev`/`ccdev`，库名 `charging_settlement`）
- 后端容器启动时自动建表并幂等播种账号、站点与样例数据。
- 国内网络构建可传镜像源：
  `docker compose build --build-arg PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple --build-arg NPM_REGISTRY=https://registry.npmmirror.com`

## 四、本地开发启动（无 Docker 时）

前置：本机 PostgreSQL 已建库（本隔离环境为 127.0.0.1:5432）。

```bash
# 1. 后端
cd backend
/opt/python-3.12/bin/python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python sample_data/generate_samples.py     # 生成三站点样例 CSV（S01/S02/S03）
.venv/bin/python -m app.seed                         # 建表 + 账号 + 导入样例
.venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8077

# 2. 前端（另开终端）
cd frontend
/opt/node-22/bin/npm install --cache /tmp/npm-cache
/opt/node-22/bin/npm run dev                         # http://127.0.0.1:5173 ，/api 自动代理到 8077
```

## 五、测试账号

| 角色 | 用户名 | 密码 | 权限 |
| --- | --- | --- | --- |
| 平台财务 | `finance` | `Finance@123` | 全部站点；导入、发起/重跑核算、确认账单、登记差额更正、**独立采纳/不采纳现场说明** |
| 运营 | `operator` | `Operator@123` | 导入；登记连接关系、处理订单冲突与拓扑校验事项、提交现场说明 |
| 上海物业 | `property_sh` | `Property@123` | 仅 S01，只能看到/回复分配给 property 的核对事项 |
| 乌鲁木齐物业 | `property_xj` | `Property@123` | 仅 S02，同上 |
| 杭州物业 | `property_hz` | `Property@123` | 仅 S03，同上；可提交现场说明（不能做财务采纳） |

## 六、样例数据覆盖的场景（均被测试与冒烟实际执行）

- **S01 上海（Asia/Shanghai，UTC+8）**：周读数总表 2600kWh；订单跨 peak/flat 且有 10:00 中间计量→实测拆分；
  跨 peak/valley 无中间计量→估算拆分；断网订单 `OR-SH-009` 先只报电量、后补起止读数（同号修订生效）；
  已确认公共照明 320kWh；两个电价版本 10-15 切换；未解释差额 30kWh（1.15%，阈值内，账单可确认）；
  作业后到达的同号更正 `orders_v3_after_adopt.csv` 演示 blocked_adopted。
- **S02 乌鲁木齐（Asia/Urumqi，UTC+6）**：M-C 换表带首尾读数→桥接 1650kWh；M-E 读数回退无换表依据→整段挂起；
  完全同时段重复订单只计一次；部分重叠订单冲突（运营确认前均不计）；无任何依据订单→待补；
  公共负荷未确认→物业核对事项、不计入；未解释差额 250kWh（7.81% > 2% 阈值）→账单挂起不可确认。
- **S03 杭州临平（Asia/Shanghai，UTC+8，站级阈值 5%）**：总表 7872kWh，经 `topology.csv` 登记
  「M-HZ-01 → BR-1/BR-2/BR-3 → 设备」三级连接；**BR-3 每个时窗残差 90~100kWh 且持续同向**→生成现场复核工单
  `BR-3`（累计 385kWh），BR-1/BR-2 残差为 0（不被摊平）；**DEV-H3 仅 10-08~10-15 一个时窗缺订单**→记为缺报
  （先追数据，不生成现场工单）；DEV-H2 于 10-15 **现场改线**由 BR-1 改挂 BR-2（按生效时间分段）；
  `M-H5`（未挂接设备）与已确认公共负荷 372kWh 记为未覆盖负荷**单列**；未确认公共负荷 60kWh 不进入残差；
  `orders_v2_backfill.csv` 用于演示补报后重算、旧现场工单转 `superseded`。

## 七、验证方式与实际验证结论

以下均为在本隔离环境**实际执行过**的验证（2026-11-13）：

1. **后端单元/规则测试**：`cd backend && PYTHONPATH=. .venv/bin/python -m pytest tests -q`
   → **29 passed**（19 个原有 + 10 个新增）。原有覆盖：原件行号留存、同号重复、修订生效、采用后 blocked 不覆盖、
   时区边界实测拆分、估算/待补标注、回退挂起不负电量、换表桥接、重复/冲突订单、未解释差额与阈值挂起、
   重跑输入冻结、重跑不受作业后新电价影响、金额精度与合计守恒、物业站点隔离、确认前修订拦截、
   差额更正原值保留、冲突处理。新增覆盖：支路集中 vs 缺订单分离、未覆盖负荷单列、改线按生效时间分段、
   重跑沿用拓扑快照（作业后改线只影响新作业）、环/重复挂接/父子覆盖不一致均停止计算并指出对象、
   现场说明需财务采纳才可减费、超出采纳电量多减被拒、补报重算使旧工单 superseded、拓扑/复核 API 全链路。
2. **HTTP 端到端冒烟**（真实 uvicorn 进程 + curl）：
   - 既有链路：登录四角色 → S01 completed / S02 suspended → 账单下钻 → 挂起账单确认 409 →
     S01 确认成功 → 减费更正无采纳引用返回 409 → 乌鲁木齐物业回复、上海物业跨站 403 → 运营解决冲突。
   - S03 新链路：8 条连接关系 → 拓扑预检 0 问题 → 发起核算（S03 completed，未被站级阈值挂起）→
     残差汇总（BR-3 集中 385kWh、DEV-H3 缺报 500kWh、未覆盖 672kWh）→ 16 条残差 / 1 条缺报 →
     现场工单 → 运营提交检修说明 → 运营调用采纳接口 403 → 财务未采纳时减费 409 →
     财务采纳 40kWh → 引用采纳登记减费更正 200 → 杭州物业可见本人工单。
3. **前端**：`npm run build` TypeScript 类型检查与生产构建成功；Vite dev 启动后首页 200、
   `/api` 代理 health 200 验证通过。新增「差额定位/现场复核」「连接关系」两个页面，账单页可按采纳记录登记更正。
   **OpenAPI**：`/openapi.json` 返回 38 个路径，含 `/api/topology/*`、`/api/jobs/{id}/residual*`、
   `/api/field-cases/*`；`/docs` Swagger UI 返回 200。
4. **Docker**：本隔离容器未安装 docker 守护进程，`Dockerfile`/`docker-compose.yml` 已交付但**未在此环境实际构建运行**；
   配置依据上述已验证的本地启动步骤编写，启动命令与端口见第三节。

## 八、主要 API 一览（详见 /docs Swagger UI）

- `POST /api/auth/login` 登录获取 JWT
- `POST /api/imports` 上传 CSV（multipart：site_id、source_type、file）；`GET /api/imports/files/{id}/rows` 原件逐行
- `POST /api/jobs` 发起核算（周期为站点本地时间，支持 `rerun_of_job_id` 冻结重跑）
- `GET /api/jobs/{id}/intervals`、`/splits` 区间与订单拆分下钻
- `GET /api/bills`、`/api/bills/{id}/precheck`、`/confirm`、`/adjustments`
- `GET /api/tasks`、`POST /api/tasks/{id}/respond` 核对事项（后端按角色与站点授权过滤）
- `GET /api/conflicts`、`POST /api/conflicts/{id}/resolve` 订单冲突
- `GET /api/source-records`、`/{id}/revisions` 来源记录与修订链
- `GET /api/entities/readings|orders|public-loads|replacements` 原始依据查询
- 差额定位与现场复核（`routers/topology.py`）：
  - `GET/POST /api/topology/edges?site_id=`、`DELETE /api/topology/edges/{id}` 连接关系登记/停用（按生效时间版本化）
  - `GET /api/topology/check?site_id=&cycle_start=&cycle_end=` 独立预检（环/重复挂接/父子覆盖）
  - `GET /api/jobs/{id}/residual-summary`、`/residuals`、`/order-gaps` 差额定位结论与逐层残差、缺报
  - `GET /api/field-cases`、`/api/field-cases/{id}` 现场复核工单与说明/采纳详情
  - `POST /api/field-cases/{id}/evidence` 现场说明（检修/公共负荷/计量错误，运营或物业提交）
  - `POST /api/field-cases/evidence/{id}/adoption` 财务独立采纳（`adopted|pending|rejected`）
  - `POST /api/bills/{id}/adjustments` 支持 `adoption_id`：减少应付费用必须引用已采纳说明

## 九、CSV 列格式

所有文件首行为表头，必须含 `record_no`（来源记录号）。完整示例见 `backend/sample_data/S01`、`S02`、`S03`：

- 读数（master_reading / branch_reading / device_reading）：`record_no,meter_no,device_id,reading_time,reading_kwh,is_correction`
- 连接关系 topology：`record_no,parent_meter_no,child_meter_no,parent_role,child_role,device_id,effective_time,end_time,evidence_note`
  （`parent_role` 为 master/branch，`child_role` 为 branch/device；改线用新 `record_no` 并按生效时间分段）
- 订单 order：`record_no,order_no,device_id,start_time,end_time,start_reading,end_reading,reported_energy`
- 电价 tariff：`record_no,tariff_name,effective_date,end_date,segment_name,start_hour,end_hour,price`（一个 record_no 可含多个分时段行）
- 公共负荷 public_load：`record_no,load_name,start_time,end_time,energy_kwh,confirmed`
- 换表 replacement：`record_no,meter_no,effective_time,old_final_reading,new_initial_reading,evidence_note`

## 十、本题（站内差额定位与现场复核）验证步骤

前置：按第四节完成本地启动并 `python -m app.seed`（会自动导入 S03 杭州临平样例）。

1. **看连接关系与预检**：登录 `operator` → 「连接关系」页选 S03 → 应看到 8 条关系（含 10-15 改线记录）；
   点「预检连接关系」→ 显示「校验通过」。
2. **发起核算并看定位结论**：登录 `finance` → 「核算作业」发起 S03 2026-10-01~2026-11-01；
   到「差额定位/现场复核」页选中该作业 → 结论应指出差额集中在 **BR-3（累计 385 kWh）**，
   同时列出 1 个缺报时窗 **DEV-H3 10-08~10-15（500 kWh）**，未覆盖负荷 672 kWh 单列；BR-1/BR-2 残差为 0。
3. **现场复核闭环**：现场复核工单里打开 BR-3 工单 → 以 `operator`/`property_hz` 提交「检修记录」说明（可填申报 kWh）；
   登录 `finance` 对说明做采纳判断（待判断/采纳/不采纳）。
4. **财务独立决定**：确认 S03 账单后，在账单页登记差额更正；若更正为**减少应付费用**，必须选择已采纳的说明，
   否则后端返回 409；减少量超过采纳电量时返回 400。
5. **补报重算使旧结论失效**：通过「原件与导入」导入 `backend/sample_data/S03/orders_v2_backfill.csv`
   （来源类型 `order`），重新发起核算 → 缺报时窗消失，旧的 BR-3 现场工单状态变为「已失效（数据重算）」，
   新工单承接新残差。
6. **冻结与改线**：对同一作业执行「重跑」→ 残差与规则输入保持不变；作业后新增/停用连接关系只影响新发起的作业。

对应自动化测试：`backend/tests/test_topology.py`（9 个）与 `backend/tests/test_api.py::test_topology_and_field_case_flow`。
