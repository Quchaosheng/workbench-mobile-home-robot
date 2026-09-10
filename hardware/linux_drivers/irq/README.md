# IRQ 软件契约

这是 LINUX6 的软件测试边界，不是目标 Linux 内核的真实 IRQ 驱动。它用 fake provider
验证共享 IRQ、上半部确认、下半部 work、停止/卸载同步和失败关闭语义，不绑定 IRQ
号码、设备寄存器、优先级策略或 PREEMPT_RT 行为。

## 生命周期

```text
register owner -> enable -> trigger/top-half -> bottom-half work
                               |
                         stop/close -> cancel/flush -> disabled/closed
```

- 非共享 IRQ 只能注册一个 owner；共享 IRQ 的每个触发必须来自已注册 owner。
- `trigger` 只确认并排队 work；真正处理由下半部显式执行。
- work 队列有固定容量，满时失败，不静默丢事件。
- `stop` 先禁止新触发，再等待活动 handler 结束并清理 pending work。
- 超过 deadline 的活动 handler 返回 `IRQHandlerTimeout`，provider 保持 STOPPING，
  不能假装已经完成卸载。
- `close` 复用 stop/flush 顺序，完成后才清除 owner 并进入 CLOSED。

## 硬件边界

真实驱动仍需硬件 Owner 确认 IRQ 号、共享关系、中断确认/清除寄存器、threaded IRQ
或 NAPI/workqueue 选择、优先级和设备树资源。fake provider 不能证明 IRQ jitter
小于 100 微秒、共享中断电气行为或 PREEMPT_RT 实时性。
