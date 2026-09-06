# Workbench Desk Robot

> **先验证，再说完成。**
>
> 面向移动家务机器人的证据优先基础：受限动作、可回放事件，以及能够明确说出
> **已确认、未满足、证据不足** 的验证器。

[![Python](https://img.shields.io/badge/Python-3.12-3776AB?logo=python&logoColor=white)](pyproject.toml)
[![License](https://img.shields.io/badge/license-Apache--2.0-2ea44f)](LICENSE)
[![Release](https://img.shields.io/github/v/release/Quchaosheng/workbench-desk-robot?display_name=tag)](https://github.com/Quchaosheng/workbench-desk-robot/releases/latest)
[![CI](https://github.com/Quchaosheng/workbench-desk-robot/actions/workflows/ci.yml/badge.svg)](https://github.com/Quchaosheng/workbench-desk-robot/actions)

![VORA 家务机器人](docs/assets/workbench-home-robot-market-v18.png)

<p align="center"><img src="docs/assets/vora-logo.svg" alt="VORA" width="360"></p>

**产品名：** VORA Home Robot  ·  **工程仓库 / 运行时：** Workbench Desk Robot（`workbench-desk-robot`）

VORA 是面向用户的产品品牌；Workbench 继续作为工程仓库和证据优先运行时的名称。
标志由开放角度和偏心轨道构成，保持识别度，同时不把品牌绑定在某一种机器人用途上。

[English](README.md) · [可旋转的 3D 产品视图](docs/assets/premium-product-render.html)

## 为什么做 Workbench？

很多机器人 Demo 把“命令已接受”当成“任务已完成”。Workbench 把证据放回主链路：

```text
目标 -> 受限规划器 -> 语义动作 -> 受信执行器
                         \-> 事件库 -> 验证器 -> 回放/看板
```

| 层 | 职责 |
| --- | --- |
| 意图 | 从小而严格的动作词表中选择 |
| 执行 | 由受信运行时下发并记录 |
| 验证 | 检查动作后的证据，不猜成功 |
| 回放 | 从追加式事件流重建状态 |

## 3 分钟开始

要求：Python 3.12。离线运行时不需要 GPU。

```bash
git clone https://github.com/Quchaosheng/workbench-desk-robot.git
cd workbench-desk-robot
python -m pip install -e ".[dev]"
python tools/scripts/sim_cli.py doctor
python tools/scripts/sim_cli.py run normal-001 --runner scripted --output-dir runs/demo
```

运行完整的可移植检查：

```bash
python -m pytest -q
python -m ruff check .
```

脚本 runner 会生成包含 manifest、场景、事件、日志、metadata 和 SHA-256 checksum 的可回放 artifact，并明确标记为 `SCRIPTED_FIXTURE`。

## 当前包含

- `confirmed`、`refuted`、`insufficient_evidence` 三态验证。
- 严格 JSON schema 与对应的 Pydantic 契约。
- 带完整性校验回放的追加式 SQLite 事件库。
- 受限语义工具的失败关闭式策略校验。
- 只读 dashboard 与确定性仿真 fixture。
- MCU、CAN、Motion、BSP 的软件基础边界。

## 可选：OmniLink 知识层

[OmniLink AI](https://github.com/vivekmaru/omnilink-ai) 是独立运行的知识服务，用于检索维修笔记、ADR、Issue 和 Workbench 运行摘要。它不参与规划、执行、验证或机器人控制。

```bash
git clone https://github.com/vivekmaru/omnilink-ai.git
cd omnilink-ai
npm install
npm run dev                 # 通常监听 http://127.0.0.1:3000
```

Workbench 通过 [`integrations/omnilink/`](integrations/omnilink/) 中的标准库适配器访问：

```python
from integrations.omnilink import OmniLinkClient

client = OmniLinkClient("http://127.0.0.1:3000")
results = client.search("gripper calibration")
answer = client.ask("Which calibration notes mention the gripper?")
```

只能导出有界的运行摘要。原始 JSONL、`TaskGraph`、`SemanticAction`、动作结果、相机数据和安全状态始终留在 Workbench。捕获 `OmniLinkError` 可保证知识服务不可用时离线流程继续。部署和安全要求见[集成说明](integrations/omnilink/README.md)。

## 诚实状态

离线运行时和软件边界已有测试。端到端 Gazebo 世界、真实感知、语义运动执行和物理硬件证据尚不构成发布承诺。当前证据边界见 [`docs/architecture/`](docs/architecture/) 和 [`sim/README.md`](sim/README.md)。

## 文档

- [用户指南](docs/user-guide/index.md)
- [产品证据层](docs/product/README.md)
- [系统架构](docs/architecture/system.md)
- [仿真边界](sim/README.md)
- [Motion 基础](robot/control/README.md)
- [安全 MCU](firmware/mcu/README.md)
- [机器人 BSP](bsp/README.md)
- [多主机部署](docs/deployment/multi-host.md)
- [安全策略](SECURITY.md)

## 许可证

Apache-2.0，详见 [LICENSE](LICENSE)。
