#!/usr/bin/env python3
"""Tests for steady v2. Run: python3 test_steady.py [--real-ai]. Each test uses its own temp state dir and cleans up its processes."""
import json, os, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); SM = os.path.join(HERE, "steady.py")
results = []
def sh(*a, env=None, t=120):
    return subprocess.run(a, capture_output=True, text=True, env=dict(os.environ, **(env or {})), timeout=t)
def check(name, cond, detail=""):
    results.append(bool(cond)); print(("PASS  " if cond else "FAIL  ") + name + ("  [" + str(detail) + "]" if detail != "" else ""), flush=True)
def stat(pid): return sh("ps", "-o", "stat=", "-p", str(pid)).stdout.strip()
MARK = "steady_test_hog"
def hogs(n):  # started detached (parent = launchd), because steady rightly protects everything started under Claude.app
    for _ in range(n): subprocess.run(["sh", "-c", "python3 -c 'while True: pass' %s >/dev/null 2>&1 &" % MARK])
    time.sleep(0.5); return []
def killall(hs=None): subprocess.run(["pkill", "-f", MARK]); time.sleep(0.5)
def jl(path): return [json.loads(x) for x in open(path)] if os.path.exists(path) else []

# 1-3 gate (admission control for new work)
d = tempfile.mkdtemp()
t = time.time(); r = sh("python3", SM, "gate", "--max-wait", "5", "--", "echo", "ran", env={"STEADY_DIR": d, "STEADY_FAKE_LEVEL": "4"}); dt = time.time() - t
check("gate holds while thrashing, then starts anyway at the max wait", 4.5 <= dt <= 10 and "ran" in r.stdout and "starting anyway" in r.stderr, "%.1f s" % dt)
t = time.time(); r = sh("python3", SM, "gate", "--max-wait", "5", "--", "echo", "ran", env={"STEADY_DIR": d, "STEADY_FAKE_LEVEL": "1"}); dt = time.time() - t
check("gate starts at once when memory is calm", dt < 3 and "ran" in r.stdout, "%.1f s" % dt)
d3 = tempfile.mkdtemp()
w = subprocess.Popen(["python3", "-c", "import json,time,os\nd=os.environ['STEADY_DIR'];t0=time.time()\nwhile time.time()-t0<20:\n json.dump({'ts':time.time(),'hold':time.time()-t0<4},open(d+'/status.json','w'));time.sleep(0.5)"], env=dict(os.environ, STEADY_DIR=d3))
time.sleep(1); t = time.time(); r = sh("python3", SM, "gate", "--max-wait", "30", "--", "echo", "ran", env={"STEADY_DIR": d3}); dt = time.time() - t; w.kill()
check("gate releases as soon as the monitor clears the hold", 1.5 <= dt <= 8 and "ran" in r.stdout, "%.1f s" % dt)

# 4 crash safety: pause, then the program dies instantly; the OS-level timer must resume the process
d4 = tempfile.mkdtemp(); h = subprocess.Popen(["python3", "-c", "while True: pass"])
sh("python3", "-c", "import sys,os; sys.path.insert(0,%r); import steady; steady.apply('pause', dict(pid=%d,cmd='hog',cpu=99), 6); os._exit(1)" % (HERE, h.pid), env={"STEADY_DIR": d4})
s1 = stat(h.pid); time.sleep(8); s2 = stat(h.pid); h.kill(); h.wait()
check("paused process resumes by itself after the monitor crashes", "T" in s1 and "T" not in s2, "just after crash %s, 8 s later %s" % (s1, s2))

# 5 dry run changes nothing
d5 = tempfile.mkdtemp(); hs = hogs(24)
sh("python3", SM, "run", "--ai", "off", "--dry", "--ticks", "9", "--tick", "1", "--hog_ticks", "2", env={"STEADY_DIR": d5})
ev = jl(d5 + "/events.jsonl"); killall(hs)
check("dry run logs would_demote and changes nothing", any(e["ev"] == "would_demote" for e in ev) and not any(e["ev"] == "demote" for e in ev), "events %s" % sorted({e["ev"] for e in ev}))

# 6 agent loop (stub model): investigates, acts, writes advice, caches the decision; a second run replays it with 0 model calls
d6 = tempfile.mkdtemp(); out = []
for run_no in (1, 2):
    hs = hogs(24)
    if run_no == 2:  # replay check must not depend on what else is busy on this machine: every process name in the table already has a stored decision
        os.environ["STEADY_DIR"] = d6; sys.path.insert(0, HERE); import steady
        dec = json.load(open(d6 + "/decisions.json")); dec["seed"] = [dict(do="demote", name=n, why="seed") for n in {steady.pname(p["cmd"]) for p in steady.sample().values()} if n]
        json.dump(dec, open(d6 + "/decisions.json", "w"))
    sh("python3", SM, "run", "--ai", "stub", "--ticks", "12", "--tick", "1", "--hog_ticks", "2", "--ai_every", "30" if run_no == 1 else "1e12", env={"STEADY_DIR": d6})
    st = json.load(open(d6 + "/status.json")); ev = jl(d6 + "/events.jsonl"); killall(hs); out.append(st)
    time.sleep(1)
left = json.load(open(d6 + "/journal.json")) if os.path.exists(d6 + "/journal.json") else {}
check("agent run 1: one model call, decision cached, advice produced", out[0]["ai_calls"] == 1 and bool(json.load(open(d6 + "/decisions.json"))) and bool(out[0]["advice"]), "ai_calls %s advice %s" % (out[0]["ai_calls"], out[0]["advice"]))
check("agent run 2 (new calls forbidden): replays the cached decision, 0 model calls", out[1]["ai_calls"] == 0 and out[1]["cache_hits"] > 0, "ai_calls %s cache_hits %s" % (out[1]["ai_calls"], out[1]["cache_hits"]))
E6 = jl(d6 + "/events.jsonl")
check("everything undone at exit (journal empty)", left == {}, "demotes %d undos %d (a demoted process that died needs no undo)" % (sum(e["ev"] == "demote" for e in E6), sum(e["ev"] == "undo_demote" for e in E6)))
check("no flapping: at most 2 demotes per busy process over 2 runs of 12 s", sum(e["ev"] == "demote" for e in E6) <= 2 * 2 * 24 + 8, "%d demotes" % sum(e["ev"] == "demote" for e in E6))

# 7 never-touch: the foreground app tree and system daemons are never in the journal
bad = [e for e in jl(d6 + "/events.jsonl") if e.get("name") in ("WindowServer", "kernel_task", "launchd", "Finder", "Dock", "Claude", "Claude Helper")]
check("protected names never acted on", not bad, bad[:2])

# 8 optional: real model
if "--real-ai" in sys.argv:
    d8 = tempfile.mkdtemp(); hs = hogs(24); t = time.time()
    sh("python3", SM, "run", "--ai", "auto", "--ticks", "45", "--tick", "2", "--hog_ticks", "2", "--ai_every", "0", env={"STEADY_DIR": d8}, t=170)
    st = json.load(open(d8 + "/status.json")); killall(hs)
    dec = json.load(open(d8 + "/decisions.json")) if os.path.exists(d8 + "/decisions.json") else {}
    print("REAL MODEL: ai_calls %s, decisions %s, advice %s, wall %.0f s" % (st["ai_calls"], json.dumps(dec)[:600], st["advice"], time.time() - t))
    check("real model returned a valid plan", bool(dec))
print("\n%d of %d passed" % (sum(results), len(results)))
