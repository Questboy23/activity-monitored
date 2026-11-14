#!/usr/bin/env python3
"""Tests for monitored v2. Run: python3 test_monitored.py [--real-ai]. Each test uses its own temp state dir and cleans up its processes."""
import json, os, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); SM = os.path.join(HERE, "monitored.py")
results = []
def sh(*a, env=None, t=120):
    return subprocess.run(a, capture_output=True, text=True, env=dict(os.environ, **(env or {})), timeout=t)
def check(name, cond, detail=""):
    results.append(bool(cond)); print(("PASS  " if cond else "FAIL  ") + name + ("  [" + str(detail) + "]" if detail != "" else ""), flush=True)
def stat(pid): return sh("ps", "-o", "stat=", "-p", str(pid)).stdout.strip()
MARK = "monitored_test_hog"
def hogs(n):  # started detached (parent = launchd), because monitored rightly protects everything started under Claude.app
    for _ in range(n): subprocess.run(["sh", "-c", "/usr/bin/python3 -c 'while True: pass' %s >/dev/null 2>&1 &" % MARK])
    time.sleep(0.5); return []
def killall(hs=None): subprocess.run(["pkill", "-f", MARK]); time.sleep(0.5)
def jl(path): return [json.loads(x) for x in open(path)] if os.path.exists(path) else []

# 1-3 gate (admission control for new work)
d = tempfile.mkdtemp()
t = time.time(); r = sh(sys.executable, SM, "gate", "--max-wait", "5", "--", "echo", "ran", env={"MONITORED_DIR": d, "MONITORED_FAKE_LEVEL": "4"}); dt = time.time() - t
check("gate holds while thrashing, then starts anyway at the max wait", 4.5 <= dt <= 10 and "ran" in r.stdout and "starting anyway" in r.stderr, "%.1f s" % dt)
t = time.time(); r = sh(sys.executable, SM, "gate", "--max-wait", "5", "--", "echo", "ran", env={"MONITORED_DIR": d, "MONITORED_FAKE_LEVEL": "1"}); dt = time.time() - t
check("gate starts at once when memory is calm", dt < 3 and "ran" in r.stdout, "%.1f s" % dt)
d3 = tempfile.mkdtemp()
w = subprocess.Popen([sys.executable, "-c", "import json,time,os\nd=os.environ['MONITORED_DIR'];t0=time.time()\nwhile time.time()-t0<20:\n json.dump({'ts':time.time(),'hold':time.time()-t0<4},open(d+'/status.json','w'));time.sleep(0.5)"], env=dict(os.environ, MONITORED_DIR=d3))
time.sleep(1); t = time.time(); r = sh(sys.executable, SM, "gate", "--max-wait", "30", "--", "echo", "ran", env={"MONITORED_DIR": d3}); dt = time.time() - t; w.kill()
check("gate releases as soon as the monitor clears the hold", 1.5 <= dt <= 8 and "ran" in r.stdout, "%.1f s" % dt)

# 4 crash safety: pause, then the program dies instantly; the OS-level timer must resume the process
d4 = tempfile.mkdtemp(); h = subprocess.Popen([sys.executable, "-c", "while True: pass"])
sh(sys.executable, "-c", "import sys,os; sys.path.insert(0,%r); import monitored; monitored.apply('pause', dict(pid=%d,cmd='hog',cpu=99), 6); os._exit(1)" % (HERE, h.pid), env={"MONITORED_DIR": d4})
s1 = stat(h.pid); time.sleep(8); s2 = stat(h.pid); h.kill(); h.wait()
check("paused process resumes by itself after the monitor crashes", "T" in s1 and "T" not in s2, "just after crash %s, 8 s later %s" % (s1, s2))

# 5 dry run changes nothing
d5 = tempfile.mkdtemp(); hs = hogs(24)
sh(sys.executable, SM, "run", "--ai", "off", "--dry", "--ticks", "9", "--tick", "1", "--hog_ticks", "2", "--hog_cpu", "3", env={"MONITORED_DIR": d5})
ev = jl(d5 + "/events.jsonl"); killall(hs)
check("dry run logs would_demote and changes nothing", any(e["ev"] == "would_demote" for e in ev) and not any(e["ev"] == "demote" for e in ev), "events %s" % sorted({e["ev"] for e in ev}))

