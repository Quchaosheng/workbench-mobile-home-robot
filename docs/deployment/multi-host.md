# 分机部署

E13 的边界是把控制端的只读投影与仿真事件源分开。两台机器只需要互相访问一个 HTTP 地址；控制端不挂载仿真数据目录，也不获得机器人控制权限。

## 仿真机

```bash
export WORKBENCH_IMAGE=ghcr.io/quchaosheng/workbench-desk-robot:v2.0.0
docker compose -f deploy/multi-host/compose.sim.yaml up -d
curl --fail http://127.0.0.1:8090/readyz
```

`SIM_BIND_ADDRESS` 默认是 `127.0.0.1`，因此仿真事件源的端口默认只发布到本机回环接口；同机烟测直接使用这个默认值。
不要把它设置为 `0.0.0.0`、`::`、主机名、公网地址、链路本地地址或组播地址；Backend 会在提供请求前拒绝这些配置，
Compose 渲染同样会失败。

仿真事件源是**未认证**的只读 API。跨主机访问必须显式设置一个字面私网地址，并同时满足下面两条之一：

```bash
export SIM_BIND_ADDRESS=10.20.30.40
export WORKBENCH_SIM_TRUST_MODE=reverse_proxy
export WORKBENCH_SIM_TRUSTED_SOURCE_ALLOWLIST=10.20.30.50/32
docker compose -f deploy/multi-host/compose.sim.yaml up -d
```

- 由宿主机防火墙把 `8090` 限制到控制机的实际网段；或
- 经过一个终止 TLS、执行用户认证并限制来源网络的反向代理，且把反向代理的实际 TCP 来源写入
  `WORKBENCH_SIM_TRUSTED_SOURCE_ALLOWLIST`（只接受回环、RFC1918 或 IPv6 ULA 的字面 IP/CIDR）。

`WORKBENCH_SIM_TRUST_MODE` 的取值与 controller 相同：`local`（默认，只允许回环来源）或 `reverse_proxy`
（必须同时给出 allow-list）。非回环发布配 `local` 会在启动前失败，而不是静默接受所有来源。

示例中的 `10.20.30.40` 应替换为仿真机的实际地址；`curl` 的目标地址必须是实际发布地址，不能用 `0.0.0.0`
作为客户端目标。

## 控制机

```bash
export WORKBENCH_IMAGE=ghcr.io/quchaosheng/workbench-desk-robot:v2.0.0
export WORKBENCH_EVENT_SOURCE_URL=http://10.20.30.40:8090
export WORKBENCH_EVENT_SOURCE_ALLOWLIST=10.20.30.40/32
docker compose -f deploy/multi-host/compose.controller.yaml up -d
curl --fail http://127.0.0.1:8080/readyz
```

`CONTROLLER_BIND_ADDRESS` 默认是 `127.0.0.1`，因此控制端 HTTP 端口默认只发布到本机回环接口。不要把它设置为
`0.0.0.0`、`::`、主机名、公网地址、链路本地地址或组播地址；Backend 会在提供请求前拒绝这些配置。

## 非本地 HTTP 信任边界

非本地开放还必须等待 Issue #65 的远端事件严格校验完成；#65 完成前，controller 必须保持默认的
`127.0.0.1` 回环发布，下面的私网发布配置只能作为后续部署参考，不能用于生产开放。

非本地访问必须经过反向代理。反向代理负责终止 TLS、执行用户认证并限制客户端来源网络；Workbench Backend
继续保持只读，不保存账号、密码或 TLS 私钥。推荐让反向代理与 controller 位于同一台主机，并让它通过回环地址访问
controller：

```bash
export CONTROLLER_BIND_ADDRESS=127.0.0.1
export WORKBENCH_CONTROLLER_TRUST_MODE=local
unset WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST
docker compose -f deploy/multi-host/compose.controller.yaml up -d
```

此时安全边界是宿主机的回环发布：只有同机进程能连接 controller，外部客户端只能先经过反向代理。Docker
端口转发后，Backend 看到的 socket peer 通常是 bridge gateway，而不是 `127.0.0.1`，因此不要把
`127.0.0.1/32` 当作容器内看到的代理来源。若必须在这个拓扑中启用 `reverse_proxy` 模式，应先从 Backend
访问日志确认实际 peer，再配置对应的最小私网 IP/CIDR。

