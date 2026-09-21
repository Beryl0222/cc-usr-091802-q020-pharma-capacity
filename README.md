# 医药园区能力兑现

把企业主体、厂房产线、关键设备、技术平台、监管资质、供应商与里程碑承诺放入
一套**项目依赖服务**，让 2030 规划中的每一个数字都能下钻到「去重后的项目、
产能依据、阶段决定与待核事项」，回答能力是否真正兑现。

## 设计要点

- **事件溯源 + 双时态**：所有事实是只追加事件。`occurred_at` 是业务生效时间，
  `recorded_at` 是系统记录时间。并购、产线迁移、里程碑延期、统计口径修订都从
  生效日形成新版本；已发布年度报告按发布时刻截断证据，任何时候都能原样复现。
- **证据分源、闸门不越级**：投资协议（承诺）、验收材料（验收完成）、实际产能
  核证（兑现）是不同证据类别，分别支撑不同阶段。融资新闻不是投产证据；许可未
  获批、设备未到位、验收未完成、无核产产能时项目无法进入下一阶段。
  阶段链：`registered → planned → equipment → accepted → licensed → operational`。
- **共享资源按时空×能力单位占用**：同一条共享产线按「年份 × 能力单位」记账，
  占用之和不得超过产线能力，超售申报直接拒绝并返回当前占用方与剩余量。
- **区域合计去重**：跨园区联合项目在区域合计中只出现一次；园区分项按
  `attribution` 分成。百亿企业按 as_of 时点并购后的集团主体汇总。
- **口径版本化**：申报口径（进入规划即计）与兑现口径（必须投产且有核产产能）
  各自带生效日，修订只影响新生效期，历史口径仍可复算。
- **影响分析 + 人工确认**：关键供应商中断或审批延迟后，系统沿依赖关系列出受
  影响项目、承诺缺口（逾期里程碑、产能承诺、被卡闸门）与可选资源（备选供应商、
  空闲产线、已装设备）；切换/等待/释放等处置必须由项目负责人确认才生效。
- **商业材料授权可见**：`confidential` 证据（如投资协议、商业秘密附件）只向持
  `commercial_access` 的用户开放，未授权用户的视图会剔除这些事件。

## 目录结构

```
pharma/
  events.py    事件、证据类别、阶段与闸门要求定义
  store.py     只追加事件日志（JSONL 持久化、双时态过滤）
  world.py     事件折叠为某业务日期的世界状态（含并购链、占用账）
  engine.py    纯规则：闸门、占用校验、去重汇总、影响分析
  app.py       应用服务：命令校验/权限/年报复现
  scenario.py  合成演示场景（覆盖全部策略拒绝情形）
service.py     HTTP 入口
tests/         27 个契约/领域/HTTP 测试
fixtures/      可公开的领域边界样例（无真实资料或凭据）
```

## 运行

```bash
python3 service.py --check                       # 身份自检
python3 service.py --demo --port 8000            # 内存 + 合成场景
python3 service.py --store data/events.jsonl     # 事件持久化到磁盘
python3 -m unittest discover -s tests -v         # 全部测试
```

## HTTP API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 服务身份 |
| POST | `/commands/<name>` | 执行命令，body 携带 `user_id` 与参数 |
| GET | `/projects/<id>?as_of=&user=` | 项目下钻：阶段、产能依据、决定、待核事项 |
| GET | `/projects/<id>/gate?as_of=` | 逐阶段闸门核对与阻塞项 |
| GET | `/lines/<id>/capacity?year=&as_of=` | 共享产线占用、占用方与剩余能力 |
| GET | `/summary?parks=张江,苏州&year=2030&as_of=2030-03-01&user=` | 区域去重合计 |
| GET | `/impact?kind=supplier&id=s-api&as_of=2030-02-16` | 中断/延迟影响分析 |
| POST | `/dispositions` | 负责人确认处置（switch/wait/release） |
| GET | `/reports/<id>/reproduce?user=` | 按发布当时证据复现年度报告 |
| GET | `/events?user=` | 事件日志（商业事件需授权） |

命令名见 `pharma/app.py` 的 `COMMANDS`，常用：`register_entity`、
`register_facility`、`register_line`、`register_equipment`、
`declare_license`、`register_supplier`、`register_project`、
`declare_dependencies`、`declare_occupancy`、`declare_milestone`、
`record_evidence`、`advance_stage`、`acquire_entity`、`relocate_line`、
`revise_caliber`、`publish_report`、`set_supplier_status`。

### 快速体验（合成场景）

```bash
python3 service.py --demo --port 8000 &

# 申报 260 亿，兑现口径只计 120 亿（其余卡在许可/仅有融资新闻）
curl "http://127.0.0.1:8000/summary?parks=%E5%BC%A0%E6%B1%9F,%E8%8B%8F%E5%B7%9E,%E4%B8%B4%E6%B8%AF&year=2029&as_of=2030-03-01"

# 供应商中断：受影响项目、承诺缺口、备选供应商（处置待负责人确认）
curl "http://127.0.0.1:8000/impact?kind=supplier&id=s-api&as_of=2030-02-16"

# 复现 2028 年报（2029 年补录的验收材料不会出现）
curl "http://127.0.0.1:8000/reports/r2028/reproduce"
```

合成场景（`pharma/scenario.py`）演示了：超售占用被拒、融资新闻不能推进阶段、
跨园区联合项目只计一次、并购改变百亿集团归属、产线迁移升版、里程碑延期、
口径修订、年报双时态复现、商业材料权限与供应商中断处置闭环。

`fixtures/sample.json` 仅保存可公开的领域边界说明，不含真实个人资料或业务凭据。
