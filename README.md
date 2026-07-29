# 光网络智能运维仿真测试平台

本平台用于回放和分析光网络仿真实验数据，支撑故障定位、原因分析、知识库检索和诊断结果评估。当前数据来自 GNPy 相关仿真实验，平台负责导入、回放、分析、诊断和评估。

## 功能概览

- 实验数据注册：按批次和 episode 管理仿真实验。
- 状态感知：接入实验后自动扫描 EDFA、Fiber、ROADM telemetry，展示设备域状态和故障窗口前后的关键变化。
- 结构化事件摘要：读取 simulation events 和 service lifecycle，过滤注入类答案事件，仅保留诊断可见事件。
- 知识库检索：检索光通信物理规律、设备机理、指标解释和处置建议。
- 历史特征匹配：基于已标注历史样本构建故障特征库，用于相似故障召回。
- 自动诊断：系统感知完成后自动输出正常/异常状态、Top-N 候选根因、关键证据和处置建议。
- 结果评估：诊断完成后读取标注结果，展示命中情况和耗时。

## 设计对标

本轮界面与工作流没有照抄某一套商用产品，而是提取了贴近本项目的共同模式：

- [Huawei iMaster NCE-T Intelligent OTN O&M](https://carrier.huawei.com/kr/products/fixed-network/nce/NCE-T/intelligent-otn-om)：光线路健康、告警压缩、RCA、故障仿真和容量预测。
- [Nokia NSP Network Operations](https://documentation.nokia.com/nsp/25-8/NSP_System_Architecture_Guide/netfns.html)：网络健康总览、遥测基线、异常检测、服务保障和下钻分析。
- [Juniper Active Assurance](https://www.juniper.net/documentation/us/en/software/juniper-paragon-automation2.4.0/user-guide/topics/concept/active-assurance-overview.html)：通过主动测试持续验证端到端服务质量。
- [Grafana Alerting](https://grafana.com/docs/grafana/latest/alerting/monitor-status/)：告警总览、状态历史、通知状态和事件处置入口。

平台据此采用“全网态势 → 事件处置 → 评测治理 → 平台架构”的信息结构，并坚持“直接遥测证据优先、诊断后必须验证、数据可观测性与算法命中率分开统计”。

## 操作流程

平台按运维工作流分为四个工作区：

1. **全网态势**：查看实验资产、数据可用率、场景覆盖、诊断效果和待关注事项。
2. **事件处置**：选择实验并启动感知，查看遥测变化、事件、Top-3 根因、知识依据和处置建议。
3. **评测治理**：分类型查看 Hit@1、Hit@3、实体命中和注入效应可观测性。
4. **平台架构**：查看数据感知、知识模型、智能分析、交互闭环四层能力。

事件处置流程：

1. 在左侧“系统状态感知区”选择数据来源和实验批次。
2. 点击“接入实验并启动感知”。
3. 系统自动读取全部可用设备指标，识别异常设备域和重点对象，中间区域展示状态、曲线和事件流。
4. 状态感知完成后执行诊断，右侧展示 Top-3 候选根因、证据、知识依据和处置建议。
5. 诊断后生成验证任务，并在评测治理工作区观察整体效果。

设备域、设备标识和指标由系统自动选择，不要求操作人员预先知道故障所在位置或手工挑选故障指标。

## 目录结构

```text
platform/
├── app.py
├── start_platform.bat
├── adapters/
├── core/
├── configs/
├── diagnosis_rules/
├── data/
│   ├── knowledge/
│   ├── topology/
│   ├── vector_store/
│   └── cache/
├── scripts/
├── tests/
├── requirements.txt
└── README.md
```

## 启动方式

首次运行先安装依赖：

```powershell
python -m pip install -r requirements.txt
```

双击：

```text
<平台目录>\start_platform.bat
```

或在命令行启动：

```powershell
cd <平台目录>
python -m streamlit run app.py --server.address 0.0.0.0 --server.port 8501 --server.headless true
```

访问地址：

- 本机：`http://localhost:8501`
- 局域网：`http://<本机IP>:8501`

## 数据配置

大体量仿真数据不复制进平台目录。数据根目录通过 `configs/platform.yaml` 指定：

```yaml
data:
  external_data_root: D:\AA_MY_WORK\codex\系统仿真数据\databackup\databackup
  external_data_roots: D:\AA_MY_WORK\codex\系统仿真数据\databackup\databackup;D:\AA_MY_WORK\codex\新数据\databcakup3
  registry_file: data/dataset_registry.json
  knowledge_dir: data/knowledge
```

`external_data_root` 是旧注册表相对路径的兼容根目录；`external_data_roots` 用分号分隔多个数据根，注册表会保存每个 episode 的绝对路径。更换电脑时修改这两项，然后重新注册数据。

## 数据注册

```powershell
cd <平台目录>
python scripts\register_databackup.py
```

临时追加其他数据根，也可以显式执行：

```powershell
python scripts\register_databackup.py --data-root <旧数据根> --data-root <新数据根>
```

注册结果写入：

```text
data/dataset_registry.json
```

## 诊断引擎配置

引擎配置已从主工作台移出，保留在“高级设置与系统信息”中。私有配置仍使用：

- `接口地址`：兼容 chat/completions 的接口地址，例如阿里云百炼兼容地址，通常以 `/compatible-mode/v1` 结尾。
- `引擎代号`：默认使用 `qwen3.7-plus`，也可以改成控制台中已开通的其他引擎代号。
- `访问密钥`：输入后以星号隐藏，不写入代码仓库。

默认使用 `qwen3.7-plus`，响应超时为 120 秒。本机访问密钥保存在 `.streamlit/secrets.toml`，该文件已加入 `.gitignore`，不应上传到公开代码仓库。
结构化研判输出上限为 2000 tokens，以容纳三项事件指标说明并避免 JSON 被截断。

闭环回放默认调用在线 `qwen3.7-plus`，只有在线配置不可用或有限重试仍失败时才由本地诊断引擎接管。

## 知识库与历史特征库

当前平台使用两类辅助证据：

- 固定知识库：`data/knowledge/knowledge_chunks.jsonl`
- 历史故障特征库：`data/knowledge/fault_signature_library.json`

固定知识库用于检索物理规律、设备机理、指标解释和排障建议；历史故障特征库由训练样本的 telemetry 窗口特征生成，用于召回相似故障。知识库相关性分值和历史相似度只表示证据匹配程度，不是根因概率。

重建历史故障特征库：

```powershell
cd <平台目录>
python scripts\build_signature_library.py --train-per-type 50
```

## 测试与评估

运行单元测试：

```powershell
cd <平台目录>
python -m pytest -q -p no:cacheprovider
```

运行批量评估：

```powershell
cd <平台目录>
python scripts\evaluate_rule_batch.py --offset-per-type 50 --max-per-type 12
```

审计 EDFA 增益衰退注入是否真实反映到目标设备 telemetry：

```powershell
cd <平台目录>
python scripts\audit_edfa_gain_observability.py --output data\cache\edfa_gain_observability_full.json
```

最近一次新数据分层抽样评估结果（3类各30组）：

- 测试样本：90 组
- 故障类型 Hit@1：87.8%
- 故障类型 Hit@3：94.4%
- 实体 Hit@1：78.9%
- 设备级命中：87.8%
- 注入效应在标注实体可观测：90/90（100%）

评测结果文件：`data/cache/batch_eval_rerun_20260724.json`。

旧数据中的110组增益衰退专项曾出现注入效应不可观测问题。新重跑数据完整审计覆盖200组，
`actual_gain_db` 预期下降为200/200，结果保存在
`data/cache/edfa_gain_observability_rerun_20260724.json`。

2026-07-24 已接入 `新数据/databcakup3` 重跑包，共 600 组：`EDFA_GAIN_DEGRADATION`、`ROADM_INBAND_CROSSTALK`、`ROADM_WSS_FILTER_SHIFT` 各 200 组。新旧数据合并后共 1600 组、9 个批次，注册校验全部为 `VALID`。最新专项审计与评测结果以 `data/cache/*rerun_20260724.json` 为准。

## 当前优化重点

- 修复 EDFA 增益衰退的数据生成链路，确保 `drop_db` 实际作用于目标 EDFA，并反映到 `actual_gain_db` 及下游功率/质量指标。
- 区分 EDFA 噪声异常与 ROADM 带内串扰。
- 引入更完整的拓扑和业务路径证据，提高实体级定位准确率。
- 压缩在线诊断输入，提升响应速度和稳定性。

当前诊断输入会自动扫描 EDFA、Fiber、ROADM 三类设备的关键变化，不依赖页面图表中手工选择的指标。对沿链路累积或传播的 PMD、NLI、噪声和波纹特征，平台先确定异常链路，再定位最早出现直接特征的实体。历史特征匹配会排除当前测试 run，避免测试样本与自身比对造成虚高结果。标注类别默认隐藏，只在诊断完成后用于评估和注入效应可观测性检查。

## 光网络智能运维闭环

平台默认入口已经改为主动监测与自动研判：

1. 在“运维总览”点击“启动监测”；
2. 平台按网络时标推进监测场景；
3. 自动扫描EDFA、Fiber、ROADM关键指标；
4. 同一路径连续两个监测周期形成稳定异常后，自动聚合运维事件；
5. 自动关联活动业务，调用现有知识检索与在线诊断流程；
6. 依次完成智能研判、根因分析和结构化处置方案建议生成；
7. 通过进度条、完成提醒和处置工作台展示根因与建议，不宣称执行当前尚未接入的方案验证。

默认故障演示episode：

`experiment_batch_20260623_194654__episode_0119`

默认正常对照episode：

`experiment_batch_20260624_085020__episode_0001`

主导航为“运维总览、事件中心、处置工作台、系统配置”。历史批量评测结果不再作为运行态分页展示。
监测启动后，异常检测器先从当次事件的可观测遥测中产生候选指标；在线模型再根据变化幅度、
方向、时序与跨设备传播关系选择三项最有诊断价值的指标。平台只接受候选集中真实存在的实体与
字段，并回填对应历史曲线；模型输出无效或不足时，以当次事件的异常证据顺序补齐，不使用预置
故障模板，也不读取故障注入信息或复核答案。监测场景、知识库数量和
研判引擎配置集中在“系统配置”中。标注结果与故障注入字段不进入异常发现、知识查询或诊断输入，
只在诊断结束后用于内部评估。

界面默认使用亮色主题，系统配置可随时切换深色主题。表单占位提示会跟随主题自动调整对比度。
事件等级使用中文；拓扑异常路径、根因候选实体和处置目标中的站点名称全部保留英文。事件趋势图
根据当前数据范围增加动态纵轴留白，避免曲线贴近图表上下边缘。

研判策略默认使用在线 `qwen3.7-plus`。临时网络或服务错误会先重试一次，只有在线配置不可用、
认证失败或连续两次调用失败时才由本地诊断引擎接管。页面顶部和研判结果会显示实际模型、
调用次数、耗时及是否发生回退。

验收截图位于 `artifacts/closed_loop_demo/`。
