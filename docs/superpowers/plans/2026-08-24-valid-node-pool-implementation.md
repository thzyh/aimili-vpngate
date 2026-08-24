# AimiliVPN 有效节点池实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将现有“只测试官方列表前 N 条并保留失败项”的行为改成“保留有效节点、失败节点冷却、向完整候选列表后部按批次补测，直到有效池达到目标或候选耗尽”。

**Architecture:** 新建纯标准库模块 `node_pool.py`，负责无副作用的活动节点保护、候选过滤、优先级和有效池合并；`vpngate_manager.py` 继续负责 API、OpenVPN、JSON 和线程。完整 OpenVPN 测试被拆成“对传入节点进行探测”和“UI 按 ID 测试并持久化”两层，使补池过程无需先把未验证节点暴露到 `nodes.json`；既有有效节点在每轮维护中优先复验，通过后才继续保留。

**Tech Stack:** Python 3.10+ 标准库、`unittest`、现有 OpenVPN/systemd/Bash 安装器。

**Spec:** `docs/superpowers/specs/2026-08-24-node-pool-and-fork-maintenance-design.md`

## 全局约束

- 不新增第三方 Python 依赖。
- 有效池默认目标为 `30`，API 候选读取上限为 `300`，批次大小为 `10`，失败冷却为 `1800` 秒。
- 纽约低内存环境继续使用 `OPENVPN_TEST_CONCURRENCY=2`。
- 同一 API 快照内同一节点最多测试一次。
- API 或部分探测失败不得清空已有有效池。
- 当前活动且出口健康的节点在补池期间必须保留。
- 本计划只实现有效节点池；最近节点优先和生产 VPS 迁移不在本计划内。
- `main` 只同步上游，所有实现提交进入 `custom`。

---

### Task 1：纯节点池选择与合并模块

**Files:**
- Create: `node_pool.py`
- Create: `tests/test_node_pool.py`
- Modify: `.gitignore`

**Interfaces:**
- Consumes: 节点字典至少包含 `id`、`probe_status` 和可选 `active`；冷却字典沿用 `blacklist.json` 的 `{node_id: {until, ...}}`。
- Produces: `protected_active_nodes(existing_nodes, active_node_id) -> list[dict]`、`candidate_queue(candidates, retained, blacklist, tested_ids, now, preferred_ids) -> list[dict]`、`merge_probe_results(pool, results, target_size) -> tuple[list[dict], list[dict]]`。

- [ ] **Step 1: 允许提交正式测试文件并写失败测试**

在 `.gitignore` 的测试段增加：

```gitignore
!tests/
!tests/test_*.py
```

创建 `tests/test_node_pool.py`，覆盖保留有效/活动节点、排除冷却和已测节点、结果合并和目标截断：

```python
import unittest

from node_pool import candidate_queue, merge_probe_results, protected_active_nodes


def node(node_id, status="not_checked", active=False):
    return {"id": node_id, "probe_status": status, "active": active}


class NodePoolTests(unittest.TestCase):
    def test_protects_only_the_current_active_node(self):
        existing = [node("good", "available"), node("bad", "unavailable"), node("live", "not_checked", True)]
        self.assertEqual(
            [item["id"] for item in protected_active_nodes(existing, "live")],
            ["live"],
        )

    def test_candidate_queue_prioritizes_existing_valid_and_excludes_ineligible_nodes(self):
        candidates = [node("fresh"), node("good"), node("cool"), node("tested")]
        queue = candidate_queue(
            candidates,
            [],
            {"cool": {"until": 200.0}},
            {"tested"},
            now=100.0,
            preferred_ids={"good"},
        )
        self.assertEqual([item["id"] for item in queue], ["good", "fresh"])

    def test_expired_cooldown_is_eligible(self):
        queue = candidate_queue([node("retry")], [], {"retry": {"until": 99.0}}, set(), now=100.0, preferred_ids=set())
        self.assertEqual([item["id"] for item in queue], ["retry"])

    def test_merge_keeps_successes_returns_failures_and_stops_at_target(self):
        pool, failed = merge_probe_results(
            [node("old", "available")],
            [node("new-1", "available"), node("dead", "unavailable"), node("new-2", "available")],
            target_size=2,
        )
        self.assertEqual([item["id"] for item in pool], ["old", "new-1"])
        self.assertEqual([item["id"] for item in failed], ["dead"])


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m unittest tests.test_node_pool -v`

