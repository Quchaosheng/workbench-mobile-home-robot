# Linux 驱动层

本目录是桌面机器人 Linux 内核驱动的归档入口。驱动位于 MCU 固件和用户空间
应用之间，向上只暴露 Linux 标准子系统接口，向下才绑定具体控制器、总线和
板级资源。

当前仓库已经有一个用于软件验证的 SocketCAN 内核模块：
[`kernel/wbcan`](../../kernel/wbcan/)。它是带故障注入面的虚拟 CAN 设备，服务
于固件和恢复路径测试，不代表已经完成某一款物理 CAN 控制器的量产驱动。
物理控制器型号、寄存器布局和 IRQ/DMA 资源由 PCB 与 MCU 接口确认后，才可以
在本目录增加对应的硬件模块。

面向发布的架构说明位于
[`docs/architecture/linux-drivers.md`](../../docs/architecture/linux-drivers.md)。

LINUX3 的软件 UART/SPI 帧契约和 fake transport 位于
[`uart_spi/README.md`](uart_spi/README.md)。该实现只验证有界编码、CRC、序号、背压、
重试和错误处理；真实控制器、pinmux、设备树、DMA 和 IRQ 参数仍由硬件负责人确认。

LINUX6 的 IRQ 生命周期和 fake provider 位于
[`irq/README.md`](irq/README.md)。它只验证共享线路、上半部/下半部、取消、flush 和
停止同步；真实 IRQ 资源和 jitter 证据仍由硬件 Owner 确认。

## 分层边界

```text
用户空间 / ROS 2
        |
        | AF_CAN、termios、spidev 或 GPIO character device
        v
Linux 子系统核心
        |
        +-- SocketCAN core -------------+
        |                                 |
        |                           net_device
        |                                 |
        +-- tty / SPI / GPIO ------------+
                                          |
                                   控制器驱动
                                   (MMIO / IRQ / DMA)
                                          |
                                    PCB 外设资源
```

驱动不得把硬件细节泄漏到应用层，也不得绕过内核子系统自行实现一套并行协议。
设备节点、网络接口、错误码和统计信息必须遵循对应内核子系统的既有语义。

## 生命周期契约

每个硬件模块都应按以下顺序实现和审查：

1. `probe`：验证设备树/ACPI 资源、时钟、复位和中断；任何资源缺失都失败关闭。
2. `register`：CAN 驱动完成 `alloc_candev` 后调用 `register_candev`；TTY 和 SPI
   驱动分别调用 `tty_register_driver` 和 `spi_register_driver` 等标准注册 API，
   注册成功前不对外发布可用设备。
3. `open`：清空硬件状态、建立 RX/TX 队列并启用 IRQ；失败时按反向顺序回滚。
4. `transfer`：上半部只确认并屏蔽必要的中断，下半部或 NAPI 完成收包、DMA 回收
   和统计更新；不可在 IRQ 上下文中睡眠。
5. `stop`：停止新传输、排空或取消 DMA、屏蔽 IRQ；等待正在执行的 IRQ handler、
   NAPI 和 work 完成后，再释放队列和状态。
6. `remove`：先撤销用户可见接口，执行与 `stop` 相同的同步和排空路径；确认没有
   IRQ、NAPI 或 work 正在运行后，再释放映射、时钟、DMA 和中断资源。

`probe`、`open` 和 `remove` 的错误路径必须是幂等的。半初始化状态不得留下网络
接口、字符设备或可继续访问的 MMIO 映射。

## 并发与数据所有权

- 配置状态使用 mutex；IRQ、TX 完成和 RX/NAPI 共享的短状态使用 spinlock。
- DMA 描述符和缓冲区由驱动拥有，提交前执行 `dma_map_*`，回收后执行对应的
  `dma_unmap_*`；不得把 DMA 地址当作 CPU 指针。
- RX 缓冲区在交给网络栈前完成 cache 同步，网络栈释放后才能重新入队。
- TX 队列只有在硬件确认可用时唤醒；队列满返回 `NETDEV_TX_BUSY`，不能静默丢帧。
- 中断统计、错误计数和最后一次恢复原因必须使用内核原子/锁保护，并可通过
  `ethtool -S` 或子系统标准统计读取。

## CAN 驱动映射

任务书中的 LINUX2 以 SocketCAN 为验收边界：

| 能力 | 内核边界 | 当前状态 |
|---|---|---|
| 标准帧与扩展帧 | `struct can_frame`、`CAN_EFF_FLAG` | `wbcan` 已覆盖测试语义 |
| CAN FD | `struct canfd_frame`、`CAN_CTRLMODE_FD` | 等待控制器型号和线速确认 |
| 过滤 | CAN core/socket filter | 由 SocketCAN 统一处理 |
| 错误统计 | `can_priv.can_stats`、错误帧 | `wbcan` 提供可重复的故障测试 |
| 延迟目标 | TX 提交到 RX 时间戳 | 必须在目标硬件上测量，不能用虚拟模块代替 |

`wbcan` 的 debugfs 故障注入只用于测试，不是生产控制 ABI。物理 CAN 驱动不得
复用该 debugfs 接口作为设备配置面；生产配置应走 SocketCAN、设备树和标准
网络工具。

## IRQ、DMA 与恢复策略

LINUX5/LINUX6 的实现必须在硬件资源确认后补充以下内容：

- IRQ：确认共享中断标志后再清中断；上半部只做确认和调度，下半部批量处理。
- DMA：预分配固定大小的环形描述符，限制单次批量，错误时停止队列并回收所有
  在途缓冲区；恢复成功后才重新唤醒队列。
- 总线恢复：CAN bus-off 通过 CAN core 的 restart 语义暴露，不能自行伪造用户空间
  状态；恢复失败必须保留错误计数并保持设备不可发送。
- 可观测性：至少提供标准网络统计、错误计数、IRQ/DMA 回收计数和最近一次恢复
  原因，便于性能与故障证据注册。

## 开发与验收

在没有目标内核头文件或真实控制器之前，只能运行静态文档检查和 `wbcan` 的
软件测试。物理驱动 PR 还必须附带：

1. 目标内核版本、设备树/ACPI 资源和控制器数据手册版本；
2. `make -C <module>`, `checkpatch.pl` 和模块加载/卸载输出；
3. SocketCAN 收发、错误恢复、并发卸载以及 DMA/IRQ 计数证据；
4. 延迟、吞吐和 jitter 的原始采样，而不是规划值。

本次 LINUX1 PR 只建立上述架构和验收边界。LINUX2 的具体硬件实现、LINUX5 的
DMA 优化和 LINUX6 的中断优化在 PCB/MCU 接口确认前保持未实现状态。
