# DMA 软件契约

这是 LINUX5 的软件验证边界。`FakeDMAProvider` 模拟 Linux DMA 驱动必须遵守的
缓冲区所有权和描述符生命周期，但不绑定真实 DMA 控制器、通道、寄存器、cache
属性或设备树资源。

## 所有权和生命周期

```text
allocate -> CPU-owned -> submit -> DMA-owned -> complete/cancel -> CPU-owned -> recycle -> FREE
```

- 缓冲区预分配且容量有界；写入只能发生在 CPU-owned 状态。
- `submit` 不复制 payload，描述符直接引用预分配 buffer，模拟零拷贝提交。
- DMA-owned buffer 不允许 CPU 读写；完成或取消后才归还 CPU。
- 描述符环固定容量；满时显式返回 backpressure，不静默覆盖在途工作。
- `DMAStatus.ERROR` 会停止引擎；必须清理在途描述符后才能 `recover`。
- 关闭会取消在途描述符、清空可见队列并拒绝后续访问。

## 硬件接入前置条件

真实驱动还需要硬件负责人确认 DMA 控制器、通道、descriptor layout、cache 一致性、
中断完成信号、最大批量、错误恢复和设备树资源。当前模型不能证明链式 DMA、物理
零拷贝、`>50 MB/s` 吞吐或目标板稳定性；这些证据保持 `NOT_EXECUTED`。

## 测试

```bash
make dma-test
```

测试覆盖所有权转移、固定描述符容量、完成/取消、错误停机、恢复、关闭和外部 buffer
拒绝。
