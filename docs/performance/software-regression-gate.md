# 软件性能回归门禁

LINUX-APP11 使用同一环境中的基线报告和当前报告进行比较。门禁同时检查绝对预算
和相对退化，任何一项超限都返回非零退出码。它只接受本地软件报告，不接受或推断
真实硬件性能。

## 采集基线和当前报告

在同一主机、Python 版本和 Compose cache 模式下分别采集基线与当前结果：

```bash
python tools/scripts/benchmark_startup.py --output runs/performance/baseline/startup.json
python tools/scripts/benchmark_resources.py \
  --project workbench-startup-benchmark \
  --output runs/performance/baseline/resources.json
python tools/scripts/demo_scripted.py \
  --iterations 30 \
  --telemetry runs/performance/baseline/simulation.jsonl
python tools/scripts/analyze_telemetry.py \
  runs/performance/baseline/simulation.jsonl \
  --output runs/performance/baseline/telemetry.json
```

在代码变更后用相同命令写入 `runs/performance/current/`。资源和 telemetry 报告
至少需要 5 个样本；正式比较建议保留现有的 30 次 telemetry 运行。

## 执行门禁

```bash
python tools/scripts/performance_regression.py \
  --policy docs/performance/software-regression-policy-v1.json \
  --baseline-startup runs/performance/baseline/startup.json \
  --current-startup runs/performance/current/startup.json \
  --baseline-resources runs/performance/baseline/resources.json \
  --current-resources runs/performance/current/resources.json \
  --baseline-telemetry runs/performance/baseline/telemetry.json \
  --current-telemetry runs/performance/current/telemetry.json \
  --output runs/performance/regression.json
```

输出中的每项检查包含基线值、当前值、绝对上限和允许的相对退化上限。以下情况
会失败关闭：

- 报告缺失、schema 版本错误或包含 `NaN`/`Infinity`；
- 基线与当前的操作系统、Python 版本或启动 cache 模式不一致；
- 样本数量不足或 P50/P95/max 顺序错误；
- telemetry 含有 `hardware` 来源；
- 当前值超过绝对预算或相对退化上限。

策略中的 2 GiB 和 2 CPU 是任务书的软件容器预算，启动和流水线阈值是开发环境
门禁。结果始终标记为 `local_software`，并明确保留
`target_hardware_measurement: NOT_EXECUTED`。目标板、真实 ROS/Gazebo 和物理机器人
必须重新建立各自的可比基线，不能沿用本门禁作为发布证据。

## 预算范围与失败率

除启动、CPU 与内存外，门禁还覆盖事件日志字节数、单次运行磁盘增长、并发运行数、
API P95/P99 与失败率，以及遥测阶段失败率。遥测报告因此额外给出 P99 和单位
（`unit: "ms"`），并从记录本身统计失败：`level` 为 `ERROR`/`CRITICAL`，或事件属于
`stage_failed`、`fault`、`policy_violation`、`run_failed`、`task_failed`。同一条记录
即使同时命中级别与事件也只计一次，安静但未上报的流水线不会被当成通过。

每个报告都记录解析出的 git 提交号（`revision`）以及平台、Python 和机器。提交号与
环境分开保存，因此基线可比性仍只由平台、Python、机器和 cache 模式决定。

## 无基线的预算门禁

宿主机没有提交基线时可用 `--budgets-only`：只检查绝对预算，不声称任何相对退化。
缺失的报告会记入 `not_evaluated` 并把状态置为 `INCOMPLETE`（退出码 2），绝不会被当作
通过。计划任务使用 `docs/performance/software-budget-policy-scripted-v1.json`，它只
声明该主机能产出的遥测指标；容器启动、容器资源和 API 延迟在无 Docker 的情况下
明确不在范围内。

```bash
make performance-budget-check
```
