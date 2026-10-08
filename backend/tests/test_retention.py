import asyncio
import time

import pytest

from app.services.persistence import InMemoryPersistence
from app.services.recordings import LocalRecordingStorage
from app.services.retention import DAY, InMemoryRetentionBackend, RetentionJob, purge_once
from app.services.state_store import InMemoryStateStore

NOW = 1_800_000_000.0


async def seed(db: InMemoryPersistence, org: str, age_days: float, with_recording: bool = False, storage=None):
    cid = await db.start_conversation(org, "s", "u")
    db.convs[cid]["created_at"] = NOW - age_days * DAY
    await db.add_transcript(org, cid, "user", "hola")
    await db.add_event(org, cid, "x", {}, None)
    await db.record_tool(org, cid, {"tool": "t", "call_id": cid, "args": {}, "result": {}, "ok": True, "duration_ms": 1})
    if with_recording:
        key = f"{org}/{cid}.wav"
        if storage:
            await storage.put(key, b"RIFF")
        await db.add_recording(org, cid, key, 4, 1.0)
    return cid


async def audit_sink(db):
    async def audit(org, actor, action, detail):
        await db.audit(org, actor, action, detail)

    return audit


@pytest.mark.asyncio
async def test_purges_only_expired_and_cleans_everything(tmp_path):
    db, st = InMemoryPersistence(), LocalRecordingStorage(str(tmp_path))
    old = await seed(db, "o1", 40, with_recording=True, storage=st)
    fresh = await seed(db, "o1", 5, with_recording=True, storage=st)
    out = await purge_once(InMemoryRetentionBackend(db, 30), st, await audit_sink(db), now=NOW)
    assert out["o1"]["conversations"] == 1 and out["o1"]["recordings"] == 1
    assert old not in db.convs and old not in db.transcripts and old not in db.events and old not in db.recordings
    assert not any(t["conversation_id"] == old for t in db.tools)
    assert fresh in db.convs and fresh in db.transcripts
    assert await st.get(f"o1/{old}.wav") is None and await st.get(f"o1/{fresh}.wav") == b"RIFF"
    assert [a for a in db.audits if a["action"] == "retention.purged"][0]["detail"]["retention_days"] == 30


@pytest.mark.asyncio
async def test_each_org_uses_its_own_retention_days():
    db = InMemoryPersistence()
    a = await seed(db, "short", 10)
    b = await seed(db, "long", 10)
    be = InMemoryRetentionBackend(db, 30, days_by_org={"short": 7, "long": 90})
    await purge_once(be, None, await audit_sink(db), now=NOW)
    assert a not in db.convs and b in db.convs


@pytest.mark.asyncio
async def test_zero_or_negative_days_keeps_everything():
    db = InMemoryPersistence()
    c = await seed(db, "o", 9999)
    out = await purge_once(InMemoryRetentionBackend(db, 0), None, await audit_sink(db), now=NOW)
    assert out == {} and c in db.convs


@pytest.mark.asyncio
async def test_storage_failure_keeps_conversation_for_retry(tmp_path):
    db = InMemoryPersistence()
    good = LocalRecordingStorage(str(tmp_path))
    cid = await seed(db, "o1", 60, with_recording=True, storage=good)

    class Failing(LocalRecordingStorage):
        async def delete(self, key):
            raise OSError("S3 caído")

    out = await purge_once(InMemoryRetentionBackend(db, 30), Failing(str(tmp_path)), await audit_sink(db), now=NOW)
    assert cid in db.convs and out["o1"]["skipped"] == 1 and out["o1"]["conversations"] == 0
    out2 = await purge_once(InMemoryRetentionBackend(db, 30), good, await audit_sink(db), now=NOW)  # ya se recupera
    assert cid not in db.convs and out2["o1"]["conversations"] == 1


@pytest.mark.asyncio
async def test_recordings_without_storage_are_not_orphaned():
    db = InMemoryPersistence()
    cid = await seed(db, "o1", 60, with_recording=True)
    out = await purge_once(InMemoryRetentionBackend(db, 30), None, await audit_sink(db), now=NOW)
    assert cid in db.convs and out["o1"]["skipped"] == 1  # sin almacenamiento no se pierde la referencia


@pytest.mark.asyncio
async def test_batches_drain_everything():
    db = InMemoryPersistence()
    ids = [await seed(db, "o1", 100 + i) for i in range(7)]
    out = await purge_once(InMemoryRetentionBackend(db, 30), None, await audit_sink(db), now=NOW, batch_size=3)
    assert out["o1"]["conversations"] == 7 and not any(i in db.convs for i in ids)


@pytest.mark.asyncio
async def test_job_takes_lock_so_only_one_replica_purges():
    db, store = InMemoryPersistence(), InMemoryStateStore()
    await seed(db, "o1", time.time() / DAY)  # antiquísima
    be = InMemoryRetentionBackend(db, 30)

    async def audit(*a):
        await db.audit(*a)

    j1 = RetentionJob(be, None, audit, store.acquire_lock, 3600, 100, initial_delay_s=0)
    j2 = RetentionJob(be, None, audit, store.acquire_lock, 3600, 100, initial_delay_s=0)
    r1, r2 = await j1.run_once(), await j2.run_once()
    assert r1 is not None and r2 is None  # la segunda réplica no obtiene el candado


@pytest.mark.asyncio
async def test_job_survives_backend_errors():
    class Broken:
        async def list_orgs(self):
            raise RuntimeError("bd caída")

    store = InMemoryStateStore()
    job = RetentionJob(Broken(), None, lambda *a: asyncio.sleep(0), store.acquire_lock, 3600, 10, initial_delay_s=0)
    assert await job.run_once() is None  # no propaga
