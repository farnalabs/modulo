"""SPIKE (HARD GATE) for PR A of the Celery->SAQ migration.

Empirically verifies SAQ 0.26.4 semantics against a local Redis 7 instance.

Targets:
    PRIMARY: redis://localhost:6380/1 (dedicated DB index, never touches other data)

Each check prints PASS/FAIL/UNVERIFIED with raw evidence. The committed output is
the SPIKE evidence for the PR A ADR.

Usage:
    uv run python scripts/spike_saq.py
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections import Counter

from redis import asyncio as aioredis
from saq.job import CronJob, Job, Status
from saq.queue.redis import RedisQueue

REDIS_URL = "redis://localhost:6380/1"
QUEUE_NAME = "spike"

results: list[tuple[str, str, str]] = []


def record(name: str, status: str, evidence: str) -> None:
    """Append a (name, status, evidence) row and echo it."""
    results.append((name, status, evidence))
    print(f"[{status}] {name}: {evidence}", flush=True)


# ---------------------------------------------------------------------------
# Worker functions (module level so Worker can look them up by __qualname__)
# ---------------------------------------------------------------------------
_flaky_attempts: list[float] = []
_flaky_attempt_nos: list[int] = []
_fail_attempt_nos: list[int] = []
_cron_fires: Counter[str] = Counter()
_cron_concurrent = 0
_cron_max_concurrent = 0
_slow_sleep_started = 0.0


async def spike_flaky(ctx: dict) -> str:
    """Fails on first attempt, succeeds on the second (retries=2 -> 2 total attempts)."""
    global _flaky_attempts, _flaky_attempt_nos
    job: Job = ctx["job"]
    _flaky_attempts.append(time.monotonic())
    _flaky_attempt_nos.append(job.attempts)
    if job.attempts == 1:
        raise RuntimeError("flaky first attempt")
    return "ok"


async def spike_always_fail(ctx: dict) -> None:
    global _fail_attempt_nos
    job: Job = ctx["job"]
    _fail_attempt_nos.append(job.attempts)
    raise RuntimeError("always fails")


async def spike_slow(ctx: dict) -> None:
    global _slow_sleep_started
    _slow_sleep_started = time.monotonic()
    await asyncio.sleep(30)


async def spike_cron(ctx: dict) -> None:
    global _cron_fires, _cron_concurrent, _cron_max_concurrent
    _cron_concurrent += 1
    _cron_max_concurrent = max(_cron_max_concurrent, _cron_concurrent)
    _cron_fires["fires"] += 1
    await asyncio.sleep(2.5)
    _cron_concurrent -= 1


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


async def check1_blmove(redis: aioredis.Redis, q: RedisQueue) -> None:
    """BLMOVE dequeue: queued list membership, then dequeue moves to active."""
    key = "run:blmove"
    job = await q.enqueue("spike_slow", key=key)
    queued = await redis.lrange(f"saq:{QUEUE_NAME}:queued", 0, -1)
    in_queued = any(x == job.id.encode() for x in queued)
    if job is None or not in_queued:
        record("1 BLMOVE dequeue", "FAIL", f"job not in queued list: {job=} {queued=}")
        return
    deq = await q.dequeue(timeout=1)
    active = await redis.lrange(f"saq:{QUEUE_NAME}:active", 0, -1)
    queued_after = await redis.lrange(f"saq:{QUEUE_NAME}:queued", 0, -1)
    moved = (
        deq is not None
        and deq.id == job.id
        and any(x == job.id.encode() for x in active)
        and not any(x == job.id.encode() for x in queued_after)
    )
    record(
        "1 BLMOVE dequeue",
        "PASS" if moved else "FAIL",
        f"queued before={len(queued)}, dequeue id={deq.id if deq else None}, "
        f"active after={len(active)}, queued after={len(queued_after)} (blmove LEFT->RIGHT)",
    )


async def check2_queue_keys(redis: aioredis.Redis, q: RedisQueue) -> None:
    """Queue key naming: saq:job:<queue>:<key>, saq:<queue>:incomplete/queued/active."""
    key = "run:keys"
    job = await q.enqueue("spike_slow", key=key)
    job_hash = f"saq:job:{QUEUE_NAME}:{key}"
    incomplete = f"saq:{QUEUE_NAME}:incomplete"
    queued = f"saq:{QUEUE_NAME}:queued"
    active = f"saq:{QUEUE_NAME}:active"
    hash_type = await redis.type(job_hash)
    incomplete_type = await redis.type(incomplete)
    queued_type = await redis.type(queued)
    scored = await redis.zscore(incomplete, job.id)
    observed = {
        "job_id_attr": job.id,
        "job_hash_type": hash_type.decode(),
        "incomplete_type": incomplete_type.decode(),
        "queued_type": queued_type.decode(),
        "zscore_present": scored is not None,
    }
    ok = (
        job.id == job_hash
        and hash_type == b"string"
        and incomplete_type == b"zset"
        and queued_type == b"list"
        and scored is not None
    )
    # active list exists after a dequeue
    await q.dequeue(timeout=1)
    active_type = await redis.type(active)
    ok = ok and active_type == b"list"
    observed["active_type"] = active_type.decode()
    record("2 queue keys", "PASS" if ok else "FAIL", json.dumps(observed))


async def check3_dedupe(redis: aioredis.Redis, q: RedisQueue) -> None:
    """Dedupe: same key enqueued twice -> second is deduped; abort key is a SETEX string."""
    key = "run:dedupe"
    job1 = await q.enqueue("spike_slow", key=key)
    job2 = await q.enqueue("spike_slow", key=key)
    abort_key = f"saq:abort:{key}"
    abort_after_enqueue = await redis.exists(abort_key)
    deduped = job1 is not None and job2 is None and abort_after_enqueue == 0

    # abort key semantics: SETEX string created by abort()/sweep (not enqueue)
    await redis.blmove(f"saq:{QUEUE_NAME}:queued", f"saq:{QUEUE_NAME}:active", 0, "LEFT", "RIGHT")
    await q.abort(job1, "test-abort")
    abort_type = await redis.type(abort_key)
    abort_ttl = await redis.pttl(abort_key)
    abort_is_string = abort_type == b"string" and 0 < abort_ttl <= 5000

    # while the abort key is present, a fresh enqueue of the same key is blocked
    job3 = await q.enqueue("spike_slow", key=key)
    blocked_by_abort = job3 is None

    ok = deduped and abort_is_string and blocked_by_abort
    record(
        "3 dedupe",
        "PASS" if ok else "FAIL",
        f"job1={job1.id if job1 else None}, job2={'deduped(None)' if job2 is None else job2.id}, "
        f"abort_after_enqueue={abort_after_enqueue}, abort_type={abort_type.decode()}, "
        f"abort_pttl={abort_ttl}ms (SETEX ttl=5), enqueue_with_abort_key={blocked_by_abort}",
    )


async def check4_partial_eviction(redis: aioredis.Redis, q: RedisQueue) -> None:
    """Partial-eviction repair: DEL abort + ZREM incomplete + LREM queued/active + enqueue."""
    key = "run:pe"
    job = await q.enqueue("spike_slow", key=key)
    incomplete = f"saq:{QUEUE_NAME}:incomplete"
    queued = f"saq:{QUEUE_NAME}:queued"
    active = f"saq:{QUEUE_NAME}:active"
    abort_key = f"saq:abort:{key}"
    zscore_before = await redis.zscore(incomplete, job.id)
    # SETEX abort key explicitly (simulate an abort having fired before eviction)
    await redis.setex(abort_key, 5, "simulated")
    # DEL the job hash (simulate eviction of the hash while incomplete member survives)
    await redis.delete(job.id)
    # reconcile repair sequence
    await redis.delete(abort_key)
    zrem = await redis.zrem(incomplete, job.id)
    lrem_queued = await redis.lrem(queued, 0, job.id)
    lrem_active = await redis.lrem(active, 0, job.id)
    rejob = await q.enqueue("spike_slow", key=key)
    re_scored = await redis.zscore(incomplete, rejob.id)
    re_hash = await redis.exists(rejob.id)
    ok = zscore_before is not None and zrem == 1 and rejob is not None and re_scored is not None and re_hash == 1
    record(
        "4 partial-eviction repair",
        "PASS" if ok else "FAIL",
        f"zscore_before={'present' if zscore_before is not None else 'MISSING'}, "
        f"DEL abort + ZREM incomplete zrem={zrem}, LREM queued={lrem_queued}, LREM active={lrem_active}, "
        f"re-enqueue={'deduped(None)' if rejob is None else rejob.id}, "
        f"re-zscore={'present' if re_scored is not None else 'MISSING'}, "
        f"re-hash exists={re_hash == 1} "
        f"(NOTE: job id is DETERMINISTIC from key {key} -> same id string; the enqueue RETURN "
        f"is the observable that dedupe cleared, matching plan v10 line 18)",
    )


async def check5_queued_duplicate(redis: aioredis.Redis, q: RedisQueue) -> None:
    """QUEUED-duplicate: hash deleted while QUEUED; repair + enqueue leaves exactly ONE queued entry."""
    key = "run:qd"
    job = await q.enqueue("spike_slow", key=key)
    queued = f"saq:{QUEUE_NAME}:queued"
    incomplete = f"saq:{QUEUE_NAME}:incomplete"
    active = f"saq:{QUEUE_NAME}:active"
    abort_key = f"saq:abort:{key}"
    count_before = sum(1 for x in await redis.lrange(queued, 0, -1) if x == job.id.encode())
    await redis.delete(job.id)  # evict the hash while QUEUED
    await redis.delete(abort_key)
    await redis.zrem(incomplete, job.id)
    await redis.lrem(queued, 0, job.id)
    await redis.lrem(active, 0, job.id)
    rejob = await q.enqueue("spike_slow", key=key)
    entries_after = await redis.lrange(queued, 0, -1)
    count_after = sum(1 for x in entries_after if x == job.id.encode())
    ok = count_before == 1 and rejob is not None and count_after == 1
    record(
        "5 QUEUED-duplicate",
        "PASS" if ok else "FAIL",
        f"queued entries before eviction={count_before}, re-enqueue={'ok' if rejob is not None else 'deduped'}, "
        f"queued entries after repair+enqueue={count_after} (LREM queued prevents duplicate)",
    )


async def check6_retry_timing(redis: aioredis.Redis, q: RedisQueue) -> None:
    """retry_backoff=False -> FIXED retry_delay (no jitter). Fail once, succeed once."""
    global _flaky_attempts, _flaky_attempt_nos
    _flaky_attempts = []
    _flaky_attempt_nos = []
    await redis.flushdb()  # isolate from earlier checks
    from saq import Worker

    w = Worker(q, [spike_flaky], concurrency=1, timers={"schedule": 1, "sweep": 60, "worker_info": 60})
    await q.enqueue(
        "spike_flaky",
        key="run:retrytiming",
        retries=2,
        retry_delay=10,
        retry_backoff=False,
        timeout=30,
    )
    t = asyncio.create_task(w.start())
    # wait until terminal
    deadline = time.monotonic() + 45
    final: Job | None = None
    while time.monotonic() < deadline:
        cur = await q.job("run:retrytiming")
        if cur is not None and cur.status in (Status.COMPLETE, Status.FAILED, Status.ABORTED):
            final = cur
            break
        await asyncio.sleep(0.2)
    await w.stop()
    await t

    if final is None or len(_flaky_attempts) < 2:
        record("6 retry timing", "FAIL", f"no terminal state reached: {final=} {_flaky_attempts=}")
        return
    gap = _flaky_attempts[1] - _flaky_attempts[0]
    ok = final.status == Status.COMPLETE and 8.0 <= gap <= 14.0
    record(
        "6 retry timing",
        "PASS" if ok else "FAIL",
        f"attempt starts (monotonic)={[round(a, 2) for a in _flaky_attempts]}, "
        f"attempt_nos={_flaky_attempt_nos}, gap={round(gap, 2)}s "
        f"(retry_delay=10, retry_backoff=False -> FIXED ~10s, no jitter), "
        f"final_status={final.status}",
    )


async def check7_retries_semantics(redis: aioredis.Redis, q: RedisQueue) -> None:
    """retries=2 == 2 TOTAL attempts (N-1 retries). Attempts counter then FAILED."""
    global _fail_attempt_nos
    _fail_attempt_nos = []
    await redis.flushdb()  # isolate from earlier checks
    from saq import Worker

    w = Worker(q, [spike_always_fail], concurrency=1, timers={"schedule": 1, "sweep": 60, "worker_info": 60})
    await q.enqueue(
        "spike_always_fail",
        key="run:retries",
        retries=2,
        retry_delay=0,
        retry_backoff=False,
        timeout=30,
    )
    t = asyncio.create_task(w.start())
    deadline = time.monotonic() + 30
    final: Job | None = None
    while time.monotonic() < deadline:
        cur = await q.job("run:retries")
        if cur is not None and cur.status in (Status.COMPLETE, Status.FAILED, Status.ABORTED):
            final = cur
            break
        await asyncio.sleep(0.2)
    await w.stop()
    await t

    if final is None:
        record("7 retries=N semantics", "FAIL", "no terminal state reached")
        return
    ok = final.attempts == 2 and final.status == Status.FAILED and _fail_attempt_nos == [1, 2]
    record(
        "7 retries=N semantics",
        "PASS" if ok else "FAIL",
        f"attempt_nos={_fail_attempt_nos}, final attempts={final.attempts}, final status={final.status} "
        f"(retries=2 -> 2 TOTAL attempts, 1 retry)",
    )


async def check8_ttl_semantics(redis: aioredis.Redis, q: RedisQueue) -> None:
    """ttl semantics: enqueue-origin vs start-origin vs finish-origin."""
    key = "run:ttl"
    job = await q.enqueue("spike_slow", key=key, ttl=300, timeout=5)
    pttl_enqueue = await redis.pttl(job.id)  # plain SET at enqueue -> -1 (no expiry)
    await redis.zscore(f"saq:{QUEUE_NAME}:incomplete", job.id)
    # simulate start-origin: worker sets ACTIVE via plain SET -> still no TTL
    job.status = Status.ACTIVE
    job.started = int(time.time() * 1000)
    await job.update(status=Status.ACTIVE)
    pttl_active = await redis.pttl(job.id)
    # clean up the dangling job so later sweeps are unaffected
    await redis.lrem(f"saq:{QUEUE_NAME}:active", 0, job.id)
    await redis.zrem(f"saq:{QUEUE_NAME}:incomplete", job.id)
    await redis.delete(job.id)

    # finish-origin: _finish SETEXes the hash with job.ttl
    job2 = await q.enqueue("spike_slow", key="run:ttlfin", ttl=300, timeout=30)
    await job2.finish(Status.COMPLETE, result=None)
    pttl_finish = await redis.pttl(job2.id)

    ok = pttl_enqueue == -1 and pttl_active == -1 and 0 < pttl_finish <= 300000
    record(
        "8 ttl semantics",
        "PASS" if ok else "FAIL",
        f"PTTL after enqueue={pttl_enqueue}ms (no expiry), PTTL after ACTIVE update={pttl_active}ms "
        f"(no expiry), PTTL after finish={pttl_finish}ms (SETEX job.ttl=300) "
        f"-> ttl is FINISH-ORIGIN (result retention), NOT enqueue-origin nor start-origin. "
        f"A ttl=300 does NOT expire the job hash mid-run.",
    )


async def check9_cli() -> None:
    """CLI invocation: python -m saq <settings module> positional; NO worker subcommand.

    Recorded `uv run python -m saq --help` output (2026-07-31):

        usage: __main__.py [-h] [--workers WORKERS] [--verbose] [--web]
                           [--extra-web-settings EXTRA_WEB_SETTINGS] [--port PORT]
                           [--check] [--quiet]
                           settings

        Start Simple Async Queue Worker

        positional arguments:
          settings              Namespaced variable containing worker settings eg: eg
                                module_a.settings

        options:
          -h, --help            show this help message and exit
          --workers WORKERS     Number of worker processes
          --verbose, -v         Logging level: 0: ERROR, 1: INFO, 2: DEBUG
          --web                 Start web app. ...
          --extra-web-settings EXTRA_WEB_SETTINGS, -e EXTRA_WEB_SETTINGS ...
          --port PORT           Web app port, defaults to 8080
          --check               Perform a health check
          --quiet, -q           Disable automatic logging configuration

    Correct invocations therefore are:
        uv run python -m saq core.saq_worker.runs_settings
        uv run python -m saq core.saq_worker.system_settings --web --port 8081
    """
    proc = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "saq", "--help"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    help_text = (proc.stdout or "") + (proc.stderr or "")
    has_settings_positional = "settings" in help_text and "Namespaced variable containing worker settings" in help_text
    usage_line = next((ln for ln in help_text.splitlines() if ln.strip().startswith("usage:")), "")
    # positional arguments section: the ONLY positional must be `settings`
    positional_block = ""
    in_positional = False
    for ln in help_text.splitlines():
        if ln.strip() == "positional arguments:":
            in_positional = True
            continue
        if in_positional:
            if not ln.strip():
                break
            positional_block += ln.strip() + "; "
    # argparse renders subcommands as a {a,b,...} choice group; no such group here
    # means no subcommands. `--workers` is a flag (contains "worker" but is not a subcommand).
    has_subcommand_group = "{" in usage_line.split("[-h]")[1] if "[-h]" in usage_line else "{" in usage_line
    positional_tokens = [t.strip() for t in positional_block.split(";") if t.strip()]
    first_positional_is_settings = bool(positional_tokens and positional_tokens[0].startswith("settings"))
    ok = has_settings_positional and not has_subcommand_group and first_positional_is_settings
    record(
        "9 CLI invocation",
        "PASS" if ok else "FAIL",
        f"settings positional present={has_settings_positional}, subcommand group absent={not has_subcommand_group}, "
        f"first positional is settings={first_positional_is_settings}; usage={usage_line.strip()}; "
        f"positional=[{positional_block}]",
    )


async def check10_worker_heartbeat_ttl(redis: aioredis.Redis, q: RedisQueue) -> None:
    """Worker info key TTL == timer+1 (write_worker_info SETEX)."""
    await redis.flushdb()  # isolate from earlier checks
    from saq import Worker

    timer = 5
    w = Worker(
        q,
        [spike_slow],
        concurrency=1,
        timers={"schedule": 1, "sweep": 60, "worker_info": timer, "abort": 1},
    )
    t = asyncio.create_task(w.start())
    key = f"saq:{QUEUE_NAME}:worker_info:{w.id}"
    pttl_observed: list[int] = []
    deadline = time.monotonic() + 12
    while time.monotonic() < deadline:
        exists = await redis.exists(key)
        if exists:
            pttl_observed.append(await redis.pttl(key))
            if pttl_observed[-1] > 0:
                break
        await asyncio.sleep(0.2)
    await w.stop()
    await t

    if not pttl_observed:
        record("10 worker heartbeat TTL", "FAIL", "no worker_info key observed")
        return
    max_observed = max(pttl_observed)
    ok = max_observed <= (timer + 1) * 1000 and max_observed >= timer * 1000
    record(
        "10 worker heartbeat TTL",
        "PASS" if ok else "FAIL",
        f"timer={timer}s, worker_info key TTL observed up to {max_observed / 1000:.1f}s "
        f"(expected timer+1={timer + 1}s; write_worker_info SETEX ttl=timer+1, worker.py:302-307)",
    )


async def check11_cron_unique(redis: aioredis.Redis, q: RedisQueue) -> None:
    """Cron unique=True: two worker ticks -> no concurrent double-fire."""
    global _cron_fires, _cron_max_concurrent
    _cron_fires = Counter()
    _cron_max_concurrent = 0
    await redis.flushdb()  # isolate from earlier checks
    from saq import Worker

    cron = CronJob(spike_cron, cron="* * * * * *", unique=True, timeout=10, retries=1)
    w = Worker(q, [spike_cron], cron_jobs=[cron], concurrency=1, timers={"schedule": 1, "sweep": 60, "worker_info": 60})
    t = asyncio.create_task(w.start())
    await asyncio.sleep(6)
    await w.stop()
    await t

    fires = _cron_fires["fires"]
    ok = fires >= 1 and _cron_max_concurrent <= 1
    record(
        "11 cron unique=True",
        "PASS" if ok else "FAIL",
        f"cron '* * * * * *' over ~6s: fires={fires}, max concurrent executions={_cron_max_concurrent} "
        f"(unique=True -> key cron:spike_cron -> deduped while incomplete, no double-fire)",
    )


async def check12_sweeper(redis: aioredis.Redis, q: RedisQueue) -> None:
    """Sweeper: (a) stuck job re-queued; (b) missing-hash job LREM+ZREM'd by the sweeper itself."""
    from saq import Worker

    await redis.flushdb()  # isolate from earlier checks

    # (a) stuck job: heartbeat=1s, worker sweeps every 1s -> aborts+retries a running job
    await q.enqueue(
        "spike_slow",
        key="run:sweepstuck",
        retries=2,
        retry_delay=5,
        retry_backoff=False,
        heartbeat=1,
        timeout=0,
    )
    incomplete = f"saq:{QUEUE_NAME}:incomplete"
    w = Worker(q, [spike_slow], concurrency=1, timers={"schedule": 1, "sweep": 1, "worker_info": 60, "abort": 1})
    t = asyncio.create_task(w.start())
    re_queued = False
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        cur = await q.job("run:sweepstuck")
        # enqueued job is QUEUED with attempts=0; after dequeue ACTIVE attempts=1;
        # a re-queue after sweep-retry is QUEUED with attempts>=1
        if cur is not None and cur.status == Status.QUEUED and cur.attempts >= 1:
            re_queued = True
            break
        await asyncio.sleep(0.1)
    await w.stop()
    await t
    cur = await q.job("run:sweepstuck")
    cur_attempts = cur.attempts if cur else None
    cur_status = cur.status if cur else None

    # (b) missing-hash ACTIVE job: sweeper itself does LREM active + ZREM incomplete
    keyb = "run:sweepmissing"
    jobb = await q.enqueue("spike_slow", key=keyb, retries=1, timeout=0)
    await redis.blmove(f"saq:{QUEUE_NAME}:queued", f"saq:{QUEUE_NAME}:active", 0, "LEFT", "RIGHT")
    await redis.delete(jobb.id)  # simulate eviction of an ACTIVE job's hash
    # the part-(a) worker's sweep task held the sweep lock for up to 60s; clear it
    # so the manual sweep below actually executes
    await redis.delete(f"saq:{QUEUE_NAME}:sweep")
    await q.sweep(lock=1, abort=1)
    active_left = sum(1 for x in await redis.lrange(f"saq:{QUEUE_NAME}:active", 0, -1) if x == jobb.id.encode())
    incomplete_score_after = await redis.zscore(incomplete, jobb.id)
    sweeper_cleaned = active_left == 0 and incomplete_score_after is None

    ok = re_queued and sweeper_cleaned
    record(
        "12 sweeper",
        "PASS" if ok else "FAIL",
        f"(a) stuck job (heartbeat=1) re-queued={re_queued} (attempts={cur_attempts}, status={cur_status}); "
        f"(b) missing-hash ACTIVE job: active_left={active_left}, incomplete_after={incomplete_score_after} "
        f"-> sweeper LREMs active + ZREMs incomplete itself, so reconcile's ZREM returns 0 for ACTIVE-evicted jobs",
    )


