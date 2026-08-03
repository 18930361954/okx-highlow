"""core/buckets.py 迁移守卫: main 的 re-export 必须与 buckets 同一对象,
防止有人再在 main.py 里写一份分叉实现 (frozen 双模块实例的历史根因)。"""
import core.buckets as buckets
import main


def test_main_reexports_are_same_objects():
    assert main.current_bucket_start is buckets.current_bucket_start
    assert main.previous_bucket_start is buckets.previous_bucket_start
    assert main.bucket_id is buckets.bucket_id


def test_reconciler_imports_from_buckets():
    import execution.reconciler as rec
    assert rec.previous_bucket_start is buckets.previous_bucket_start
    assert rec.bucket_id is buckets.bucket_id


def test_bucket_functions_behave():
    from datetime import datetime, timezone
    now = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc)
    cur = buckets.current_bucket_start(now, "6H")
    assert cur.hour == 12
    prev = buckets.previous_bucket_start(now, "6H")
    assert prev.hour == 6
    assert buckets.bucket_id(prev) == "2026-07-30T06:00Z"
