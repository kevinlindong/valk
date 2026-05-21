"""Probe exchange server for undocumented endpoints, hidden fields,
and other competitive intel that could narrow our prior on (a, w) or
otherwise leak information beyond what's in the docs.

Uses OUR API key only. Light request volume (no flooding). Logs every
non-trivial response so we can grep through it after.

Run:
    python day1/probe_server.py
    python day1/probe_server.py --out day1/probe_server_<ts>.json
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from typing import Any, Dict, List, Optional

import requests

try:
    import websockets
except Exception:
    websockets = None  # WS probing optional

URL = "http://192.168.50.167:8000"
API_KEY = "intern2-KEVD"

# Candidate undocumented paths. Common framework / admin / debug routes.
CANDIDATE_PATHS = [
    # Admin / debug / dev
    "/admin", "/admin/", "/api/admin", "/api/debug", "/api/dev",
    "/api/internal", "/api/secret", "/api/private", "/api/_debug",
    "/api/_dev", "/api/_internal", "/api/raw",
    # Health / metrics / info
    "/health", "/healthz", "/readyz", "/livez", "/status",
    "/metrics", "/api/metrics", "/api/health",
    "/version", "/api/version", "/api/info",
    # OpenAPI / docs
    "/docs", "/redoc", "/openapi.json", "/swagger", "/swagger.json",
    "/api/docs", "/api/openapi.json", "/api/schema",
    # FastAPI / common framework
    "/__debug__/", "/.well-known/openapi",
    # Game-state expansions
    "/api/game/full", "/api/game/debug", "/api/game/state",
    "/api/game/raw", "/api/game/secret", "/api/game/params",
    "/api/game/seed", "/api/seed", "/api/rng",
    "/api/settlement", "/api/settlement/preview",
    "/api/settle", "/api/settle/preview",
    "/api/reveals", "/api/reveals/all",
    # Bot / player enumeration
    "/api/players", "/api/players/all", "/api/users", "/api/bots",
    "/api/keys", "/api/accounts",
    # Per-instrument deep
    "/api/instruments/A", "/api/instruments/A/params",
    "/api/instruments/A/secret", "/api/instruments/A/debug",
    # Direct truth probes
    "/api/truth", "/api/answer", "/api/oracle",
    "/api/a", "/api/w", "/api/params",
    # Files / config
    "/config", "/config.json", "/.env", "/static/",
    "/api/config", "/api/.env",
]

# WS endpoints to try
WS_CANDIDATES = [
    "/ws", "/ws/", "/ws/public", "/ws/admin", "/ws/debug",
    "/ws/observer", "/ws/spectator", "/ws/game", "/ws/all",
    "/ws/v2", "/ws/private/raw", "/ws/internal",
]

# Documented endpoints we want a FULL dump of (in case there are
# undocumented fields).
DOCUMENTED_GETS: List[str] = [
    "/api/game", "/api/instruments", "/api/positions",
    "/api/leaderboard", "/api/book?symbol=A&depth=20",
    "/api/orders",
]


def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"X-API-Key": API_KEY})
    return s


def probe_http(s: requests.Session) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for path in CANDIDATE_PATHS:
        url = f"{URL}{path}"
        for method in ("GET", "OPTIONS"):
            try:
                r = s.request(method, url, timeout=2.0)
            except Exception as e:
                out.append({"path": path, "method": method, "error": repr(e)})
                continue
            entry: Dict[str, Any] = {
                "path": path,
                "method": method,
                "status": r.status_code,
                "len": len(r.content),
                "ct": r.headers.get("content-type", ""),
            }
            # If we got something other than a clean 404/405, capture body
            if r.status_code not in (404, 405, 401, 403):
                body_preview = r.text[:2000]
                entry["body_preview"] = body_preview
            elif r.status_code in (401, 403):
                # Auth gate — interesting because the route exists
                entry["body_preview"] = r.text[:500]
                entry["note"] = "AUTH GATE — endpoint exists"
            out.append(entry)
            # Light pacing so we don't trip the rate limiter; the SDK uses
            # 18 req/s, this script uses raw session so we throttle ourselves.
            time.sleep(0.05)
    return out


def dump_documented(s: requests.Session) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for path in DOCUMENTED_GETS:
        url = f"{URL}{path}"
        try:
            r = s.get(url, timeout=2.0)
            ct = r.headers.get("content-type", "")
            entry: Dict[str, Any] = {"status": r.status_code, "ct": ct,
                                     "len": len(r.content)}
            if "json" in ct.lower():
                try:
                    entry["body"] = r.json()
                except Exception:
                    entry["body_text"] = r.text[:5000]
            else:
                entry["body_text"] = r.text[:5000]
            out[path] = entry
        except Exception as e:
            out[path] = {"error": repr(e)}
        time.sleep(0.05)
    return out


def probe_methods(s: requests.Session) -> List[Dict[str, Any]]:
    """Try PUT/PATCH on /api/game and similar — sometimes admin verbs are
    open on otherwise-readonly routes."""
    out = []
    for path in ("/api/game", "/api/instruments", "/api/instruments/A",
                 "/api/positions", "/api/leaderboard"):
        for method in ("PUT", "PATCH", "DELETE", "POST"):
            url = f"{URL}{path}"
            try:
                r = s.request(method, url, timeout=2.0)
            except Exception as e:
                continue
            if r.status_code not in (404, 405):
                out.append({"path": path, "method": method,
                            "status": r.status_code,
                            "body_preview": r.text[:500]})
            time.sleep(0.05)
    return out


def probe_ws_topics() -> List[Dict[str, Any]]:
    if websockets is None:
        return [{"error": "websockets module unavailable"}]

    async def _try_one(ws_url: str, timeout: float = 2.0) -> Dict[str, Any]:
        try:
            async with websockets.connect(ws_url, ping_interval=None,
                                          open_timeout=timeout) as ws:
                # Read a few messages (or timeout)
                msgs: List[Any] = []
                t_end = time.time() + 1.5
                while time.time() < t_end and len(msgs) < 8:
                    try:
                        raw = await asyncio.wait_for(
                            ws.recv(), timeout=max(0.1, t_end - time.time()))
                    except asyncio.TimeoutError:
                        break
                    except Exception as e:
                        msgs.append({"recv_err": repr(e)})
                        break
                    try:
                        msgs.append(json.loads(raw))
                    except Exception:
                        msgs.append(raw[:400])
                return {"path": ws_url, "status": "open", "msgs": msgs}
        except Exception as e:
            return {"path": ws_url, "status": "fail", "error": repr(e)}

    async def _run() -> List[Dict[str, Any]]:
        ws_base = URL.replace("http://", "ws://").replace("https://", "wss://")
        results = []
        for path in WS_CANDIDATES:
            for suffix in ("", f"?api_key={API_KEY}"):
                u = f"{ws_base}{path}{suffix}"
                r = await _try_one(u)
                results.append(r)
                await asyncio.sleep(0.1)
        return results

    return asyncio.run(_run())


def field_diff(observed: Dict[str, Any], known: set[str]) -> List[str]:
    """Recursively collect dict keys that aren't in our `known` whitelist."""
    novel: List[str] = []

    def walk(node: Any, prefix: str = "") -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                p = f"{prefix}.{k}" if prefix else k
                if k not in known:
                    novel.append(p)
                walk(v, p)
        elif isinstance(node, list):
            for i, v in enumerate(node[:3]):
                walk(v, f"{prefix}[{i}]")
    walk(observed)
    return novel


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=None,
                        help="Where to write full results JSON.")
    args = parser.parse_args()
    if args.out is None:
        ts = time.strftime("%Y%m%d_%H%M%S", time.localtime())
        args.out = f"day1/probe_server_{ts}.json"

    s = _session()
    print(f"[probe] target={URL}  key={API_KEY}")
    print(f"[probe] output -> {args.out}\n")

    print("== Step 1: dump documented endpoints in full ==")
    dump = dump_documented(s)
    for path, info in dump.items():
        print(f"  {path:<35s}  status={info.get('status')}  "
              f"len={info.get('len')}  ct={info.get('ct')}")
        body = info.get("body")
        if isinstance(body, dict):
            print(f"    top-level keys: {sorted(body.keys())}")
    print()

    print("== Step 2: probe undocumented HTTP paths ==")
    http_results = probe_http(s)
    # Filter and summarize: anything with status != 404 OR auth gate
    interesting = [r for r in http_results
                   if isinstance(r.get("status"), int)
                   and r["status"] not in (404,)]
    print(f"  total probes={len(http_results)}, "
          f"non-404 responses={len(interesting)}")
    for r in interesting:
        print(f"  {r['method']:<7s} {r['path']:<40s}  "
              f"status={r['status']}  len={r.get('len')}  "
              f"ct={r.get('ct', '')[:40]}")
        if r.get("note"):
            print(f"      NOTE: {r['note']}")
        if r.get("body_preview") and len(r["body_preview"]) < 200:
            print(f"      body: {r['body_preview']!r}")
    print()

    print("== Step 3: try non-GET methods on known endpoints ==")
    methods = probe_methods(s)
    if not methods:
        print("  (none — known endpoints reject all non-GET cleanly)")
    else:
        for m in methods:
            print(f"  {m['method']:<7s} {m['path']:<40s}  "
                  f"status={m['status']}  body={m['body_preview'][:200]!r}")
    print()

    print("== Step 4: try undocumented WS topics ==")
    ws_results = probe_ws_topics()
    opens = [w for w in ws_results if w.get("status") == "open"]
    print(f"  total tries={len(ws_results)}, opened={len(opens)}")
    for w in opens:
        print(f"  OPEN: {w['path']}")
        for m in (w.get("msgs") or [])[:3]:
            print(f"    msg: {m}")
    # also note any that responded with non-default error
    for w in ws_results:
        if w.get("status") == "fail":
            err = w.get("error", "")
            # Surface anything that's not the generic 404/connection-refused
            if "404" not in err and "refused" not in err.lower() \
                    and "rejected" not in err.lower():
                print(f"  unusual fail: {w['path']}  err={err[:150]}")
    print()

    # Persist full dump
    out_obj = {
        "ts": time.time(),
        "target": URL,
        "documented": dump,
        "http_probes": http_results,
        "method_probes": methods,
        "ws_probes": ws_results,
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w") as f:
        json.dump(out_obj, f, indent=2, default=str)
    print(f"[probe] full results -> {args.out}")


if __name__ == "__main__":
    main()
