"""变更检测的核心不变式：new / updated / 无变更 / 字段白名单顺序。

对应临时冒烟 p4_smoke_diff.py 的提炼（不搬边界穷举，只保核心语义）。
"""

from __future__ import annotations

from collections.abc import Callable

from canvas_task_monitor.core.hashing import canonical_hash, hash_fields
from canvas_task_monitor.core.models import RawItem
from canvas_task_monitor.diff.change_detector import detect_changes
from canvas_task_monitor.storage.snapshot_repo import SnapshotRepo

SOURCE = "canvas_assignment"


def test_new_when_no_history(
    snapshot_repo: SnapshotRepo, make_item: Callable[..., RawItem]
) -> None:
    """本地无历史 → new，且 new 的 changed_fields 是全部白名单字段。"""
    item = make_item(1)

    changes = detect_changes("canvas", [item], snapshot_repo)

    assert len(changes) == 1
    assert changes[0].change_type == "new"
    assert changes[0].diff["hash"]["from"] is None
    assert changes[0].diff["hash"]["to"] == canonical_hash(SOURCE, item.payload)
    assert changes[0].diff["changed_fields"] == list(hash_fields(SOURCE))


def test_detect_does_not_write_snapshot(
    snapshot_repo: SnapshotRepo, memory_db, make_item: Callable[..., RawItem]
) -> None:
    """detect_changes 只读不写：落库必须在 LLM 成功之后由 Poller 负责。"""
    detect_changes("canvas", [make_item(1)], snapshot_repo)

    with memory_db.lock:
        count = memory_db.conn.execute("SELECT COUNT(*) AS c FROM snapshots").fetchone()["c"]
    assert count == 0


def test_no_change_when_hash_equal(
    snapshot_repo: SnapshotRepo, make_item: Callable[..., RawItem]
) -> None:
    """已落快照且内容未变 → 空列表。"""
    item = make_item(1)
    snapshot_repo.upsert_many([item])

    assert detect_changes("canvas", [item], snapshot_repo) == []


def test_updated_reports_changed_field_names(
    snapshot_repo: SnapshotRepo, make_item: Callable[..., RawItem]
) -> None:
    """内容变化 → updated，changed_fields 精确列出变化字段（按白名单顺序）。"""
    snapshot_repo.upsert_many([make_item(1)])

    updated = make_item(1, name="作业终稿", due_at="2026-10-15")
    changes = detect_changes("canvas", [updated], snapshot_repo)

    assert len(changes) == 1
    assert changes[0].change_type == "updated"
    assert changes[0].diff["changed_fields"] == ["name", "due_at"]
    assert changes[0].diff["hash"]["from"] != changes[0].diff["hash"]["to"]


def test_non_whitelisted_noise_does_not_trigger_change(
    snapshot_repo: SnapshotRepo, make_item: Callable[..., RawItem]
) -> None:
    """白名单外字段（如 updated_at）变化不产生变更（省 token 的关键前提）。"""
    snapshot_repo.upsert_many([make_item(1)])

    noisy = make_item(1, updated_at="2099-01-01T00:00:00Z")

    assert detect_changes("canvas", [noisy], snapshot_repo) == []