# 6 agent loop (stub model): investigates, acts, writes advice, caches the decision; a second run replays it with 0 model calls
d6 = tempfile.mkdtemp(); out = []
for run_no in (1, 2):
    hs = hogs(24)
    if run_no == 2:  # replay check must not depend on what else is busy on this machine: every process name in the table already has a stored decision
        os.environ["MONITORED_DIR"] = d6; sys.path.insert(0, HERE); import monitored
        dec = json.load(open(d6 + "/decisions.json")); dec["seed"] = [dict(do="demote", name=n, why="seed") for n in {monitored.pname(p["cmd"]) for p in monitored.sample().values()} if n]
        json.dump(dec, open(d6 + "/decisions.json", "w"))
    sh(sys.executable, SM, "run", "--ai", "stub", "--ticks", "12", "--tick", "1", "--hog_ticks", "2", "--hog_cpu", "3", "--ai_every", "30" if run_no == 1 else "1e12", env={"MONITORED_DIR": d6})
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
    sh(sys.executable, SM, "run", "--ai", "auto", "--ticks", "45", "--tick", "2", "--hog_ticks", "2", "--hog_cpu", "3", "--ai_every", "0", env={"MONITORED_DIR": d8}, t=170)
    st = json.load(open(d8 + "/status.json")); killall(hs)
    dec = json.load(open(d8 + "/decisions.json")) if os.path.exists(d8 + "/decisions.json") else {}
    print("REAL MODEL: ai_calls %s, decisions %s, advice %s, wall %.0f s" % (st["ai_calls"], json.dumps(dec)[:600], st["advice"], time.time() - t))
    check("real model returned a valid plan", bool(dec))

# 9 a failing tick is logged and the monitor keeps going; 10 in a row exit non-zero so the login item restarts it
d9 = tempfile.mkdtemp(); r = sh(sys.executable, SM, "run", "--ai", "off", "--ticks", "3", "--tick", "1", env={"MONITORED_DIR": d9, "MONITORED_FAULT": "1"})
check("a failing tick is logged and the monitor keeps running", r.returncode == 0 and sum(e["ev"] == "error" for e in jl(d9 + "/events.jsonl")) == 3, r.stderr[-100:])
d9b = tempfile.mkdtemp(); r = sh(sys.executable, SM, "run", "--ai", "off", "--ticks", "20", "--tick", "1", env={"MONITORED_DIR": d9b, "MONITORED_FAULT": "1"})
check("10 failing ticks in a row exit with code 1", r.returncode == 1 and sum(e["ev"] == "error" for e in jl(d9b + "/events.jsonl")) == 10, "code %s" % r.returncode)
# 10 log rotation
d10 = tempfile.mkdtemp(); big = d10 + "/x.jsonl"; open(big, "w").write("x" * 4100000)
sh(sys.executable, "-c", "import sys; sys.path.insert(0, %r); import monitored; monitored.jl(%r, {'a': 1})" % (HERE, big), env={"MONITORED_DIR": d10})
check("a log over 4 MB is rotated", os.path.exists(big + ".1") and os.path.getsize(big) < 100)
# 11 work started on purpose is never slowed, and the same kind of busy program with an ordinary name is
d11 = tempfile.mkdtemp()
for tag in ("plain_hog", "ffmpeg"):
    for _ in range(8): subprocess.run(["sh", "-c", "/usr/bin/python3 -c 'while True: pass' %s %s >/dev/null 2>&1 &" % (tag, MARK)])
