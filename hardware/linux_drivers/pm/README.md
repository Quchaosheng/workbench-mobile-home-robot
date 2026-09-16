# Linux 电源管理软件契约（LINUX8）

本目录验证驱动 suspend/resume 的软件生命周期，不是目标板的真实休眠、唤醒或功耗证据。

`FakePowerManager` 固化以下边界：

- 资源按注册顺序恢复，按逆序挂起；
- 挂起和恢复都有正的截止时间；
- 任一资源超时都会进入 `fault`，不自动假设恢复成功；
- 只有完整挂起后的 `suspended` 状态才能恢复；
- `fault`、`closed` 和进行中的过渡状态拒绝不安全操作。

资源 cost 是确定性的测试记账单位，不等同于真实毫秒延迟。真实 Linux PM 通知器、设备树、时钟、
regulator、runtime-PM、系统 suspend/resume 和唤醒功耗，必须在板卡、内核版本和硬件接口冻结后补充。
