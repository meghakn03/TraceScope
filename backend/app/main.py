from __future__ import annotations

import json
import re
import sqlite3
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "data"
TRACE_DIR = DATA / "traces"
DB = DATA / "tracescope.sqlite3"
TRACE_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="TraceScope API", version="1.0.0", description="Local, deterministic trace replay and evaluation workbench")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"], allow_methods=["*"], allow_headers=["*"])


class ReplayRequest(BaseModel):
    source_run_id: str
    scenario: str | None = None


class Thresholds(BaseModel):
    repeat_window: int = Field(default=5, ge=2, le=50)
    retry_limit: int = Field(default=3, ge=1, le=20)
    slow_ms: int = Field(default=1500, ge=100, le=60000)
    high_steps: int = Field(default=12, ge=2, le=100)


class EvaluateRequest(BaseModel):
    agent_label: str = "baseline"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, scenario TEXT, started_at TEXT, agent_label TEXT, outcome TEXT, duration_ms INTEGER, event_count INTEGER, token_count INTEGER, trace_path TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS evaluations (id TEXT PRIMARY KEY, created_at TEXT, agent_label TEXT, result_json TEXT)")


def event(event_id: str, turn: str, kind: str, title: str, at: str, duration: int = 0, status: str = "success", tool: str | None = None, args: Any = None, result: Any = None, error: Any = None, tokens: int | None = None) -> dict:
    d = {"id": event_id, "parent_id": turn, "type": kind, "timestamp": at, "duration_ms": duration, "status": status, "title": title}
    if tool: d["tool_name"] = tool
    if args is not None: d["arguments"] = args
    if result is not None: d["result"] = result
    if error is not None: d["error"] = error
    if tokens is not None: d["token_count"] = tokens
    return d


def make_trace(scenario: str, label: str, variant: str = "standard") -> dict:
    base = datetime.now(timezone.utc).replace(microsecond=0)
    # Stable-looking timestamps keep the synthetic story legible; run IDs remain unique.
    base = base.replace(hour=10, minute=14, second=22)
    def ts(offset: int) -> str: return (base.replace(second=22) + __import__("datetime").timedelta(milliseconds=offset)).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    turn = "turn-1"
    ev: list[dict] = []
    if scenario == "success":
        ev = [event("e1", turn, "model_request", "Find the latest Python release", ts(0)), event("e2", turn, "tool_call", "Search release notes", ts(80), 142, tool="search", args={"query":"latest Python release"}), event("e3", turn, "tool_result", "Found Python 3.13.2 release notes", ts(222), result={"summary":"Python 3.13.2 released Feb 2025"}), event("e4", turn, "model_response", "Python 3.13.2 is the latest release in this synthetic dataset.", ts(310), 92, tokens=184)]
        outcome = "success"
    elif scenario == "loop":
        n = 1 if variant == "corrected" else 5
        ev.append(event("e1", turn, "model_request", "Calculate 18% of 240", ts(0)))
        for i in range(n):
            offset = 60 + i * 210
            ev.append(event(f"e{i+2}", turn, "tool_call", "Calculate percentage", ts(offset), 75, tool="calculator", args={"expression":"240 * 0.18"}))
            ev.append(event(f"e{i+2+n}", turn, "tool_result", "Result: 43.2", ts(offset + 75), result={"value":43.2}))
        ev.append(event(f"e{2+n*2}", turn, "model_response", "18% of 240 is 43.2.", ts(1200 if n == 5 else 520), 58, tokens=128))
        outcome = "success"
    elif scenario == "tool_error":
        ev = [event("e1", turn, "model_request", "Look up a missing record", ts(0)), event("e2", turn, "tool_call", "Search records", ts(95), 110, "error", "search", {"query":"record ZX-000"}), event("e3", turn, "tool_error", "Search service unavailable", ts(205), status="error", error={"code":"TOOL_UNAVAILABLE","message":"Synthetic search backend returned an error"}), event("e4", turn, "model_response", "I could not retrieve that record because search failed.", ts(300), 54, "partial", tokens=90)]
        outcome = "partial"
    else:
        ev = [event("e1", turn, "model_request", "Summarize the project status", ts(0)), event("e2", turn, "tool_call", "Search project updates", ts(40), 2380, "success", "search", {"query":"project status"}), event("e3", turn, "tool_result", "Found two synthetic updates", ts(2420), result={"matches":2}), event("e4", turn, "model_response", "Two updates were found; the latest says the project is on track.", ts(2520), 120, tokens=162)]
        outcome = "success"
    duration = max(int((__import__("datetime").datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) - __import__("datetime").datetime.fromisoformat(ev[0]["timestamp"].replace("Z", "+00:00"))).total_seconds() * 1000) + e["duration_ms"] for e in ev)
    return {"schema_version":"1.0", "run_id":str(uuid.uuid4()), "scenario":scenario, "started_at":ts(0), "agent_config":{"label":label,"mode":"synthetic","model":"none"}, "events":ev,"outcome":outcome,"review_notes":None,"metadata":{"synthetic":True,"duration_ms":duration,"token_count":sum(e.get("token_count",0) for e in ev)}}


