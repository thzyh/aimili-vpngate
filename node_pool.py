"""AimiliVPN 有效节点池的纯数据处理函数。"""

from __future__ import annotations

from typing import Any


Node = dict[str, Any]


def _node_id(node: Node) -> str:
    return str(node.get("id") or "").strip()


def filter_country_rows(rows: list[Node], country: str) -> list[Node]:
    """从原始 VPNGate 行中稳定筛出指定国家。"""
    wanted = str(country or "").strip().upper()
    return [
        row
        for row in rows
        if str(row.get("CountryShort") or "").strip().upper() == wanted
    ]


def protected_node_ids(active_node_id: str, slot_node_ids: list[str]) -> set[str]:
    """返回目录刷新时不能删除的主连接和受管槽位节点 ID。"""
    result = {
        str(node_id or "").strip()
        for node_id in [active_node_id, *slot_node_ids]
    }
    result.discard("")
    return result


def merge_country_pool(
    existing_nodes: list[Node],
    refreshed_nodes: list[Node],
    country: str,
    protected_ids: set[str],
    target_size: int,
) -> list[Node]:
    """只替换一个国家的有效节点，同时保留其他国家和活动节点。"""
    wanted = str(country or "").strip().upper()
    protected = {str(node_id or "").strip() for node_id in protected_ids}
    merged: list[Node] = []
    seen: set[str] = set()

    def append(node: Node) -> bool:
        node_id = _node_id(node)
        if not node_id or node_id in seen:
            return False
        merged.append(node)
        seen.add(node_id)
        return True

    for node in existing_nodes:
        node_country = str(node.get("country_short") or "").strip().upper()
        if node_country != wanted:
            append(node)

    selected_count = 0
    for node in existing_nodes:
        node_id = _node_id(node)
        node_country = str(node.get("country_short") or "").strip().upper()
        if node_country == wanted and node_id in protected and append(node):
            selected_count += 1

    for node in refreshed_nodes:
        if selected_count >= target_size:
            break
        node_country = str(node.get("country_short") or "").strip().upper()
        if node_country != wanted or node.get("probe_status") != "available":
            continue
        if append(node):
            selected_count += 1

    return merged


def protected_active_nodes(existing_nodes: list[Node], active_node_id: str) -> list[Node]:
    """返回补池期间必须保护的当前活动节点，最多一个。"""
    active_node_id = str(active_node_id or "").strip()
    if not active_node_id:
        return []
    for node in existing_nodes:
        node_id = _node_id(node)
        if node_id == active_node_id:
            return [node]
    return []


def candidate_queue(
    candidates: list[Node],
    retained: list[Node],
    blacklist: dict[str, dict[str, Any]],
    tested_ids: set[str],
    now: float,
    preferred_ids: set[str],
) -> list[Node]:
    """构建未测试候选队列，并优先复验上一轮的有效节点。"""
    excluded = {_node_id(node) for node in retained} | set(tested_ids)
    queue: list[Node] = []
    seen: set[str] = set()
    for node in candidates:
        node_id = _node_id(node)
        cooling = float((blacklist.get(node_id) or {}).get("until", 0) or 0) > now
        if node_id and node_id not in seen and node_id not in excluded and not cooling:
            queue.append(node)
            seen.add(node_id)
    preferred = set(preferred_ids)
    return sorted(queue, key=lambda node: 0 if _node_id(node) in preferred else 1)


def merge_probe_results(
    pool: list[Node],
    results: list[Node],
    target_size: int,
) -> tuple[list[Node], list[Node]]:
    """把本批成功节点加入有效池，并单独返回失败节点。"""
    merged = list(pool)
    seen = {_node_id(node) for node in merged}
    failed: list[Node] = []
    for node in results:
        node_id = _node_id(node)
        if node.get("probe_status") == "available":
            if node_id and node_id not in seen and len(merged) < target_size:
                merged.append(node)
                seen.add(node_id)
        else:
            failed.append(node)
    return merged[:target_size], failed


def rebalance_valid_pool(
    existing_nodes: list[Node],
    refreshed_nodes: list[Node],
    protected_ids: set[str],
    manual_ids: set[str],
    limit: int = 30,
) -> list[Node]:
    """Select a stable bounded pool while preserving runtime and country coverage."""
    capacity = max(0, min(30, int(limit)))
    existing_ids = {_node_id(node) for node in existing_nodes}
    by_id: dict[str, Node] = {}
    for item in [*refreshed_nodes, *existing_nodes]:
        node_id = _node_id(item)
        if node_id:
            by_id[node_id] = item

    def country(item: Node) -> str:
        return str(item.get("country_short") or "").strip().upper()

    def quality(item: Node) -> tuple[int, int, float, str]:
        kind = str(item.get("ip_type") or "").strip().lower()
        kind_rank = 0 if kind in ("residential", "mobile") else 1
        try:
            latency = int(item.get("latency_ms") or item.get("ping") or 999999)
        except (TypeError, ValueError):
            latency = 999999
        try:
            score = -float(item.get("score") or 0)
        except (TypeError, ValueError):
            score = 0
        return kind_rank, latency, score, _node_id(item)

    selected: list[Node] = []
    seen: set[str] = set()

    def add(item: Node, allow_unavailable: bool = False) -> None:
        node_id = _node_id(item)
        if len(selected) >= capacity or not node_id or node_id in seen:
            return
        if not allow_unavailable and item.get("probe_status") != "available":
            return
        selected.append(item)
        seen.add(node_id)

    for node_id in sorted(protected_ids):
        if node_id in by_id:
            add(by_id[node_id], allow_unavailable=True)
    for node_id in sorted(manual_ids):
        if node_id in by_id:
            add(by_id[node_id])

    available = [item for item in by_id.values() if item.get("probe_status") == "available"]
    existing_countries = sorted({country(item) for item in available if _node_id(item) in existing_ids and country(item)})
    new_countries = sorted({country(item) for item in available if country(item)} - set(existing_countries))
    for code in [*existing_countries, *new_countries]:
        if any(country(item) == code for item in selected):
            continue
        candidates = [item for item in available if country(item) == code]
        old = [item for item in candidates if _node_id(item) in existing_ids]
        add(min(old or candidates, key=quality))

    old_healthy = sorted(
        (item for item in available if _node_id(item) in existing_ids), key=quality
    )
    new_residential = sorted(
        (item for item in available if _node_id(item) not in existing_ids and str(item.get("ip_type") or "").lower() in ("residential", "mobile")),
        key=quality,
    )
    new_datacenter = sorted(
        (item for item in available if _node_id(item) not in existing_ids and item not in new_residential),
        key=quality,
    )
    for item in [*old_healthy, *new_residential, *new_datacenter]:
        add(item)
    return selected
