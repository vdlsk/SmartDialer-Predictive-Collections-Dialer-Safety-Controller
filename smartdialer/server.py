"""Dashboard server: a simulation running on virtual time, plus a small JSON API and the static page.

    python -m smartdialer.server              # http://127.0.0.1:8765

One background thread owns the simulation (and therefore every database connection that steps it).
HTTP handlers never touch the engine: they read the last published state and queue commands.
POST /webhook/<provider> is the production-shaped entry for provider events; here it feeds the same
CallService.handle_event the simulator uses.
"""
import argparse
import json
import os
import queue
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .models import ProviderEvent
from .sim.harness import Simulation
from .sim.world import SCENARIOS

WEB = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "web")
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml", ".png": "image/png"}
STATE_KEYS = ["AVAILABLE", "RESERVED", "DIALING", "CONNECTED", "WRAP_UP", "PAUSED", "OFFLINE"]


class Runner(threading.Thread):
    def __init__(self, agents=100, mode="PREDICTIVE", scenario="B", workers=2, speed=10.0, seed=1):
        super().__init__(daemon=True)
        self.cfg = dict(agents=agents, mode=mode, scenario=scenario, workers=workers, seed=seed)
        self.speed = speed
        self.running = True
        self.cmds: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self.published: Dict[str, Any] = {}
        self.lock = threading.Lock()
        self.error: Optional[str] = None
        self.stop_flag = False
        self.sim = self._new_sim()
        self._publish()

    # ------------------------------------------------------------------ lifecycle
    def _new_sim(self) -> Simulation:
        c = self.cfg
        return Simulation(agents=c["agents"], mode=c["mode"], scenario=c["scenario"], workers=c["workers"], seed=c["seed"],
                          borrowers=max(20000, c["agents"] * 40), sample_s=1.0)

    def run(self):
        acc, last, last_pub = 0.0, time.time(), 0.0
        while not self.stop_flag:
            now = time.time()
            dt, last = min(0.25, now - last), now
            try:
                while True:
                    self._command(self.cmds.get_nowait())
            except queue.Empty:
                pass
            if self.running:
                acc += self.speed * dt
                try:
                    n = 0
                    while acc >= self.sim.tick_s and n < 40:
                        self.sim.step()
                        acc -= self.sim.tick_s
                        n += 1
                    if n == 40:
                        acc = 0.0                      # cannot keep up: drop the backlog instead of spiralling
                except Exception:
                    self.error = traceback.format_exc(limit=4)
                    self.running = False
                    self.sim.log("error", "simulation stopped: " + self.error.strip().splitlines()[-1])
            if now - last_pub >= 0.2:
                last_pub = now
                self._publish()
            time.sleep(0.02)

    # ------------------------------------------------------------------ commands (all run on this thread)
    def _command(self, c: Dict[str, Any]):
        sim, a = self.sim, c.get("action")
        try:
            if a == "play":
                self.running = True
                self.error = None
            elif a == "pause":
                self.running = False
            elif a == "speed":
                self.speed = max(0.5, min(100.0, float(c["value"])))
            elif a == "mode":
                if c["value"] in ("PROGRESSIVE", "PREDICTIVE"):
                    sim.set_mode(c["value"])
            elif a == "scenario":
                if c["value"] in SCENARIOS:
                    sim.set_scenario(c["value"])
            elif a == "rates":
                sim.set_rates(c.get("answer_rate"), c.get("talk_mean_s"))
            elif a == "drop_agents":
                sim.drop_agents(max(1, int(c["value"])))
            elif a == "restore_agents":
                sim.restore_agents()
            elif a == "add_agents":
                sim.add_agents(max(1, min(500, int(c["value"]))))
            elif a == "outage":
                sim.provider_outage(c["provider"], bool(c["on"]))
            elif a == "stress":
                self._stress(bool(c["on"]))
            elif a == "messy":
                self._messy(bool(c["on"]))
            elif a == "crash":
                sim.crash_worker(c.get("point", "after_place"))
            elif a == "restart_worker":
                sim.restart_worker()
            elif a == "campaign":
                sim.set_status("RUNNING" if c["value"] == "RUNNING" else "PAUSED")
                sim.log("control", f"campaign {c['value'].lower()}")
            elif a == "reset":
                for k in ("agents", "workers"):
                    if k in c:
                        self.cfg[k] = max(1, min(2000 if k == "agents" else 6, int(c[k])))
                self.cfg["mode"] = c.get("mode") or sim.db.conn().execute("SELECT mode FROM campaigns").fetchone()["mode"]
                self.cfg["scenario"] = c.get("scenario") or sim.scn.key
                self.cfg["seed"] += 1
                self.sim = self._new_sim()
                self.error = None
                self.sim.log("control", f"reset: {self.cfg['agents']} agents, {self.cfg['workers']} workers, scenario {self.cfg['scenario']}")
            elif a == "webhook":
                w = sim.live()[0]
                r = w.calls.handle_event(ProviderEvent(c["provider"], c["event_id"], c["call_id"], c["type"],
                                                       float(c.get("ts", 0)), float(c.get("talk_s", 0))))
                sim.log("webhook", f"{c['provider']} {c['type']} {c['call_id'][-6:]} -> {r}")
        except Exception as e:
            sim.log("error", f"{a}: {e}")

    def _stress(self, on):
        from .sim.world import stress
        from .providers.mock import provider_a, provider_b
        for p, fresh in ((self.sim.A, provider_a(self.sim.clock, self.sim.model)), (self.sim.B, provider_b(self.sim.clock, self.sim.model))):
            p.p.__dict__.update(fresh.p.__dict__)
            if on:
                stress(p.p)
        self.sim.log("fault" if on else "control", "providers degraded: slower, flaky, noisy" if on else "providers back to normal profiles")

    def _messy(self, on):
        p = self.sim.B.p
        p.dup_rate, p.reorder_rate = (0.5, 0.4) if on else (0.12, 0.08)
        p.reorder_delay_s = (1.0, 45.0) if on else (2.0, 15.0)
        self.sim.log("fault" if on else "control", "provider B duplicate + out-of-order storm" if on else "provider B back to normal noise")

    # ------------------------------------------------------------------ published state
    def _publish(self):
        try:
            st = self._build()
        except Exception:
            st = dict(self.published, error=traceback.format_exc(limit=3))
        with self.lock:
            self.published = st

    def _build(self) -> Dict[str, Any]:
        sim = self.sim
        db = sim.db.conn()
        w = sim.workers[-1]
        now = sim.clock.now()
        m = sim.metrics[-1] if sim.metrics else sim.sample()
        camp = db.execute("SELECT mode, status FROM campaigns").fetchone()
        series = sim.metrics[-300:]
        # rolling abandon rate over the controller's own window, from cumulative counters
        ser = []
        cum = [(x["t"], x["connected_total"], x["abandoned"]) for x in sim.metrics]
        j = 0
        for x in series:
            while cum[j][0] < x["t"] - 300:
                j += 1
            k = next(i for i, c in enumerate(cum) if c[0] == x["t"])
            dc, da = cum[k][1] - cum[j][1], cum[k][2] - cum[j][2]
            ser.append([x["t"], round(x["util"], 4), round(x["occ"], 4), x["unanswered_bound"] + x["unanswered_overflow"], x["available"],
                        round(da / (da + dc), 4) if da + dc > 0 else 0.0, x["p_true"], x["p_est"], x["p_plan"], x["online"], x["held"], x["initiated"], x["connected_total"]])
        agents = [[r["id"], r["state"]] for r in db.execute("SELECT id, state FROM agents ORDER BY id")]
        # one reading of "who is on a call": agents with a ringing call bound to them
        ringing = {r["agent_id"] for r in db.execute("SELECT agent_id FROM calls WHERE agent_id IS NOT NULL AND state IN ('INITIATED','RINGING')")}
        decs = []
        for r in db.execute("SELECT * FROM decisions ORDER BY id DESC LIMIT 40"):
            ex = json.loads(r["explain"] or "{}")
            decs.append(dict(id=r["id"], t=round(r["ts"] - sim.t0, 1), worker=r["worker"], mode=r["mode"], requested=r["requested"], approved=r["approved"],
                             bound=r["bound"], overflow=r["overflow"], verdict=r["verdict"], effective=r["effective_mode"],
                             text=ex.get("text", ""), rules=json.loads(r["reasons"] or "[]"), explain={k: v for k, v in ex.items() if k != "text"}))
        sc = w.safety
        with sim.db.tx() as c:
            ss = sc.state(c, sim.cid, now)
            mon = sc.monitor(c, sim.cid, now)
        health = w.router.health()
        tot = sim.totals()
        calls_by = {r["state"]: r["n"] for r in db.execute("SELECT state, COUNT(*) n FROM calls GROUP BY state")}
        return dict(
            t=round(sim.t, 1), running=self.running, speed=self.speed, error=self.error,
            campaign=dict(mode=camp["mode"], status=camp["status"], agents=m["online"]),
            scenario=sim.scn.key, scenario_note=sim.scn.note, scenarios={k: dict(title=v.title, note=v.note, answer=v.answer_rate if isinstance(v.answer_rate, float) else None,
                               talk=v.talk_mean_s if isinstance(v.talk_mean_s, float) else None) for k, v in SCENARIOS.items()},
            rates=dict(answer=round(m["p_true"], 3), talk=round(sim.model.talk_mean_s(now) if callable(sim.model.talk_mean_s) else sim.model.talk_mean_s, 0),
                       scripted=callable(sim.model.answer_rate)),
            now=m, series=ser, agents=agents, ringing=sorted(ringing),
            counts={s: sum(1 for a in agents if a[1] == s) for s in STATE_KEYS},
            decisions=decs,
            safety=dict(fallback_left=max(0, round(ss["fallback_until"] - now, 1)), shock_left=max(0, round(ss["shock_until"] - now, 1)),
                        abandon_rate=round(mon.abandon_rate, 4), answered_window=int(mon.answered_calls), abandoned_window=int(mon.abandoned),
                        limit=sc._cfg.max_abandon_rate, learning=mon.resolved_300 < sc._cfg.min_samples_predictive,
                        samples=int(mon.resolved_300), need=sc._cfg.min_samples_predictive),
            providers={n: dict(health=health[n], stats=dict(p.stats), outage=p.outage) for n, p in sim.providers.items()},
            workers=[dict(id=x.id, alive=x.alive) for x in sim.workers],
            totals={k: int(v) for k, v in tot.items()}, calls_by_state=calls_by, events=dict(sim.event_counts),
            log=sim.events_log[-40:], violations=sim.invariants() if int(sim.t) % 5 == 0 else [],
            recon={k: sum(x.recon.counters[k] for x in sim.workers) for k in w.recon.counters},
        )


