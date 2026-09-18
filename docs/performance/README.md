# 性能与证据基线

本目录的报告必须由脚本从原始数据生成。所有耗时以阶段事件的 `duration_ms` 记录，汇总同时给出 P50、P95、P99 和最大值，并显式标注单位；不要把任务执行耗时当成容器启动耗时。每份报告同时记录解析出的 git 提交号（`revision`），使结果可以追溯到具体代码。

## 端到端阶段

```bash
make performance-test
```

命令生成：

- `runs/performance/simulation.jsonl`：30 次仿真形状的原始统一日志；
- `runs/performance/stages.json`：每阶段 P50/P95 汇总。

仿真日志的 `source=simulation` 只能证明软件流水线，不具备真机发布资格。

## 启动阶段

```bash
python tools/scripts/benchmark_startup.py --output runs/performance/startup.json
```

报告分别记录镜像构建、容器启动到 `/healthz`、启动到 `/readyz` 以及完整 clone-to-ready 时间。`--no-cache` 用于冷缓存测量；默认的标准缓存结果不能替代外部冷启动测试。

## CPU 与内存

容器启动后运行：

```bash
python tools/scripts/benchmark_resources.py \
  --project workbench-startup-benchmark \
  --output runs/performance/resources.json
```

该脚本使用 `docker stats` 采集每个服务容器的 CPU 百分比和 RSS 近似值，并保存机器、Python、采样次数和间隔。报告还记录事件日志总字节数、单次运行的磁盘增长、并发运行数，以及只读 API 的 P50/P95/P99 延迟和失败率，因此事件日志大小、并发、API 延迟、内存、CPU 与磁盘增长都有可比较的预算输入。`--data-dir` 指定统计事件日志的目录（默认 `runs`）。

## 真机日志

真机服务也使用同一 JSONL 字段（`source=hardware`、`run_id`、`sequence_no`、`details.stage`、`details.duration_ms`）。分析前先绑定操作员和硬件编号：

```bash
python tools/scripts/register_hardware_evidence.py runs/hardware/run-001.jsonl \
  --hardware-id arm-01 --operator name \
  --output runs/hardware/run-001.evidence.json
python tools/scripts/analyze_telemetry.py runs/hardware/run-001.jsonl \
  --hardware-evidence runs/hardware/run-001.evidence.json \
  --output runs/hardware/run-001.report.json
```

没有哈希匹配的操作员证明，分析器会拒绝 `source=hardware`，因此测试夹具不能被悄悄当作真实硬件证据。

## 软件性能回归

同环境基线比较、绝对预算和失败关闭规则见
[`software-regression-gate.md`](software-regression-gate.md)。该门禁只适用于开发主机的
软件性能，不替代目标板或物理机器人测量。

没有提交基线的宿主机（例如计划任务 runner）运行 `make performance-budget-check`：
它只检查绝对预算，未提供的报告记为 `not_evaluated` 并返回 `INCOMPLETE`，不会伪装成通过。
