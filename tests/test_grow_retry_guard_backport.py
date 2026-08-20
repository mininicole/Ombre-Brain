import asyncio

import pytest

from grow_retry_guard import request_fingerprint, reset_for_tests, run_once


@pytest.fixture(autouse=True)
def clear_retry_guard():
    reset_for_tests()
    yield
    reset_for_tests()


@pytest.mark.asyncio
async def test_completed_retry_reuses_result_without_running_twice():
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        return "2条|新2合0"

    fingerprint = request_fingerprint("same diary")
    first = await run_once(fingerprint, operation)
    second = await run_once(fingerprint, operation)

    assert first == "2条|新2合0"
    assert "未重复写入" in second
    assert calls == 1


@pytest.mark.asyncio
async def test_inflight_retry_returns_immediately_and_survives_waiter_cancel():
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        completed.set()
        return "后台写入完成"

    fingerprint = request_fingerprint("slow diary")
    original_waiter = asyncio.create_task(run_once(fingerprint, operation))
    await started.wait()
    duplicate = await asyncio.wait_for(run_once(fingerprint, operation), timeout=0.1)
    assert "仍在后台处理中" in duplicate

    original_waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await original_waiter

    release.set()
    await asyncio.wait_for(completed.wait(), timeout=1)
    await asyncio.sleep(0)
    retried = await run_once(fingerprint, operation)
    assert "未重复写入" in retried
    assert calls == 1


@pytest.mark.asyncio
async def test_failed_operation_is_not_cached():
    calls = 0

    async def operation():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary failure")
        return "retry succeeded"

    fingerprint = request_fingerprint("failed diary")
    with pytest.raises(RuntimeError, match="temporary failure"):
        await run_once(fingerprint, operation)
    assert await run_once(fingerprint, operation) == "retry succeeded"
    assert calls == 2
