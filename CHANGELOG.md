# 更新日志 (Changelog)

本文件记录 **AimiliVPN 多出口增强版（二开）** 相对上游原项目 [baoweise-bot/aimili-vpngate](https://github.com/baoweise-bot/aimili-vpngate) 的改动。
格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)。

---

## [Custom 1.1.0] - 2026-08-24

### 新增 (Added)

- **有效节点池自动补充**：既有有效节点优先复验，失败节点立即移出可见列表并进入冷却；有效数量不足时继续向 VPNGate API 列表后部按批次补测，达到目标或候选耗尽后停止。
- 新增 `TARGET_VALID_POOL_SIZE`、`MAX_FETCH_ROWS`、`NODE_TEST_BATCH_SIZE` 和 `PROBE_FAILURE_COOLDOWN_SECONDS` 环境变量；兼容读取旧的 `TARGET_VALID_NODES` 和 `MAX_SCAN_ROWS`。
- 新增纯标准库自动化测试，覆盖候选优先级、失败冷却、列表后部补测、候选耗尽停止、API 失败保留现有节点池，以及探测与持久化隔离。

### 变更 (Changed)

- `nodes.json` 只保存已验证有效节点和当前活动节点，不再长期展示 `unavailable` 节点。
- OpenVPN 精验改为小批次执行，低内存 VPS 可以维持较大的候选读取范围而不同时启动大量探测。

## [Fork 1.0.1] - 2026-06-14

主连接(7928)可靠性修复：根治节点自动切换后下游 3x-ui/Xray 需手动重启才恢复的问题。

### 修复 (Fixed)
- **主连接(7928)切换后强制下游重连 + 假活节点冷却（根治"需重启 Xray"）**：主连接自动漂移到新节点后，下游 3x-ui/Xray 会复用切换前已黑洞化的连接/DNS，导致以主连接为出口 IP 的 3x-ui 节点协议出站断网，必须手动重启 xray 才恢复；且原主连接单次出口失败即切换、无坏节点冷却，易反复切到"握手成功但不转发"的假活节点。本次将多出口已验证的加固逻辑对齐到主连接：
  - `proxy_server`：新增 `ConnRegistry` 跟踪主代理(7928)下游连接并支持一次性强制断开（先 `shutdown(SHUT_RDWR)` 唤醒阻塞在 select 的 relay 线程再 `close`）；新增 `purge_dns_cache` 按设备清隧道 DNS 缓存；`start_proxy_server` 增加可选 `registry` 参数（多出口不传，零影响，向后兼容）。
  - `vpngate_manager`：主代理启动带 `registry`；`connect_node` 出口验证通过后调用 `reset_main_proxy_connections` 强制下游重连新隧道并清 `tun0` DNS，根治"切换后需重启 Xray"。
  - **主连接对齐多出口加固**：`background_proxy_checker` 出口连续失败达 `MAIN_EGRESS_FAIL_THRESHOLD`（默认 2）次才切换，切前 `mark_main_bad_node` 将坏节点加入冷却（`MAIN_BAD_NODE_COOLDOWN` 默认 600s）；`auto_switch_node` 排除冷却期内的坏节点，避免切回"假活不转发"节点。
- 新增可调环境变量：`MAIN_EGRESS_FAIL_THRESHOLD`、`MAIN_BAD_NODE_COOLDOWN`。

> 验证：已 `py_compile` + 静态检查；端到端需在 Linux VPS 上实测自动切换后 3x-ui 出站自恢复。

## [Fork 1.0.0] - 2026-06-12

首个二次开发版本，在保留上游全部能力的基础上新增多出口能力并做了性能/可靠性优化。

### 新增 (Added)
- **多出口住宅 IP（Multi-Exit）**：单机同时维持 N 条相互隔离的隧道，每条 = 独立 `tunN` + 独立策略路由表（`200+i`）+ 独立本地代理端口（`17928+i`），连接不同住宅节点。
  - 后台「管理员 → 多出口住宅 IP」面板：实时设置出口数量、国家过滤（如 `JP,KR`）、仅住宅 IP 开关，并展示每个槽位的状态 / IP / 国家 / 端口。
  - **per-slot 自动漂移**：某槽位节点掉线时自动从健康住宅节点池补齐。
  - **逐槽位地区过滤**：可给每个槽位单独设定地区（如槽位0=KR、槽位1=JP），留空跟随全局；API `POST /api/set_slot_country`。
  - **逐槽位运营商(ISP)过滤**：全局多出口可设 ISP 默认值，每个槽位还可单独覆盖运营商关键字（匹配 owner/as_name/asn）；供给器自动补齐与手动换 IP 均按「本槽地区 + 本槽运营商」选节点，留空跟随全局。API `POST /api/set_slot_isp`，`POST /api/update_exit_slots` 增加 `isp` 字段，`select_slot_nodes` 增加 isp 过滤。
  - **手动换 IP**：每个槽位可一键重摇到同地区的另一住宅节点，应对不同运营商 IP 质量差异；API `POST /api/switch_exit_slot`。
  - **节点列表直接指派 + 锁定**：主节点列表「操作」列新增「多出口▾」，可把指定 IP/运营商节点「切换到槽位 #N」或「新增槽位用此 IP」；被指派节点会锁定(pin)且行内显示 `出口#N` 角标；供给器优先使用锁定节点，失效时临时回退保连通。API `POST /api/assign_slot_node`、`POST /api/add_slot_with_node`。
  - **完整生命周期管理**：每个槽位支持停止/启动/删除，面板可新增空槽位；槽位模型由「数量」改为显式「启用索引列表 + 暂停集合」（`exit_slot_active` / `exit_slot_paused`），删除中间槽位不重排其余端口/索引，已配置的 3x-ui outbound 不错位。API `POST /api/stop_slot`、`/api/start_slot`、`/api/delete_slot`、`/api/add_slot`。
  - **集成到主界面**：多出口面板由独立弹窗改为主界面内联面板，工具栏新增「多出口住宅IP」按钮一键展开（管理员菜单入口保留）。
  - **一键导出 3x-ui / Xray 出站配置**（`outbounds` + `routing.rules` 模板）。
  - 新增 API：`GET /api/exit_slots`、`POST /api/update_exit_slots`、`GET /api/exit_slots/3xui`。
  - 槽位状态持久化到 `slots.json`。
