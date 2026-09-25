"""Wave 0: swarm_enabled shared-state race.

``POST /api/chat`` mutates the SHARED ``settings.swarm_enabled`` around each
run (set -> run -> restore). Two concurrent requests with opposite flags
interleave, so one observes the other's value. Both tests FAIL before the
fix by design: the first proves the race exists, the second pins the shape
of the real fix (explicit per-call flag, never shared settings).
"""
import asyncio
import threading

from nimna.api.app import create_app
from nimna.core.agent import Agent
from nimna.core.state import AgentResult, RunStatus

MSG_A = "[swarm] task alpha"
MSG_B = "[swarm] task beta"


def test_concurrent_swarm_isolated(agent, settings, monkeypatch):
    """Two concurrent /api/chat calls with opposite flags stay isolated.

    Deterministic by construction: A is parked *before its read* while B's
    mutation is live, so today A unavoidably observes B's value. A naive
    asyncio.gather without the double gate could pass by luck (or by the
    finally-restore laundering the race); this cannot.
    """
    import httpx

    assert settings.swarm_enabled is False  # suite default; the race needs a known base
    observed: dict[str, bool] = {}
    gate = threading.Lock()
    arrivals = 0
    a_parked = threading.Event()
    b_parked = threading.Event()
    release_a = threading.Event()
    release_b = threading.Event()
    real_should_swarm = Agent._should_swarm

    def gated_should_swarm(self, user_message, *args, **kwargs):
        nonlocal arrivals
        with gate:
            arrivals += 1
            slot = arrivals
        if slot == 1:
            a_parked.set()  # A parked pre-read; its mutation (True) is live
            assert release_a.wait(timeout=15), "deadlock: A never released"
        elif slot == 2:
            b_parked.set()  # B parked pre-read; ITS mutation (False) is now live
            assert release_b.wait(timeout=15), "deadlock: B never released"
        # the read happens HERE, after the park — modelling a slow read
        decision = real_should_swarm(self, user_message, *args, **kwargs)
        with gate:
            observed[user_message] = decision
        return decision

    def wait_until(pred, timeout, what):
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if pred():
                return
            time.sleep(0.005)
        raise AssertionError(f"timeout waiting for: {what}")

    monkeypatch.setattr(Agent, "_should_swarm", gated_should_swarm)
    app = create_app(settings, agent=agent)
    headers = {"X-Nimna-Key": agent.settings.api_keys[0]}

    async def main():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://t", headers=headers
        ) as client:
            task_a = asyncio.create_task(
                client.post("/api/chat", json={"message": MSG_A, "swarm": True})
            )
            try:
                await asyncio.to_thread(wait_until, a_parked.is_set, 10, "A parked")
                task_b = asyncio.create_task(
                    client.post("/api/chat", json={"message": MSG_B, "swarm": False})
                )
                # B has mutated (False live) and is parked pre-read; A still parked.
                await asyncio.to_thread(wait_until, b_parked.is_set, 10, "B parked")
                release_a.set()  # A reads NOW — False is live. Guaranteed misread today.
                await asyncio.to_thread(
                    wait_until, lambda: MSG_A in observed, 10, "A observed"
                )
                release_b.set()  # B reads False — correct for B either way.
                resp_a = await task_a
                resp_b = await task_b
                assert resp_a.status_code == 200
                assert resp_b.status_code == 200
            finally:
                release_a.set()  # never strand a parked worker on failure
                release_b.set()

    asyncio.run(main())
    assert observed[MSG_A] is True, f"A saw {observed.get(MSG_A)} — B's value won"
    assert observed[MSG_B] is False


def test_agent_run_accepts_swarm_argument(agent, monkeypatch):
    """Agent.run routes on an explicit per-call flag, never shared settings.

    Guards against the superficial fix (moving the field but still reading
    shared state): the swarm path must trigger on the argument, and
    settings.swarm_enabled must stay untouched.
    """
    swarm_calls: list[str] = []

    def fake_run_swarm(self, user_message, session_id=None):
        swarm_calls.append(user_message)
        return AgentResult(
            run_id="r-swarm",
            session_id=session_id or "s",
            status=RunStatus.DONE,
            reply="swarmed",
        )

    monkeypatch.setattr(Agent, "run_swarm", fake_run_swarm)
    assert agent.settings.swarm_enabled is False
    out_true = agent.run("[swarm] do it", session_id="s1", swarm=True)
    out_false = agent.run("[swarm] do it", session_id="s2", swarm=False)
    assert swarm_calls == ["[swarm] do it"]  # only the True run took the swarm path
    assert out_true.reply == "swarmed" and out_false.status == RunStatus.DONE
    assert agent.settings.swarm_enabled is False  # shared state untouched