Expected: FAIL，错误包含 `ModuleNotFoundError: No module named 'node_pool'`。

- [ ] **Step 3: 实现最小纯函数模块**

创建 `node_pool.py`：

```python
from __future__ import annotations

from typing import Any


def _node_id(node: dict[str, Any]) -> str:
    return str(node.get("id") or "").strip()


def protected_active_nodes(existing_nodes: list[dict[str, Any]], active_node_id: str) -> list[dict[str, Any]]:
    for node in existing_nodes:
        node_id = _node_id(node)
        if node_id and (node_id == active_node_id or node.get("active")):
            return [node]
    return []


def candidate_queue(candidates, retained, blacklist, tested_ids, now, preferred_ids):
    excluded = {_node_id(node) for node in retained} | set(tested_ids)
    queue = []
    seen = set()
    for node in candidates:
        node_id = _node_id(node)
        cooling = float((blacklist.get(node_id) or {}).get("until", 0) or 0) > now
        if node_id and node_id not in seen and node_id not in excluded and not cooling:
            queue.append(node)
            seen.add(node_id)
    preferred = set(preferred_ids)
    return sorted(queue, key=lambda node: 0 if _node_id(node) in preferred else 1)


def merge_probe_results(pool, results, target_size):
    merged = list(pool)
    seen = {_node_id(node) for node in merged}
    failed = []
    for node in results:
        node_id = _node_id(node)
        if node.get("probe_status") == "available":
            if node_id and node_id not in seen and len(merged) < target_size:
                merged.append(node)
                seen.add(node_id)
        else:
            failed.append(node)
    return merged[:target_size], failed
```

- [ ] **Step 4: 运行测试并确认通过**

Run: `python -m unittest tests.test_node_pool -v`

Expected: 4 tests，全部 PASS。

- [ ] **Step 5: 提交纯模块**

```bash
git add .gitignore node_pool.py tests/test_node_pool.py
git commit -m "feat: add valid node pool selection helpers"
```

---

### Task 2：可独立探测批次的 OpenVPN 测试层

**Files:**
- Modify: `vpngate_manager.py`
- Create: `tests/test_probe_batches.py`

**Interfaces:**
- Consumes: Task 1 的节点字典格式。
- Produces: `probe_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]`；现有 `test_multiple_nodes(node_ids)` 保持 API 兼容并负责持久化。

- [ ] **Step 1: 写失败测试**

创建 `tests/test_probe_batches.py`，用 mock 验证传入节点会被逐个探测且函数自身不调用 `write_json`：

```python
import unittest
from unittest import mock

import vpngate_manager as manager


class ProbeBatchTests(unittest.TestCase):
    def test_probe_nodes_returns_one_result_per_input_without_persisting(self):
        items = [
            {"id": "a", "config_text": "remote 127.0.0.1 443 tcp", "remote_host": "127.0.0.1", "remote_port": 443, "ping": 1},
            {"id": "b", "config_text": "remote 127.0.0.2 443 tcp", "remote_host": "127.0.0.2", "remote_port": 443, "ping": 2},
        ]
        def result(item):
            return dict(item, probe_status=("available" if item["id"] == "a" else "unavailable"))
        with mock.patch.object(manager, "tcp_prescreen_dead", return_value={}), mock.patch.object(manager.vpn_utils, "enrich_ip_info"), mock.patch.object(manager, "_probe_one_node", side_effect=result) as probe, mock.patch.object(manager, "write_json") as write:
            actual = manager.probe_nodes(items)
        self.assertEqual([item["id"] for item in actual], ["a", "b"])
        self.assertEqual(probe.call_count, 2)
        write.assert_not_called()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m unittest tests.test_probe_batches -v`

