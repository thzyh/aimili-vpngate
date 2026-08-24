"""AimiliVPN 有效节点池的纯数据处理函数。"""

from __future__ import annotations

from typing import Any


Node = dict[str, Any]


def _node_id(node: Node) -> str:
    return str(node.get("id") or "").strip()


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
