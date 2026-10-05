"""Estado compartido entre réplicas: tickets WS de un solo uso, rate limiting y cupo de sesiones.

Redis en producción; implementación en memoria (un solo proceso) para desarrollo y tests.
"""
import json
import time
from typing import Protocol

from app.security.auth import Principal


class StateStore(Protocol):
    async def put_ticket(self, ticket: str, p: Principal, ttl: int, agent_id: str | None = None) -> None: ...
    async def take_ticket(self, ticket: str) -> tuple[Principal, str | None] | None: ...
    async def hit(self, scope: str, ident: str, limit: int, window_s: int) -> bool: ...
    async def acquire_session(self, org_id: str, session_id: str, limit: int, stale_after_s: int = 7200) -> bool: ...
    async def release_session(self, org_id: str, session_id: str) -> None: ...
    async def active_sessions(self, org_id: str) -> int: ...


class InMemoryStateStore:
    def __init__(self) -> None:
        self._tickets: dict[str, tuple[Principal, float]] = {}
        self._hits: dict[str, tuple[int, float]] = {}
        self._sessions: dict[str, dict[str, float]] = {}

    async def put_ticket(self, ticket, p, ttl, agent_id=None):
        self._tickets[ticket] = (p, agent_id, time.time() + ttl)

    async def take_ticket(self, ticket):
        entry = self._tickets.pop(ticket, None)  # un solo uso
        if not entry or entry[2] < time.time():
            return None
        return entry[0], entry[1]

    async def hit(self, scope, ident, limit, window_s):
        key = f"{scope}:{ident}"
        now = time.time()
        count, reset = self._hits.get(key, (0, now + window_s))
        if reset <= now:
            count, reset = 0, now + window_s
        count += 1
        self._hits[key] = (count, reset)
        return count <= limit

    async def acquire_session(self, org_id, session_id, limit, stale_after_s=7200):
        sess = self._sessions.setdefault(org_id, {})
        now = time.time()
        for sid in [s for s, t in sess.items() if t < now - stale_after_s]:
            del sess[sid]
        if len(sess) >= limit:
            return False
        sess[session_id] = now
        return True

    async def release_session(self, org_id, session_id):
        self._sessions.get(org_id, {}).pop(session_id, None)

    async def active_sessions(self, org_id):
        return len(self._sessions.get(org_id, {}))


# Comprobar cupo y registrar la sesión debe ser atómico: dos peticiones simultáneas no pueden pasar el límite.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1] - ARGV[4])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then return 0 end
redis.call('ZADD', KEYS[1], ARGV[1], ARGV[2])
redis.call('EXPIRE', KEYS[1], ARGV[4])
return 1
"""


class RedisStateStore:
    def __init__(self, client) -> None:  # redis.asyncio.Redis
        self.r = client
        self._acquire = client.register_script(_ACQUIRE_LUA)

    async def put_ticket(self, ticket, p, ttl, agent_id=None):
        await self.r.set(f"ticket:{ticket}", json.dumps({"p": p.__dict__, "agent_id": agent_id}), ex=ttl)

    async def take_ticket(self, ticket):
        raw = await self.r.getdel(f"ticket:{ticket}")  # atómico: un solo uso (Redis >= 6.2)
        if not raw:
            return None
        data = json.loads(raw)
        return Principal(**data["p"]), data["agent_id"]

    async def hit(self, scope, ident, limit, window_s):
        key = f"rl:{scope}:{ident}:{int(time.time() // window_s)}"
        pipe = self.r.pipeline()
        pipe.incr(key)
        pipe.expire(key, window_s)
        count, _ = await pipe.execute()
        return int(count) <= limit

    async def acquire_session(self, org_id, session_id, limit, stale_after_s=7200):
        res = await self._acquire(keys=[f"sessions:{org_id}"], args=[int(time.time()), session_id, limit, stale_after_s])
        return int(res) == 1

    async def release_session(self, org_id, session_id):
        await self.r.zrem(f"sessions:{org_id}", session_id)

    async def active_sessions(self, org_id):
        return int(await self.r.zcard(f"sessions:{org_id}"))