Expected: FAIL，错误包含 `AttributeError`，指出缺少 `_probe_one_node` 或 `probe_nodes`。

- [ ] **Step 3: 抽取单节点探测和批次探测**

在 `vpngate_manager.py` 中把现有 `test_multiple_nodes` 内部 `test_worker` 抽为：

```python
def _probe_one_node(node: dict[str, Any]) -> dict[str, Any]:
    node_id = node["id"]
    config_text = node.get("config_text") or ""
    host = str(node.get("remote_host") or node.get("ip"))
    port = parse_int(node.get("remote_port"))
    fallback_ping = parse_int(node.get("ping"))
    temp_path = test_config_path(node_id)
    try:
        CONFIG_DIR.mkdir(exist_ok=True, parents=True)
        temp_path.write_text(config_text, encoding="utf-8")
    except Exception as exc:
        return {
            "id": node_id,
            "latency_ms": 0,
            "probe_status": "unavailable",
            "probe_message": f"Failed to write configuration: {exc}",
            "probed_at": time.time(),
            "owner": "", "asn": "", "as_name": "", "location": "",
            "ip_type": "", "quality": "",
        }

    latency = vpn_utils.ping_latency_ms(host, port, fallback_ping)
    tun_index = None
    try:
        tun_index = get_free_test_index()
        ok, message, _ = run_openvpn_until_ready(
            str(temp_path), keep_alive=False, route_nopull=True,
            timeout=12, dev=f"tun{tun_index}",
        )
    finally:
        if tun_index is not None:
            release_test_index(tun_index)
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass

    return {
        **node,
        "latency_ms": latency,
        "probe_status": "available" if ok else "unavailable",
        "probe_message": message,
        "probed_at": time.time(),
        "owner": "", "asn": "", "as_name": "", "location": "",
        "ip_type": "", "quality": "",
    }


def probe_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    dead_prescreen = tcp_prescreen_dead(nodes)
    remaining = [node for node in nodes if node.get("id") not in dead_prescreen]
    results = dict(dead_prescreen)
    workers = min(OPENVPN_TEST_CONCURRENCY, max(1, len(remaining)))
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {executor.submit(_probe_one_node, node): node["id"] for node in remaining}
        for future in concurrent.futures.as_completed(futures):
            node_id = futures[future]
            try:
                results[node_id] = future.result()
            except Exception as exc:
                results[node_id] = {"id": node_id, "probe_status": "unavailable", "probe_message": f"Test exception: {exc}", "latency_ms": 0}
    successful = [item for item in results.values() if item.get("probe_status") == "available"]
    if successful:
        try:
            vpn_utils.enrich_ip_info(successful)
        except Exception as exc:
            print(f"[probe_nodes] 批量富化 IP 失败: {exc}", flush=True)
    return [results[node["id"]] for node in nodes if node.get("id") in results]
```

重写 `test_multiple_nodes(node_ids)` 为兼容包装层：读取节点、调用 `probe_nodes`、按 ID 合并结果、排序并写回 `NODES_FILE`。保留现有 API 返回结构。

- [ ] **Step 4: 运行批次测试和语法检查**

Run: `python -m unittest tests.test_probe_batches -v`

Expected: PASS。

Run: `python -m py_compile vpngate_manager.py vpn_utils.py proxy_server.py node_pool.py`

Expected: exit 0，无输出。

- [ ] **Step 5: 提交探测层重构**

```bash
git add vpngate_manager.py tests/test_probe_batches.py
git commit -m "refactor: separate node probing from persistence"
```

---

### Task 3：按批次补满有效节点池

**Files:**
- Modify: `vpngate_manager.py`
- Create: `tests/test_pool_maintenance.py`

