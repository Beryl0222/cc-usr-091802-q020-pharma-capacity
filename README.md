# 医药园区能力兑现（pharma-capacity）

医药产业项目依赖服务：把企业主体、厂房产线、关键设备、技术平台、监管资质、供应商与
里程碑承诺放入一套可核对的依赖注册表，让任何 2030 规划数字都能下钻到
**去重后的项目、产能依据、阶段决定与待核事项**，回答「能力是否真正兑现」。

`fixtures/sample.json` 只含虚构样例（5 个项目、3 个园区、3 条共享产线、5 家供应商），
不含真实个人资料或业务凭据。

## 运行

```bash
python3 service.py --check                     # 装载自检
python3 service.py --port 8000                 # 启动 HTTP 服务（内存态，不落盘）
python3 -m unittest discover -s tests -v       # 50 项契约/规则测试
```

## 语义约定

### 1. 阶段门禁：未兑现不得越级

阶段固定为 `planning → licensing → equipment → acceptance → production`，
退出每一阶段所需证据：

| 阶段 | 进入下一阶段所需前置条件 |
| --- | --- |
| planning 规划签约 | 投资协议（只证明承诺） |
| licensing 许可审评 | 监管资质获批 |
| equipment 设备到位 | 设备进场核验完成 |
| acceptance 验收验证 | 验收报告完成 **且** 实际产能经核验 |
| production 投产 | 终态 |

三类来源分别保留、互不替代：**投资协议 / 验收材料（含设备到位）/ 实际产能 / 监管资质**。
融资发布（`funding_news`）只是事件，永不构成门禁证据——「刚融资」不会被记成「已投产」。
门禁不满足时推进请求返回 409，同时留下一条 `approved=false` 的阶段决定留痕。

### 2. 共享资源按「时间 × 能力单位」占用

设施以半开区间 `[start, end)` 预订，占用量 = 重叠天数 × `units_per_day`。
同一设施、同一时间段内各项目占用速率之和超过供给即报冲突；
区间端点相接不算重叠，迁移在生效日之后自动解除旧冲突。

### 3. 区域合计去重一次

跨园区联合项目（`joint=true`，多 `park_ids`）在园区合计中各园区可见，
在区域合计中按项目 id 只出现一次。每个项目同时给出三列数字：

* `claimed` —— 规划申报值（投资协议/申报材料中的承诺）；
* `reserved` / `booking_basis` —— 共享产线预订折算的产能依据；
* `delivered` —— 最近一次经核验实际产能的年化值（试生产记录未核验不计）。

每行可下钻到 `capacity_basis`（按设施、按区间）、`decisions`（阶段决定）、
`pending_items`（缺证、证据在审、占用冲突、未证实产能缺口、开放问题、未确认处置）。

### 4. 双时态版本：事件生效起换版，年报按当时证据复现

每个版本同时带：

* `effective_on` —— 业务生效日：并购、迁移、延期、口径修订、阶段推进自该日起形成新版本；
* `recorded_on` —— 系统记录日：事后补录的证据不进入历史观察点。

`GET /totals?effective=…&recorded=…` 或 `GET /reports/{id}/reproduce` 均按
双时态选择版本与证据，已发布年度报告可原样复现（例：2025 年报中的安融仍是
验收阶段、兑现产能为 0，尽管它现在已投产）。

统计口径（`calibers`）本身也是带生效日的版本：2025 口径全额计入申报值，
2026 修订口径只计预订产能依据且要求项目至少进入许可阶段。

### 5. 授权

证据可标记 `commercial: true`。未经授权的访问（含未带身份）看不到商业材料正文，
只返回 `redacted_commercial_evidence` 计数；授权按人员 × 项目授予（`admin` 可见全部）。
身份通过 `X-Principal-Id` 头或 `?as=` 参数传递。

### 6. 影响传播：系统列影响，负责人做处置

供应商中断或审批延迟后，`GET /impacts?supplier=…`（或 `?issue=…`）沿
「供应商→设备→项目→前置项目→下游项目」依赖链列出：受影响项目及传播层级、
窗口内到期的承诺缺口（含被哪个门禁/问题阻塞）、同类替代供应商与窗口内有余量的
同能力设施。系统**不自动处置**：`POST /dispositions` 必须带负责人 `owner`，
空负责人返回 400；处置留痕但不自动改写阶段或预订。

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份 |
| GET | `/projects` | 项目清单（阶段、缺证、待核数），支持双时态参数 |
| GET | `/projects/{id}` | 项目详情：版本、里程碑、预订、依赖、证据（按授权脱敏） |
| POST | `/projects/{id}/advance` | 阶段推进；门禁失败返回 409 并留痕 |
| POST | `/events` | `merger`/`relocation`/`extension`/`revision`，生效日起换版 |
| GET | `/reservations/conflicts` | 共享设施时间×能力单位冲突 |
| GET | `/facilities/{id}/occupancy?date=` | 指定日占用速率与余量 |
| GET | `/totals?scope=region\|park&id=&year=&caliber=&effective=&recorded=` | 去重合计 + 全量下钻行 |
| GET | `/reports`、`/reports/{id}/reproduce` | 年报清单与按当时证据复现 |
| GET | `/impacts?supplier=&project=&issue=&window=` | 影响项目、承诺缺口、可选资源 |
| POST | `/dispositions` | 负责人确认处置（无 owner 拒绝） |
| GET | `/decisions?project=` | 阶段决定留痕 |

写入（推进、事件、处置）只保存在进程内存中，重启回到样例状态。

## 代码结构

* `domain.py` —— 纯领域原语：阶段/门禁、证据来源、半开区间与能力单位、双时态版本选择、年化折算；
* `registry.py` —— 注册表与全部业务规则（与传输无关，可直接在脚本/测试中调用）；
* `service.py` —— HTTP 路由与 JSON 序列化；
* `fixtures/sample.json` —— 公开虚构样例；
* `tests/` —— 领域、注册表情景、HTTP 契约三层测试。
