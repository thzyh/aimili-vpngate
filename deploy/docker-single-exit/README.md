# 本机 Docker 单出口隔离原型

该原型在 Windows Docker Desktop 的独立 Linux 网络命名空间中运行一条真实 AimiliVPN/OpenVPN 出口。它只发布本机回环端口，不修改 Windows 系统代理、默认路由或 v2rayN 配置，也不连接任何 VPS。

这是完整本机 Docker 方案的第一阶段数据面验证，不包含 Gateway、3x-ui/Xray、订阅和四出口协议事务。

## 固定本机入口

- HTTP/SOCKS5 代理：`127.0.0.1:17928`
- AimiliVPN 管理页：`127.0.0.1:18787` 加首次启动时生成的随机路径

两个端口都只绑定 Windows 回环地址，局域网和公网不能直接访问。容器内代理不要求账号密码，因为访问边界就是本机回环；不要把该端口改为 `0.0.0.0`。

## 启动

在 AimiliVPN 功能工作树运行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\docker-single-exit\start.ps1
```

脚本会先检查端口并记录脱敏宿主基线；Docker Engine 未运行时会启动 Docker Desktop。首次构建会在容器镜像中安装 OpenVPN、路由工具和证书，不修改 Windows 或全局 Python 环境。

## 自动验证

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\docker-single-exit\verify.ps1 -TimeoutSeconds 900
```

通过条件包括：

- 容器健康；
- `tun0` 存在；
- 恰好一个 OpenVPN 进程；
- 经 `127.0.0.1:17928` 的显式代理请求返回有效出口；
- 经 SOCKS5H 访问 v2rayN 当前延迟检测目标返回 HTTP 204；不满足时 AimiliVPN 会淘汰该免费节点并自动换节点；
- 代理出口与容器普通 `eth0` 出口不同；
- v2rayN PID、系统代理、Windows 默认路由和现用代理健康与启动前一致。

验证输出只包含布尔值和稳定状态，不输出 VPNGate 节点、出口 IP 或凭据。

## 查看本地管理入口

只有用户主动运行以下脚本时，终端才显示管理页 URL、用户名和密码：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\docker-single-exit\show-access.ps1
```

凭据保存在专用 Docker volume `aimili-single-exit-data`，不会写入 Git 工作树。不要把终端输出复制到公开日志、Issue 或提交记录。

## 用户手动验收

1. 运行 `show-access.ps1`，在浏览器打开给出的本机 URL，确认只显示一条主连接且状态正常。
2. 保持当前 v2rayN 代理不变，先用其他显式支持 HTTP/SOCKS5 的测试请求连接 `127.0.0.1:17928`。
3. 若要在 v2rayN 中验收，由用户手动新增一个 SOCKS5 节点：服务器 `127.0.0.1`、端口 `17928`、无认证。是否设为活动节点由用户决定，项目脚本不会操作 v2rayN。

## 停止和清理

停止容器但保留节点缓存与本地凭据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\docker-single-exit\stop.ps1
```

下次启动会复用相同的专用 volume 和管理凭据。

彻底删除容器、专用网络和原型数据：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\deploy\docker-single-exit\stop.ps1 -PurgeData
```

`-PurgeData` 会删除准确命名的 `aimili-single-exit-data` volume，其中的节点缓存和本地凭据无法恢复；不会删除其他 Docker volume。