**Interfaces:**
- Consumes: `node_pool.protected_active_nodes`、`candidate_queue`、`merge_probe_results` 和 `probe_nodes`。
- Produces: `replenish_valid_pool(existing_nodes, candidates, blacklist, probe_batch, now) -> tuple[list[dict], dict, dict]`；`maintain_valid_nodes` 使用该函数并只持久化有效池。

- [ ] **Step 1: 写补池状态机失败测试**

创建 `tests/test_pool_maintenance.py`。测试临时覆盖目标值和批次值，模拟前两批失败、后续成功，验证继续向后补测并停止：

```python
import unittest
from unittest import mock

import vpngate_manager as manager


def node(index, status="not_checked", active=False):
    return {"id": f"n{index}", "probe_status": status, "active": active, "config_file": f"n{index}.ovpn", "config_text": ""}


class PoolMaintenanceTests(unittest.TestCase):
    def test_replenishes_from_later_candidates_until_target(self):
        existing = [node(index, "available") for index in range(2)]
        candidates = [node(index) for index in range(10)]
        calls = []

        def probe(batch):
            calls.append([item["id"] for item in batch])
            return [dict(item, probe_status=("unavailable" if item["id"] in {"n2", "n3"} else "available")) for item in batch]

        with mock.patch.object(manager, "TARGET_VALID_POOL_SIZE", 5), mock.patch.object(manager, "NODE_TEST_BATCH_SIZE", 2):
            pool, blacklist, stats = manager.replenish_valid_pool(existing, candidates, {}, probe, now=100.0)

        self.assertEqual([item["id"] for item in pool], ["n0", "n1", "n4", "n5", "n6"])
        self.assertEqual(calls, [["n0", "n1"], ["n2", "n3"], ["n4", "n5"], ["n6", "n7"]])
        self.assertEqual(set(blacklist), {"n2", "n3"})
        self.assertEqual(stats["stop_reason"], "target_reached")

    def test_stops_when_candidates_are_exhausted(self):
        def fail(batch):
            return [dict(item, probe_status="unavailable", probe_message="failed") for item in batch]
        with mock.patch.object(manager, "TARGET_VALID_POOL_SIZE", 3), mock.patch.object(manager, "NODE_TEST_BATCH_SIZE", 2):
            pool, _, stats = manager.replenish_valid_pool([], [node(1), node(2)], {}, fail, now=100.0)
        self.assertEqual(pool, [])
        self.assertEqual(stats["tested"], 2)
        self.assertEqual(stats["stop_reason"], "candidates_exhausted")

    def test_api_failure_keeps_the_existing_pool(self):
        existing = [node(1, "available")]
        with mock.patch.object(manager, "active_openvpn_running", return_value=True), mock.patch.object(manager, "read_nodes", return_value=existing), mock.patch.object(manager, "fetch_candidates", side_effect=RuntimeError("API down")), mock.patch.object(manager.vpn_utils, "check_and_fix_dns"), mock.patch.object(manager.vpn_utils, "diagnose_api_failure", return_value=(1000, "API down")), mock.patch.object(manager, "set_state"), mock.patch.object(manager, "write_json") as write:
            message = manager.maintain_valid_nodes()
        self.assertIn("保留现有有效节点", message)
        write.assert_not_called()
```

- [ ] **Step 2: 运行测试并确认失败**

Run: `python -m unittest tests.test_pool_maintenance -v`

Expected: FAIL，缺少 `TARGET_VALID_POOL_SIZE` 或 `replenish_valid_pool`。

- [ ] **Step 3: 添加配置兼容层**

在导入区增加 `import node_pool`，用以下兼容层替换原来的 `TARGET_VALID_NODES` 和 `MAX_SCAN_ROWS` 定义：