- **真机自检脚本** `scripts/selfcheck_multiexit.sh`：逐个出口槽位核对 tun 设备、策略路由表/规则、本地代理端口监听，并经各槽位代理实测真实出口 IP，输出 PASS/FAIL 汇总。
- 新增可调环境变量：`MAX_EXIT_SLOTS`、`MULTI_EXIT_SLOTS`、`SLOT_PORT_BASE`、`SLOT_DEV_BASE`、`SLOT_TABLE_BASE`、`SLOT_PROXY_HOST`、`EXIT_SLOTS_CHECK_INTERVAL`、`OPENVPN_TEST_CONCURRENCY`、`TCP_PRESCREEN_CONCURRENCY`、`OPENVPN_TUN_DNS`、`LOCAL_PROXY_DNS_TTL`、`LOCAL_PROXY_RELAY_TIMEOUT` 等。
- `.gitattributes`：统一 LF 行尾，避免 Windows 编辑给 bash/python 脚本引入 CRLF。

### 优化 (Changed)
- **主连接(7928)运营商(ISP)过滤**：路由设置新增「运营商(ISP)过滤」（`routing_isp`，逗号分隔关键字，匹配节点 owner/as_name/asn）。开启后主连接自动漂移只切换到匹配 ISP 的节点，配合「固定地区」即可实现「只切某地区某运营商的纯净住宅 IP」。在「管理员 → 代理设置」中配置。
- **分层并发测速**：`test_multiple_nodes` 先用高并发 TCP 连通性粗筛淘汰明确不可达的 TCP 协议节点，再对存活节点做完整 OpenVPN 精验；UDP/未知节点不参与粗筛以避免误杀。OpenVPN 精验并发数由写死的 5 改为可配置（默认 8）。
- **隧道内 DNS**：`resolve_dns_over_tun0` 加入 TTL 缓存与多上游 DNS 竞速（默认 `8.8.8.8,1.1.1.1`），消除每连接重复解析与单一 DNS 不可达时的干等超时。
- **本地代理转发**：`relay` 重写为非阻塞双向泵（带背压与半关闭传播），修复原「只 select 可读 + 阻塞 sendall」可能导致的半双工背压卡死，空闲超时可配置。
- `proxy_server` 出站设备参数化（`tun0` → `device`），代理网关支持 `stop_event` 优雅停止；策略路由 `setup/cleanup_policy_routing` 参数化（接口 + 路由表号）。以上改动默认值保持与原行为一致，**向后兼容**。

### 修复 (Fixed)
- **多出口槽位出口健康检测 + 自动漂移（重要）**：此前槽位仅判断 OpenVPN 进程是否存活，不验证节点是否真转发流量——遇到"握手成功但不转发"的假活节点（VPNGate 免费/住宅节点常见）会一直占着槽位且出口不通。新增 `slot_egress_checker_loop`：周期经各槽位本地 socks 端口实测出口（curl），连续失败则把该节点加入冷却名单(`slot_bad_nodes`)并自动漂移到其他能转发的节点；锁定(pin)的槽位只提示不强切。`slots.json`/面板新增 `exit_ip`/`egress_ok`。可调 `SLOT_EGRESS_CHECK_INTERVAL`/`SLOT_EGRESS_FAIL_THRESHOLD`/`SLOT_BAD_NODE_COOLDOWN`。
- **安全**：多出口代理原先复用 `LOCAL_PROXY_HOST`，当主代理对公网开放（`::`）时会连带把所有住宅出口端口暴露公网且默认无鉴权。新增 `SLOT_PROXY_HOST`（默认 `127.0.0.1`）与主代理解耦，槽位代理默认仅绑回环。
- 多出口供给器并发重入：`supervise_exit_slots_once` 加非阻塞互斥锁，避免周期线程与 API 触发线程同时对同一槽位重复拨号、累加 `ip rule`。
- 进程重启遗留孤儿隧道：启动时 `kill_slot_openvpn_processes` 回收带 `AIMILI_SLOT` 标记的旧槽位 OpenVPN 进程并清理残留路由表，避免新供给器无法在 `tunN` 上重新拨号。
- 主连接清理不再误杀多出口隧道：`kill_existing_openvpn_processes` 跳过带 `AIMILI_SLOT` 标记的进程；槽位拨号 `report_status=False`，不污染主连接的 UI 状态显示。
- `ml` 命令行：`check_openvpn_process` 跳过 `AIMILI_SLOT` 隧道使「连接核心」只反映主连接，并新增「多出口住宅IP」状态行。

### 文档 (Docs)
- README 重写为本二开项目的独立文档（中英双语），突出多出口与 3x-ui 集成，含核心特性表、环境变量表、架构示意图与 3x-ui 配置示例。
- 安装脚本 `install.sh` 默认仓库地址切换为本二开仓库 `Guli-Joy/aimili-vpngate`（保留命令行参数覆盖能力）。
- 安装脚本更新逻辑：对已存在的安装目录，自动将 `git remote origin` 切换为本次安装目标仓库，支持从上游/旧仓库平滑升级到本二开仓库（重跑一键命令即可，配置 `vpngate_data/` 保留）。