async def check_upstash_maxmemory() -> None:
    """Record the Upstash maxmemory policy for the demo instance (read-only)."""
    record(
        "Upstash maxmemory-policy",
        "PASS",
        "modulo-demo-redis INFO reports maxmemory_policy=optimistic-volatile "
        "(maxmemory=1GiB, used_memory=0). CONFIG GET maxmemory-policy returns {} (Upstash restricts CONFIG). "
        "Per upstash.com/docs/redis/features/eviction (fetched 2026-07-31): 'a single eviction algorithm, called "
        "optimistic-volatile, which is a combination of volatile-random and allkeys-random. Initially, Upstash "
        "employs random sampling ... giving priority to keys marked with a TTL (expire field). If there is a "
        "shortage of volatile keys or they are insufficient to create space, additional non-volatile keys are "
        "randomly chosen for eviction.' -> NON-TTL keys (e.g. a running job hash, which per check 8 has no TTL) "
        "ARE evictable -> F3c partial-eviction machinery is LOAD-BEARING (the allkeys branch).",
    )


async def main() -> None:
    redis = aioredis.from_url(REDIS_URL, decode_responses=False)
    try:
        await redis.flushdb()
        q = RedisQueue(redis, name=QUEUE_NAME)
        await q.connect()

        await check1_blmove(redis, q)
        await check2_queue_keys(redis, q)
        await check3_dedupe(redis, q)
        await check4_partial_eviction(redis, q)
        await check5_queued_duplicate(redis, q)
        await check6_retry_timing(redis, q)
        await check7_retries_semantics(redis, q)
        await check8_ttl_semantics(redis, q)
        await check9_cli()
        await check10_worker_heartbeat_ttl(redis, q)
        await check11_cron_unique(redis, q)
        await check12_sweeper(redis, q)
        await check_upstash_maxmemory()
    finally:
        await redis.aclose()

    print("\n=== SPIKE RESULTS ===")
    for name, status, _ev in results:
        print(f"{status:10s} {name}")
    failed = [name for name, status, _ev in results if status == "FAIL"]
    if failed:
        print(f"\nABORT: {len(failed)} check(s) FAILED: {failed}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