def persist(trace: dict) -> dict:
    trace = redact(trace)
    path = TRACE_DIR / f"{trace['run_id']}.json"
    path.write_text(json.dumps(trace, indent=2))
    m = trace["metadata"]
    with db() as conn:
        conn.execute("INSERT OR REPLACE INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (trace["run_id"], trace["scenario"], trace["started_at"], trace["agent_config"]["label"], trace["outcome"], m["duration_ms"], len(trace["events"]), m.get("token_count", 0), str(path)))
    return trace


SENSITIVE_KEYS = {"api_key", "apikey", "authorization", "secret", "password", "access_token", "refresh_token", "cookie", "session_token"}
SECRET_PATTERNS = [(re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{12,}"), "Bearer [REDACTED]"), (re.compile(r"\b(?:sk|pk|key)-[A-Za-z0-9_-]{16,}\b"), "[REDACTED_KEY]")]


def redact(value: Any) -> Any:
    """Recursively remove credential fields and scrub recognizable credential strings."""
    if isinstance(value, dict):
        return {k: redact(v) for k, v in value.items() if k.lower() not in SENSITIVE_KEYS}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        for pattern, replacement in SECRET_PATTERNS:
            value = pattern.sub(replacement, value)
    return value


def seed() -> None:
    with db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0]
    if not count:
        for scenario, label in [("success","baseline"),("loop","baseline"),("tool_error","baseline"),("slow","baseline"),("loop","corrected")]: persist(make_trace(scenario,label,"corrected" if label == "corrected" else "standard"))


def read_trace(run_id: str) -> dict:
    with db() as conn: row = conn.execute("SELECT trace_path FROM runs WHERE id=?",(run_id,)).fetchone()
    if not row: raise HTTPException(404,"Run not found")
    return json.loads(Path(row["trace_path"]).read_text())


def run_row(row: sqlite3.Row) -> dict:
    return {k:row[k] for k in row.keys() if k != "trace_path"}


def detect(trace: dict, cfg: Thresholds | None = None) -> list[dict]:
    cfg = cfg or Thresholds()
    ev = trace["events"]; alerts=[]
    calls=[e for e in ev if e["type"] == "tool_call"]
    seen: dict[str,list[dict]] = {}
    positions: dict[str,int] = {}
    for position, e in enumerate(calls):
        key=json.dumps([e.get("tool_name"),e.get("arguments")],sort_keys=True)
        seen.setdefault(key,[]).append(e)
        positions[e["id"]]=position
        if e["duration_ms"] >= cfg.slow_ms: alerts.append({"id":"slow-"+e["id"],"severity":"warning","rule":f"Slow step: duration ≥ {cfg.slow_ms} ms","message":f"{e['tool_name']} took {e['duration_ms']:,} ms, above the configured threshold.","event_ids":[e["id"]]})
    for group in seen.values():
        in_window=[group[0]]
        for candidate in group[1:]:
            if positions[candidate["id"]]-positions[in_window[-1]["id"]] <= cfg.repeat_window: in_window.append(candidate)
        if len(in_window)>=2:
            alerts.append({"id":"repeat-"+group[0]["id"],"severity":"warning","rule":f"Repeated identical call within {cfg.repeat_window}-call window","message":f"{group[0].get('tool_name')} was called with the same arguments {len(in_window)} times. Repetition can be valid; review the context.","event_ids":[e["id"] for e in in_window]})
        if len(group)>cfg.retry_limit: alerts.append({"id":"retry-"+group[0]["id"],"severity":"error","rule":f"Retry count exceeds {cfg.retry_limit}","message":f"The same call occurred {len(group)} times, beyond the retry limit.","event_ids":[e["id"] for e in group]})
    # Flag a repeated multi-tool pattern, such as search → calculator → search → calculator.
    names=[e.get("tool_name", "") for e in calls]
    sequence_found=False
    for width in range(2, min(4, len(names)//2 + 1)):
        for start in range(0, len(names)-2*width+1):
            first=names[start:start+width]; second=names[start+width:start+2*width]
            if first==second and len(set(first))>1:
                ids=[e["id"] for e in calls[start:start+2*width]]
                alerts.append({"id":"sequence-"+ids[0],"severity":"warning","rule":f"Repeated tool sequence ({width} calls)","message":"A multi-tool sequence repeated consecutively. This can be valid; review the turns and results.","event_ids":ids})
                sequence_found=True; break
        if sequence_found: break
    for e in ev:
        if e["type"]=="tool_error" or e["status"] in ("error","timeout") and e["type"]=="tool_call": alerts.append({"id":"error-"+e["id"],"severity":"error","rule":"Tool failure or timeout","message":e.get("error",{}).get("message", e["title"]),"event_ids":[e["id"]]})
    if len(calls)>cfg.high_steps: alerts.append({"id":"steps","severity":"warning","rule":f"High tool-call count: > {cfg.high_steps}","message":f"Run contains {len(calls)} tool calls.","event_ids":[e["id"] for e in calls]})
    return alerts


@app.on_event("startup")
def startup() -> None: init_db(); seed()


@app.get("/api/health")
def health(): return {"status":"ok","storage":"sqlite + versioned JSON","external_services":False}


@app.get("/api/runs")
def list_runs(q: str = "", scenario: str = "all", outcome: str = "all"):
    with db() as conn: rows=conn.execute("SELECT * FROM runs ORDER BY started_at DESC").fetchall()
    data=[run_row(x) for x in rows]
    return [x for x in data if (not q or q.lower() in (x["scenario"]+x["agent_label"]+x["id"]).lower()) and (scenario=="all" or x["scenario"]==scenario) and (outcome=="all" or x["outcome"]==outcome)]


@app.get("/api/runs/{run_id}")
def get_run(run_id: str): return read_trace(run_id)


@app.post("/api/runs/{run_id}/replay")
def replay(run_id: str):
    original=read_trace(run_id); copy=json.loads(json.dumps(original)); copy["run_id"]=str(uuid.uuid4()); copy["started_at"]=now_iso(); copy["agent_config"]["label"] += " · replay"; copy["metadata"]["replay_of"]=run_id
    return persist(copy)


@app.post("/api/scenarios/{scenario}/run")
def run_scenario(scenario: str, label: str = "local-demo"):
    if scenario not in {"success","loop","tool_error","slow"}: raise HTTPException(404,"Unknown built-in scenario")
    return persist(make_trace(scenario,label))


@app.get("/api/runs/{run_id}/alerts")
def alerts(run_id: str, repeat_window: int = 5, retry_limit: int = 3, slow_ms: int = 1500, high_steps: int = 12):
    return detect(read_trace(run_id), Thresholds(repeat_window=repeat_window,retry_limit=retry_limit,slow_ms=slow_ms,high_steps=high_steps))


@app.post("/api/evaluations/run")
def evaluate(body: EvaluateRequest):
    cases=[("success","success",["search"]),("loop","success",["calculator"]),("tool_error","partial",["search"]),("slow","success",["search"])]
    results=[]
    for scenario, expected, allowed in cases:
        tr=persist(make_trace(scenario,body.agent_label,"corrected" if body.agent_label=="corrected" and scenario=="loop" else "standard")); calls=[e for e in tr["events"] if e["type"]=="tool_call"]
        checks={"expected_outcome":tr["outcome"]==expected,"allowed_tools":all(e.get("tool_name") in allowed for e in calls),"no_repeated_calls":len({json.dumps([e.get("tool_name"),e.get("arguments")],sort_keys=True) for e in calls})==len(calls),"step_limit":len(calls)<=5}
        results.append({"scenario":scenario,"run_id":tr["run_id"],"passed":all(checks.values()),"checks":checks,"tool_selection_accuracy":sum(e.get("tool_name") in allowed for e in calls)/len(calls) if calls else 1,"duration_ms":tr["metadata"]["duration_ms"],"token_count":tr["metadata"]["token_count"],"loop_rate":any(a["id"].startswith("repeat") for a in detect(tr))})
    durations=[r["duration_ms"] for r in results]
    summary={"scenario_count":len(results),"passed":sum(r["passed"] for r in results),"pass_rate":sum(r["passed"] for r in results)/len(results),"tool_selection_accuracy":sum(r["tool_selection_accuracy"] for r in results)/len(results),"failure_rate":sum(r["checks"]["expected_outcome"] is False for r in results)/len(results),"loop_rate":sum(r["loop_rate"] for r in results)/len(results),"median_duration_ms":median(durations),"token_usage":sum(r["token_count"] for r in results),"method":"Deterministic checks over four built-in synthetic scenarios; no model calls."}
    result={"id":str(uuid.uuid4()),"created_at":now_iso(),"agent_label":body.agent_label,"summary":summary,"results":results}
    with db() as conn: conn.execute("INSERT INTO evaluations VALUES (?, ?, ?, ?)",(result["id"],result["created_at"],body.agent_label,json.dumps(result)))
    return result


@app.get("/api/evaluations")
def evaluations():
    with db() as conn: rows=conn.execute("SELECT result_json FROM evaluations ORDER BY created_at DESC").fetchall()
    return [json.loads(x[0]) for x in rows]


@app.get("/api/compare")
def compare(left: str, right: str):
    a=read_trace(left); b=read_trace(right)
    def calls(t): return [e for e in t["events"] if e["type"]=="tool_call"]
    ac,bc=calls(a),calls(b); sig=lambda e:json.dumps([e.get("tool_name"),e.get("arguments")],sort_keys=True)
    ca,cb=Counter(map(sig,ac)),Counter(map(sig,bc))
    added=[e for e in bc if ca[sig(e)]==0 or sum(sig(x)==sig(e) for x in bc)>sum(sig(x)==sig(e) for x in ac)]
    removed=[e for e in ac if cb[sig(e)]==0 or sum(sig(x)==sig(e) for x in ac)>sum(sig(x)==sig(e) for x in bc)]
    return {"left":a,"right":b,"diff":{"added":added,"removed":removed,"left_call_order":[e.get("tool_name") for e in ac],"right_call_order":[e.get("tool_name") for e in bc],"new_errors":[e for e in b["events"] if e["type"]=="tool_error" and not any(x["type"]=="tool_error" and x["title"]==e["title"] for x in a["events"])],"duration_delta_ms":b["metadata"]["duration_ms"]-a["metadata"]["duration_ms"],"step_delta":len(bc)-len(ac),"token_delta":b["metadata"].get("token_count",0)-a["metadata"].get("token_count",0),"outcome_changed":a["outcome"]!=b["outcome"]}}


@app.post("/api/import")
def import_trace(trace: dict):
    if trace.get("schema_version")!="1.0" or not isinstance(trace.get("events"),list) or not trace.get("run_id"): raise HTTPException(422,"Expected trace schema version 1.0 with run_id and events")
    for e in trace["events"]:
        if not all(k in e for k in ("id","type","timestamp","duration_ms","status")): raise HTTPException(422,"Every event needs id, type, timestamp, duration_ms, and status")
        forbidden={"api_key","authorization","secret","password","access_token"}
        if forbidden.intersection(k.lower() for k in e): raise HTTPException(422,"Trace contains a sensitive field name")
    trace.setdefault("metadata",{}); trace["metadata"].setdefault("duration_ms",sum(e.get("duration_ms",0) for e in trace["events"])); trace["metadata"].setdefault("token_count",0)
    return persist(trace)


FRONTEND_DIST = ROOT / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=FRONTEND_DIST, html=True), name="frontend")