time.sleep(0.5)
def pids_of(tag): return set(subprocess.run(["pgrep", "-f", tag + " " + MARK], capture_output=True, text=True).stdout.split())
plain, ff = pids_of("plain_hog"), pids_of("ffmpeg")
sh(sys.executable, SM, "run", "--ai", "off", "--ticks", "14", "--tick", "1", "--hog_ticks", "2", "--hog_cpu", "3", env={"MONITORED_DIR": d11})
ev11 = jl(d11 + "/events.jsonl"); killall()
dp = sum(1 for e in ev11 if e["ev"] == "demote" and str(e.get("pid")) in plain); df = sum(1 for e in ev11 if e["ev"] == "demote" and str(e.get("pid")) in ff)
check("busy programs with ordinary names are slowed, and the same load named ffmpeg is not", len(plain) >= 6 and len(ff) >= 6 and dp > 0 and df == 0, "ordinary %d slowed of %d, ffmpeg %d slowed of %d" % (dp, len(plain), df, len(ff)))
# 12 a slowed program that becomes protected is restored within seconds (stands in for switching to it)
d12 = tempfile.mkdtemp(); hogs(12)
pids12 = set(subprocess.run(["pgrep", "-f", MARK], capture_output=True, text=True).stdout.split())
p12 = subprocess.Popen([sys.executable, SM, "run", "--ai", "off", "--tick", "1", "--hog_ticks", "2", "--hog_cpu", "3"], env=dict(os.environ, MONITORED_DIR=d12))
time.sleep(9); before = sum(str(e.get("pid")) in pids12 for e in jl(d12 + "/events.jsonl") if e["ev"] == "demote")
open(d12 + "/protect.txt", "w").write(MARK + "\n"); time.sleep(5)
ev12 = jl(d12 + "/events.jsonl"); left = json.load(open(d12 + "/journal.json")) if os.path.exists(d12 + "/journal.json") else {}
p12.terminate(); p12.wait(); killall()
check("a slowed program that becomes protected is restored within 5 s", before > 0 and any(e["ev"] == "undo_demote" and e.get("why") == "protected" for e in ev12) and not (set(left) & pids12), "slowed %d, still slowed %d" % (before, len(set(left) & pids12)))
# 13 installer: starts the login item, status works, uninstall removes everything (sandbox folder and label)
d13 = tempfile.mkdtemp(); e13 = {"MONITORED_DIR": d13, "MONITORED_LABEL": "com.monitored.test"}
r = sh("bash", os.path.join(HERE, "install.sh"), "--mode", "watch", env=e13)
up = sh("launchctl", "list", "com.monitored.test").returncode == 0
st = sh(sys.executable, d13 + "/monitored.py", "status", env=e13).stdout
sh(sys.executable, d13 + "/monitored.py", "uninstall", env=e13)
gone = sh("launchctl", "list", "com.monitored.test").returncode != 0 and not os.path.exists(os.path.expanduser("~/Library/LaunchAgents/com.monitored.test.plist"))
check("installer starts it, status reads it, uninstall removes everything", "watching only" in r.stdout and up and "Running." in st and gone, "up %s gone %s" % (up, gone))
# 14 program names ignore path arguments, and a copy job (rsync) is protected by default
os.environ["MONITORED_DIR"] = tempfile.mkdtemp(); sys.path.insert(0, HERE); import monitored as _m
names_ok = (_m.pname("gitleaks dir /Users/x/mirrors/edge --no-banner") == "gitleaks dir"
            and _m.pname("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome Helper (Renderer) --type=renderer") == "Google Chrome Helper (Renderer)"
            and _m.pname("/usr/bin/python3 /tmp/a/b.py -x") == "python3")
check("program names ignore path arguments", names_ok)
check("rsync is protected by default", bool(_m.PRIORITY.search("rsync -a /src /dst")))
# a refreshed demotion keeps exactly one undo timer, and undo cancels it (old timers used to restore the program early)
_t = subprocess.Popen(["sleep", "300"]); _p = dict(pid=_t.pid, cmd="sleep 300 " + MARK, cpu=50.0)
_m.apply("demote", _p, 300); _first = _m.journal()[str(_t.pid)]["timer"]; time.sleep(0.3)
_m.apply("demote", _p, 300); _second = _m.journal()[str(_t.pid)]["timer"]; time.sleep(0.3)
def _alive(x):
    try: os.kill(x, 0); return sh("ps", "-o", "stat=", "-p", str(x)).stdout.strip()[:1] not in ("", "Z")
    except OSError: return False
_one = (not _alive(_first)) and _alive(_second)
_m.undo(_t.pid, "test"); time.sleep(0.3); _t.kill()
check("a refreshed demotion keeps one undo timer and undo cancels it", _one and not _alive(_second), "first %s second %s" % (_first, _second))
print("\n%d of %d passed" % (sum(results), len(results)))