```python
_legacy_max_scan_rows = env_int("MAX_SCAN_ROWS", 300, 1)
MAX_FETCH_ROWS = env_int("MAX_FETCH_ROWS", _legacy_max_scan_rows, 1)
_legacy_target_valid_nodes = env_int("TARGET_VALID_NODES", 30, 1)
TARGET_VALID_POOL_SIZE = env_int("TARGET_VALID_POOL_SIZE", _legacy_target_valid_nodes, 1)
TARGET_VALID_NODES = TARGET_VALID_POOL_SIZE
NODE_TEST_BATCH_SIZE = env_int("NODE_TEST_BATCH_SIZE", 10, 1)
PROBE_FAILURE_COOLDOWN_SECONDS = env_int("PROBE_FAILURE_COOLDOWN_SECONDS", 1800, 1)
```

将 `fetch_candidates` 中的 `rows[:MAX_SCAN_ROWS]` 改为 `rows[:MAX_FETCH_ROWS]`，并继续在解析阶段排除仍处冷却期的节点。

- [ ] **Step 4: 实现有限状态补池函数**

在 `vpngate_manager.py` 中实现：

```python
def replenish_valid_pool(existing_nodes, candidates, blacklist, probe_batch, now=None):
    now = time.time() if now is None else now
    pool = node_pool.protected_active_nodes(existing_nodes, active_openvpn_node_id)
    preferred_ids = {
        str(item.get("id") or "")
        for item in existing_nodes
        if item.get("probe_status") == "available" and not item.get("active")
    }
    tested_ids = set()
    failed_entries = dict(blacklist)
    tested_count = 0

    while len(pool) < TARGET_VALID_POOL_SIZE:
        queue = node_pool.candidate_queue(
            candidates, pool, failed_entries, tested_ids, now, preferred_ids,
        )
        if not queue:
            return pool, failed_entries, {"tested": tested_count, "stop_reason": "candidates_exhausted"}
        batch = queue[:NODE_TEST_BATCH_SIZE]
        tested_ids.update(str(item.get("id") or "") for item in batch)
        results = probe_batch(batch)
        returned_ids = {str(item.get("id") or "") for item in results}
        for item in batch:
            node_id = str(item.get("id") or "")
            if node_id not in returned_ids:
                results.append({
                    **item,
                    "probe_status": "unavailable",
                    "probe_message": "节点探测未返回结果",
                    "probed_at": now,
                })
        tested_count += len(batch)
        pool, failed = node_pool.merge_probe_results(pool, results, TARGET_VALID_POOL_SIZE)
        for item in failed:
            node_id = str(item.get("id") or "")
            failed_entries[node_id] = {
                "id": node_id,
                "ip": item.get("ip") or item.get("remote_host") or "",
                "country": item.get("country", ""),
                "reason": item.get("probe_message") or "OpenVPN 节点验证失败",
                "marked_at": now,
                "until": now + PROBE_FAILURE_COOLDOWN_SECONDS,
            }

    return pool, failed_entries, {"tested": tested_count, "stop_reason": "target_reached"}
```

- [ ] **Step 5: 将维护线程切换到新补池流程**

`maintain_valid_nodes` 在拉取 API 之前先执行 `existing = read_nodes()`。API 失败时更新错误状态并返回 `f"获取节点失败，保留现有有效节点 {len(existing)} 个"`，不得调用 `write_json`。API 成功后执行：

```python
existing = read_nodes()
blacklist = load_blacklist()
pool, blacklist, stats = replenish_valid_pool(existing, candidates, blacklist, probe_nodes)
write_json(BLACKLIST_FILE, blacklist)
write_json(NODES_FILE, sort_all_nodes(pool))
```

删除“把全部候选先写入 `nodes.json` 并一次性测试”的旧代码。状态报告只统计有效池，增加本轮候选数、测试数和停止原因；API 失败时直接保留 `existing` 并返回错误状态，不写空池。

- [ ] **Step 6: 运行状态机测试和全部单元测试**

Run: `python -m unittest tests.test_pool_maintenance -v`