class Handler(BaseHTTPRequestHandler):
    runner: Runner = None  # type: ignore

    def log_message(self, *a):
        pass

    def _send(self, code, body: bytes, ctype="application/json"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/api/state":
            with self.runner.lock:
                body = json.dumps(self.runner.published, default=str).encode()
            return self._send(200, body)
        if path in ("/", "/index.html"):
            path = "/index.html"
        f = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not f.startswith(WEB) or not os.path.isfile(f):
            return self._send(404, b'{"error":"not found"}')
        with open(f, "rb") as fh:
            return self._send(200, fh.read(), MIME.get(os.path.splitext(f)[1], "application/octet-stream"))

    def do_POST(self):
        try:
            n = int(self.headers.get("Content-Length", 0))
            data = json.loads(self.rfile.read(n) or b"{}")
        except Exception:
            return self._send(400, b'{"error":"bad json"}')
        if self.path == "/api/control":
            self.runner.cmds.put(data)
            return self._send(202, b'{"ok":true}')
        if self.path.startswith("/webhook/"):
            try:
                self.runner.cmds.put(dict(action="webhook", provider=self.path.split("/")[2], event_id=str(data["event_id"]),
                                          call_id=str(data["call_id"]), type=str(data["type"]), ts=data.get("ts", 0), talk_s=data.get("talk_s", 0)))
            except KeyError as e:
                return self._send(400, json.dumps({"error": f"missing {e}"}).encode())
            return self._send(202, b'{"ok":true}')
        return self._send(404, b'{"error":"not found"}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--agents", type=int, default=100)
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--mode", default="PREDICTIVE", choices=["PROGRESSIVE", "PREDICTIVE"])
    ap.add_argument("--scenario", default="B", choices=list(SCENARIOS))
    ap.add_argument("--speed", type=float, default=10.0, help="simulated seconds per real second")
    a = ap.parse_args()
    r = Runner(a.agents, a.mode, a.scenario, a.workers, a.speed)
    Handler.runner = r
    r.start()
    srv = ThreadingHTTPServer((a.host, a.port), Handler)
    print(f"SmartDialer dashboard on http://{a.host}:{a.port}   (Ctrl-C to stop)")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        r.stop_flag = True


if __name__ == "__main__":
    main()