若反向代理必须从另一台受控私网主机连接，可将 `CONTROLLER_BIND_ADDRESS` 明确设置为控制机的 RFC1918 IPv4
地址或 IPv6 ULA 地址，并把 `WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST` 设置为代理实际 TCP 来源地址的最小
IP/CIDR。例如代理为 `10.20.30.10`、控制机为 `10.20.30.40`：

```bash
export CONTROLLER_BIND_ADDRESS=10.20.30.40
export WORKBENCH_CONTROLLER_TRUST_MODE=reverse_proxy
export WORKBENCH_CONTROLLER_TRUSTED_PROXY_ALLOWLIST=10.20.30.10/32,127.0.0.1/32
docker compose -f deploy/multi-host/compose.controller.yaml up -d
```

这里的 `10.20.30.10/32` 是反向代理实际 peer，`127.0.0.1/32` 只供 Compose 容器自身的健康检查访问
`/readyz`。两者都必须显式列入；不要重新为 health/readiness 增加绕过 allowlist 的隐式信任。

allowlist 只接受回环、RFC1918 或 IPv6 ULA 的字面 IP/CIDR，不能使用主机名、`0.0.0.0/0` 或 `::/0`。
Backend 根据实际 TCP peer 判断代理身份，故意忽略 `X-Forwarded-For` 和 `Forwarded`；反向代理不得依靠伪造这些
Header 绕过来源限制。Docker 或代理网络变化后必须重新核对 Backend 日志中的实际 peer，再更新最小 allowlist。

示例配置均为非敏感地址。环境变量、命令行和本文档不得包含密码、令牌或其他凭据；凭据由反向代理的秘密管理机制
提供。非本地部署在 TLS、认证和来源网络限制全部生效前不得开放。

`/healthz` 和 `/readyz` 是状态受限的运维路径，`/api/v1/**` 是只读数据路径。反向代理不得向非运维客户端公开
`/healthz` 或 `/readyz`。Backend 不提供写入或控制端点；`POST`、`PUT`、`PATCH` 和 `DELETE` 返回 `405 read_only`。
请求体上限为 1 MiB，JSON 响应上限为 4 MiB，同时执行的请求最多为 16；达到并发上限时返回
`503 server_busy`。

控制机必须同时设置 `WORKBENCH_EVENT_SOURCE_URL` 和 `WORKBENCH_EVENT_SOURCE_ALLOWLIST`。后者是以逗号分隔的字面 IP 地址或 CIDR 网段列表，例如 `10.20.30.40/32,10.20.31.0/24`；allow-list 条目不能使用主机名。若事件源 URL 使用主机名，则它解析出的每个地址都必须落在这些网段内。缺失、空白或格式错误的 allow-list 会保持非回环事件源为未就绪状态。

示例中的 `10.20.30.40` 应替换为仿真机的实际地址。URL 和 allow-list 都是非敏感部署配置；不要在 URL 中放入用户名、密码、令牌或其他凭据。

控制端的 `/readyz` 会检查仿真端；仿真端宕机、返回错误或事件源不合法时，控制端明确返回 `503 not_ready`。两个 Compose 项目没有共享卷，便于在不同主机运行。

## 同机烟测

```bash
docker compose -f deploy/multi-host/compose.sim.yaml up -d
$env:WORKBENCH_EVENT_SOURCE_URL = "http://host.docker.internal:8090"
$env:WORKBENCH_EVENT_SOURCE_ALLOWLIST = "192.168.65.0/24"
docker compose -f deploy/multi-host/compose.controller.yaml up -d
curl --fail http://127.0.0.1:8080/readyz
```

同机示例面向 Docker Desktop；运行前应确认 `host.docker.internal` 解析到的字面 IP，并把 `192.168.65.0/24` 替换成覆盖该地址的最小 IP 或 CIDR。这个烟测只证明网络边界和故障传播；它不替代两台物理机器的网络延迟、丢包和时钟同步验收。
