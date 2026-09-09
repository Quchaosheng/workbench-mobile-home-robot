# GPIO 软件契约

这是 LINUX4 的软件测试边界，使用 Linux GPIO character-device 的语义建模，但不
绑定真实 GPIO 控制器、line number、设备树节点、pinmux 或安全电路。生产代码应
通过 `libgpiod`/内核 GPIO character device 访问线路，不新增 sysfs 私有 ABI。

## 契约

- 每条线有唯一逻辑名称、输入/输出方向、有效电平、边沿和去抖时间。
- 输出初始化为非激活值；输入在首次观测前是 unknown，不能被当作安全或许可状态。
- 输入时间戳必须严格递增；回退、重复或非法时间戳失败关闭。
- 边沿事件进入固定容量队列；队列满返回 backpressure，不能静默丢弃。
- 关闭 provider 后，所有读写和事件读取都失败；挂起事件被清空。
- fake provider 用 `RLock` 保护配置、状态和事件序号，模拟单写者状态边界。

## 安全边界

`FakeGPIOProvider` 只验证软件行为。它不控制电机使能、E-stop、安全继电器或
watchdog，也不能证明 GPIO 电平、电气时序、抗干扰或硬件安全回路。真实 GPIO
编号、极性、边沿、中断和权限必须由 PCB、MCU、安全 Owner 在设备树和实机 bring-up
中确认。

## 测试

```bash
make gpio-test
```

测试覆盖默认安全态、unknown 输入、边沿和去抖、序号和时间戳、队列背压、关闭
语义、权限隔离和非法配置。硬件验证保持 `NOT_EXECUTED`。