Expected: 3 tests，全部 PASS。

Run: `python -m unittest discover -s tests -v`

Expected: 所有测试 PASS，且没有网络请求和 OpenVPN 进程残留。

- [ ] **Step 7: 提交补池实现**

```bash
git add vpngate_manager.py tests/test_pool_maintenance.py
git commit -m "feat: replenish the valid node pool in batches"
```

---

### Task 4：文档、安装器自检与回归验证

**Files:**
- Modify: `README.md`
- Modify: `CHANGELOG.md`
- Modify: `scripts/selfcheck_multiexit.sh`

**Interfaces:**
- Consumes: Task 3 新增的四个环境变量和 `nodes.json` 只保存有效节点的语义。
- Produces: 用户可见配置说明，以及能够检查池内是否残留无效节点的只读自检输出。

- [ ] **Step 1: 写安装器和文档契约测试**

创建 `tests/test_project_contract.py`：

```python
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]


class ProjectContractTests(unittest.TestCase):
    def test_readme_documents_pool_controls(self):
        text = (ROOT / "README.md").read_text(encoding="utf-8")
        for name in ("TARGET_VALID_POOL_SIZE", "MAX_FETCH_ROWS", "NODE_TEST_BATCH_SIZE", "PROBE_FAILURE_COOLDOWN_SECONDS"):
            self.assertIn(name, text)

    def test_selfcheck_rejects_visible_unavailable_nodes(self):
        text = (ROOT / "scripts" / "selfcheck_multiexit.sh").read_text(encoding="utf-8")
        self.assertIn('probe_status')
        self.assertIn('unavailable', text)
```

- [ ] **Step 2: 运行契约测试并确认失败**

Run: `python -m unittest tests.test_project_contract -v`

Expected: FAIL，README 缺少新配置或自检缺少节点状态检查。

- [ ] **Step 3: 更新 README、CHANGELOG 和自检**

README 配置表新增四个环境变量，并明确：“有效数量取决于 VPNGate 当前供给；达到目标或候选耗尽即停止”。CHANGELOG 新增 `Custom 1.1.0` 节点池条目。

在 `scripts/selfcheck_multiexit.sh` 增加只读 Python 检查：

```bash
python3 - <<'PY'
import json
from pathlib import Path

path = Path('/opt/aimilivpn/vpngate_data/nodes.json')
nodes = json.loads(path.read_text(encoding='utf-8')) if path.exists() else []
invalid = [node.get('id', '') for node in nodes if node.get('probe_status') == 'unavailable']
if invalid:
    raise SystemExit(f"节点池仍包含 {len(invalid)} 个 unavailable 节点")
print(f"有效节点池检查通过：当前可见节点 {len(nodes)} 个")
PY
```

确认 `install.sh` 不写死新变量值；由部署脚本或用户 drop-in 覆盖，项目默认值保持代码中的 `300/30/10/1800`。本任务不修改安装器。

- [ ] **Step 4: 完整静态与单元回归**

Run: `python -m unittest discover -s tests -v`

Expected: 全部 PASS。

Run: `python -m py_compile vpngate_manager.py vpn_utils.py proxy_server.py node_pool.py`

Expected: exit 0。

Run: `bash -n install.sh && bash -n scripts/selfcheck_multiexit.sh`

Expected: exit 0。

- [ ] **Step 5: 对比自定义分支差异并提交**

Run: `git diff --check upstream/main...HEAD`

Expected: exit 0。

```bash
git add README.md CHANGELOG.md scripts/selfcheck_multiexit.sh tests/test_project_contract.py
git commit -m "docs: document and verify valid node pool behavior"
```

- [ ] **Step 6: 推送候选实现但不迁移生产 VPS**

```bash
git push origin custom
```

推送后记录最终提交哈希。该哈希将作为下一份独立计划“修改 `aimili-3xui-simple-deploy` 并固定部署用户 Fork”的输入；此时仍不修改 `ny`。
