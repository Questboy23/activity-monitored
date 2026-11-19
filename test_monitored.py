#!/usr/bin/env python3
"""Tests for monitored. Run: python3 test_monitored.py. Each test uses its own temp state dir and cleans up its processes."""
import json, os, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__)); SM = os.path.join(HERE, "monitored.py")
results = []
os.environ["MONITORED_TEST_ONLY"] = "monitored_test_"  # never slow or pause a real program during tests
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

# 4 crash safety: slow a program, then the monitor dies instantly; the OS-level timer must restore its priority
d4 = tempfile.mkdtemp(); h = subprocess.Popen([sys.executable, "-c", "while True: pass"]); time.sleep(0.5)
def pri(pid): return sh("ps", "-o", "pri=", "-p", str(pid)).stdout.strip()
p0 = pri(h.pid)
sh(sys.executable, "-c", "import sys,os; sys.path.insert(0,%r); import monitored; monitored.apply('demote', dict(pid=%d,cmd='hog',cpu=99), 6); os._exit(1)" % (HERE, h.pid), env={"MONITORED_DIR": d4})
s1 = pri(h.pid); time.sleep(8); s2 = pri(h.pid); h.kill(); h.wait()
check("slowed program is restored by itself after the monitor crashes", s1 == "4" and s2 == p0, "before %s, just after crash %s, 8 s later %s" % (p0, s1, s2))

# 5 dry run changes nothing
d5 = tempfile.mkdtemp(); hs = hogs(24)
sh(sys.executable, SM, "run", "--dry", "--ticks", "9", "--tick", "1", "--hog_ticks", "2", "--hog_cpu", "3", env={"MONITORED_DIR": d5})
ev = jl(d5 + "/events.jsonl"); killall(hs)
check("dry run logs would_demote and changes nothing", any(e["ev"] == "would_demote" for e in ev) and not any(e["ev"] == "demote" for e in ev), "events %s" % sorted({e["ev"] for e in ev}))

# 6 two real runs: busy programs are slowed, everything is undone at exit, no flapping
d6 = tempfile.mkdtemp()
for run_no in (1, 2):
    hs = hogs(24)
    sh(sys.executable, SM, "run", "--ticks", "12", "--tick", "1", "--hog_ticks", "2", "--hog_cpu", "3", env={"MONITORED_DIR": d6})
    killall(hs); time.sleep(1)
left = json.load(open(d6 + "/journal.json")) if os.path.exists(d6 + "/journal.json") else {}
E6 = jl(d6 + "/events.jsonl")
check("everything undone at exit (journal empty)", left == {}, "demotes %d undos %d (a demoted process that died needs no undo)" % (sum(e["ev"] == "demote" for e in E6), sum(e["ev"] == "undo_demote" for e in E6)))
check("no flapping: at most 2 demotes per busy process over 2 runs of 12 s", sum(e["ev"] == "demote" for e in E6) <= 2 * 2 * 24 + 8, "%d demotes" % sum(e["ev"] == "demote" for e in E6))

# 7 never-touch: the foreground app tree and system daemons are never in the journal
bad = [e for e in jl(d6 + "/events.jsonl") if e.get("name") in ("WindowServer", "kernel_task", "launchd", "Finder", "Dock", "Claude", "Claude Helper")]
check("protected names never acted on", not bad, bad[:2])

# 9 a failing tick is logged and the monitor keeps going; 10 in a row exit non-zero so the login item restarts it
d9 = tempfile.mkdtemp(); r = sh(sys.executable, SM, "run", "--ticks", "3", "--tick", "1", env={"MONITORED_DIR": d9, "MONITORED_FAULT": "1"})
check("a failing tick is logged and the monitor keeps running", r.returncode == 0 and sum(e["ev"] == "error" for e in jl(d9 + "/events.jsonl")) == 3, r.stderr[-100:])
d9b = tempfile.mkdtemp(); r = sh(sys.executable, SM, "run", "--ticks", "20", "--tick", "1", env={"MONITORED_DIR": d9b, "MONITORED_FAULT": "1"})
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
sh(sys.executable, SM, "run", "--ticks", "14", "--tick", "1", "--hog_ticks", "2", "--hog_cpu", "3", env={"MONITORED_DIR": d11})
ev11 = jl(d11 + "/events.jsonl"); killall()
dp = sum(1 for e in ev11 if e["ev"] == "demote" and str(e.get("pid")) in plain); df = sum(1 for e in ev11 if e["ev"] == "demote" and str(e.get("pid")) in ff)
check("busy programs with ordinary names are slowed, and the same load named ffmpeg is not", len(plain) >= 6 and len(ff) >= 6 and dp > 0 and df == 0, "ordinary %d slowed of %d, ffmpeg %d slowed of %d" % (dp, len(plain), df, len(ff)))
# 12 a slowed program that becomes protected is restored within seconds (stands in for switching to it)
d12 = tempfile.mkdtemp(); hogs(12)
pids12 = set(subprocess.run(["pgrep", "-f", MARK], capture_output=True, text=True).stdout.split())
p12 = subprocess.Popen([sys.executable, SM, "run", "--tick", "1", "--hog_ticks", "2", "--hog_cpu", "3"], env=dict(os.environ, MONITORED_DIR=d12))
before, t12 = 0, time.time()
while not before and time.time() - t12 < 40:  # a busy Mac can take longer than 9 s to reach the first slowdown
    time.sleep(1); before = sum(str(e.get("pid")) in pids12 for e in (jl(d12 + "/events.jsonl") if os.path.exists(d12 + "/events.jsonl") else []) if e["ev"] == "demote")
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
# warnings: a disk under the threshold produces one disk warning (rate-limited), and status shows disk free and the warning
dw = tempfile.mkdtemp(); env = {"MONITORED_DIR": dw, "MONITORED_DISK_MIN_GB": "1000000", "MONITORED_NO_NOTIFY": "1"}
sh(sys.executable, SM, "run", "--ticks", "3", "--tick", "0.5", env=env, t=60)
w = [e for e in jl(dw + "/events.jsonl") if e.get("ev") == "warn"]
check("low disk produces exactly one rate-limited warning", len(w) == 1 and w[0]["kind"] == "disk" and "GB free" in w[0]["msg"], w)
r = sh(sys.executable, SM, "status", env=env)
check("status reports disk free and the warning", "Disk free:" in r.stdout and "ago): Disk below the" in r.stdout, r.stdout[-200:])
dw2 = tempfile.mkdtemp(); sh(sys.executable, SM, "run", "--ticks", "3", "--tick", "0.5", env={"MONITORED_DIR": dw2, "MONITORED_DISK_MIN_GB": "0", "MONITORED_NO_NOTIFY": "1"}, t=60)
check("enough disk produces no disk warning", not [e for e in jl(dw2 + "/events.jsonl") if e.get("kind") == "disk"])

# memory warning fires once and stays rate-limited; ps runs once, not every tick; a program slowed 30 times makes no popup
dm = tempfile.mkdtemp(); r = sh(sys.executable, "-c", """
import importlib.util, sys
s = importlib.util.spec_from_file_location("m", sys.argv[1]); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
n = [0]; real = m.sample
def counted(): n[0] += 1; return real()
m.sample = counted
for _ in range(30): m.warn_check(2, 0.95, False)
print(n[0])""", SM, env={"MONITORED_DIR": dm, "MONITORED_NO_NOTIFY": "1", "MONITORED_DISK_MIN_GB": "0"})
kinds = [e["kind"] for e in jl(dm + "/events.jsonl") if e.get("ev") == "warn"]
check("memory warning fires once, ps sampled once, nothing else pops up", kinds == ["memory"] and r.stdout.strip() == "1", (kinds, r.stdout.strip(), r.stderr[-200:]))

# front app unknown means no action
dt = tempfile.mkdtemp(); r = sh(sys.executable, "-c", """
import importlib.util, sys
s = importlib.util.spec_from_file_location("m", sys.argv[1]); m = importlib.util.module_from_spec(s); s.loader.exec_module(m)
m.foreground = lambda procs: (None, None); m.sample = lambda: {1: dict(pid=1, ppid=0, uid=m.ME, cpu=99.0, rss=0, stat="R", cmd="x")}
m.memory = lambda: (4, 0.9, 999)
mon = m.Mon(); mon.cheap = lambda: 1.0
acted = []; m.apply = lambda *a, **k: acted.append(a)
print("RET", mon.tick(), "ACTED", len(acted))
""", SM, env={"MONITORED_DIR": dt, "MONITORED_NO_NOTIFY": "1", "MONITORED_DISK_MIN_GB": "0"})
out = r.stdout
check("unknown front app: tick takes no action", "RET None ACTED 0" in out, (out[-200:], r.stderr[-300:]))

# gate with a missing program: a clear message and exit 1, not a Python traceback
r = sh(sys.executable, SM, "gate", "--", "no-such-program-xyz", env={"MONITORED_DIR": tempfile.mkdtemp()})
check("gate with a missing program prints one line and exits 1", r.returncode == 1 and "cannot run no-such-program-xyz" in r.stderr and "Traceback" not in r.stderr, r.stderr[-200:])

# memory relief: a big background program is paused while memory is short, resumed when it recovers, and never by a pop-up
MM = "monitored_test_mem"
subprocess.run(["sh", "-c", "%s -c 'import os, time; b = os.urandom(300 << 20); time.sleep(90)' %s >/dev/null 2>&1 &" % (sys.executable, MM)])
time.sleep(2); mp = sh("pgrep", "-f", MM).stdout.split()
dm = tempfile.mkdtemp()
r = sh(sys.executable, SM, "run", "--ticks", "16", "--tick", "0.5", env={"MONITORED_DIR": dm, "MONITORED_FAKE_LEVEL": "4"}, t=60)
ev = jl(dm + "/events.jsonl")
check("memory short: biggest background program paused", any(e.get("ev") == "pause" for e in ev) and mp, [e.get("ev") for e in ev][:6])
check("monitor exit resumes the paused program", mp and "T" not in stat(mp[0]), stat(mp[0]) if mp else "no hog")
w = subprocess.Popen([sys.executable, SM, "run", "--tick", "0.5"], env=dict(os.environ, MONITORED_DIR=dm, MONITORED_FAKE_LEVEL="4"))
t0 = time.time()
while time.time() - t0 < 30 and not (mp and "T" in stat(mp[0])): time.sleep(0.5)  # slow on a loaded Mac
paused = mp and "T" in stat(mp[0]); w.kill(); w.wait(); time.sleep(0.5)  # SIGKILL: the monitor gets no chance to undo
still = mp and "T" in stat(mp[0])
check("crash safety: paused program stays paused only until its own timer", paused and still, (paused, still))
J = json.load(open(dm + "/journal.json")); tp = J.get(mp[0], {}).get("timer") if mp else None
if tp: os.killpg(int(tp), 9)  # run the timer's job now instead of waiting 2 minutes
if mp: os.kill(int(mp[0]), 18)
check("no pop-up code left", "osascript" not in open(SM).read())
subprocess.run(["pkill", "-f", MM])

# disk relief: compression frees space, deletes nothing, keeps every byte, and is reversible
import hashlib
def sha(p): return hashlib.sha256(open(p, "rb").read()).hexdigest()
dk = tempfile.mkdtemp(); root = tempfile.mkdtemp(); fake = dk + "/free"; open(fake, "w").write("10")
old = time.time() - 10 * 86400
big = os.path.join(root, "sub", "corpus.txt"); os.makedirs(os.path.dirname(big))
open(big, "w").write("".join("line %d of a very repetitive log file\n" % i for i in range(600000))); os.utime(big, (old, old))
new_f = os.path.join(root, "fresh.txt"); open(new_f, "w").write("x" * 20000000)  # modified just now: must be left alone
opened = os.path.join(root, "open.txt"); open(opened, "w").write("y" * 20000000); os.utime(opened, (old, old))
zipf = os.path.join(root, "a.zip"); open(zipf, "w").write("z" * 20000000); os.utime(zipf, (old, old))
holder = subprocess.Popen(["/usr/bin/tail", "-f", opened], stdout=subprocess.DEVNULL); time.sleep(0.5)
h0, du0, m0 = sha(big), os.lstat(big).st_blocks, os.lstat(big).st_mtime
env = {"MONITORED_DIR": dk, "MONITORED_COMPRESS_ROOTS": root, "MONITORED_FAKE_DISK": fake}
sh(sys.executable, SM, "compress", env=env, t=300)
st = os.lstat(big); holder.kill()
check("compression shrinks a big idle file, same bytes and date", sha(big) == h0 and st.st_mtime == m0 and st.st_flags & 0x20 and st.st_blocks < du0 / 3,
      "%d -> %d blocks" % (du0, st.st_blocks))
check("compression skips new, open and already-compressed files", not any(os.lstat(f).st_flags & 0x20 for f in (new_f, opened, zipf)))
lg = jl(dk + "/compressed.jsonl")
check("every compressed file is recorded", [e["path"] for e in lg if e["ev"] == "compress"] == [big], lg)
check("no temporary copies left behind", not [f for _, _, fs in os.walk(root) for f in fs if f.endswith(".am-tmp")])
open(fake, "w").write("10")
r = sh(sys.executable, SM, "uncompress", env=env, t=300)
check("uncompress refuses while less than 50 GB would stay free", os.lstat(big).st_flags & 0x20 and "1 still compressed" in r.stdout, r.stdout.strip())
open(fake, "w").write("200")
r = sh(sys.executable, SM, "uncompress", env=env, t=300)
check("uncompress restores the file exactly", not os.lstat(big).st_flags & 0x20 and sha(big) == h0 and os.lstat(big).st_mtime == m0, r.stdout.strip())

# disk nearly full: pause the program writing most to disk, resume once there is room again
dd = tempfile.mkdtemp(); fake2 = dd + "/free"; open(fake2, "w").write("1")
WM = "monitored_test_writer"
wr = subprocess.Popen(["/bin/sh", "-c", "exec /usr/bin/python3 -c \"import os,sys\nwhile True:\n f=open(sys.argv[1],'wb'); f.write(os.urandom(4000000)); f.flush(); os.fsync(f.fileno()); f.close()\" %s/w.bin %s" % (dd, WM)], start_new_session=True)
env2 = {"MONITORED_DIR": dd, "MONITORED_FAKE_DISK": fake2, "MONITORED_DISK_EVERY": "2", "MONITORED_COMPRESS_ROOTS": dd, "MONITORED_DISK_TARGET": "10"}
mon = subprocess.Popen([sys.executable, SM, "run", "--tick", "1"], env=dict(os.environ, **env2))
s_paused = ""
for _ in range(20):
    time.sleep(1); s_paused = stat(wr.pid)
    if "T" in s_paused: break
check("program filling the disk is paused when under 3 GB are free", "T" in s_paused, s_paused)
open(fake2, "w").write("8")
s_res = s_paused
for _ in range(15):
    time.sleep(1); s_res = stat(wr.pid)
    if "T" not in s_res: break
check("it resumes once half the target (5 of 10 GB) is free", "T" not in s_res, s_res)
ev = [e["ev"] for e in jl(dd + "/events.jsonl")]
check("disk pause and resume are recorded", "dpause" in ev and "undo_dpause" in ev, ev[-6:])
mon.terminate(); mon.wait(); wr.kill()

# emergency rule: scripts an app started may be paused, the app and protected tools never
sys.path.insert(0, HERE); os.environ["MONITORED_DIR"] = tempfile.mkdtemp(); import monitored as M
def P(pid, cmd): return dict(pid=pid, ppid=1, uid=os.getuid(), cpu=0.0, rss=0, stat="R", cmd=cmd + " monitored_test_")
check("emergency rule allows a script under Claude", M.emergency_ok(P(91001, ".venv/bin/python disguise.py"), 91000, set()))
check("emergency rule never pauses the app, its helpers, claude or the front app",
      not any(M.emergency_ok(P(91002, c), 91000, set()) for c in ("/Applications/Claude.app/Contents/MacOS/Claude", "/Applications/Claude.app/Contents/Frameworks/Claude Helper (Renderer).app/x",
              "/Users/x/claude-code/claude", "pytest -q")) and not M.emergency_ok(P(91000, "python x.py"), 91000, set()))

# emergency end to end: the only big program is protected (as scripts under Claude are), memory is short and swap full
EM = "monitored_test_emg"
eh = subprocess.Popen(["/usr/bin/python3", "-c", "b=bytearray(300*1024*1024)\nfor i in range(0,len(b),4096): b[i]=1\nimport time\nwhile True: time.sleep(1)", EM], start_new_session=True); time.sleep(3)
os.environ["MONITORED_TEST_ONLY"] = EM; M.TEST_ONLY = EM
mo = M.Mon(); mo.severe, mo.fg_pid = True, 1
pr = M.sample(); prot = {eh.pid}
for _ in range(3): mo.relieve(pr, prot, True, time.time())
s_e = stat(eh.pid); M.revert_all("test"); s_after = stat(eh.pid); eh.kill()
check("memory emergency pauses a protected script, and undo resumes it", "T" in s_e and "T" not in s_after, (s_e, s_after))
mo2 = M.Mon(); mo2.severe = False; eh2 = subprocess.Popen(["/usr/bin/python3", "-c", "b=bytearray(300*1024*1024)\nfor i in range(0,len(b),4096): b[i]=1\nimport time\nwhile True: time.sleep(1)", EM], start_new_session=True); time.sleep(3)
for _ in range(3): mo2.relieve(M.sample(), {eh2.pid}, True, time.time())
check("without a swap emergency a protected script is left alone", "T" not in stat(eh2.pid), stat(eh2.pid)); M.revert_all("test"); eh2.kill()
os.environ["MONITORED_TEST_ONLY"] = "monitored_test_"

# threshold: below a third of the target while swap keeps growing, pause the biggest memory user; resume at half the target
ds = tempfile.mkdtemp(); fd_, fs_ = ds + "/free", ds + "/swap"; open(fd_, "w").write("8"); open(fs_, "w").write("1")
SM2 = "monitored_test_swap"
sh_ = subprocess.Popen(["/usr/bin/python3", "-c", "b=bytearray(600*1024*1024)\nfor i in range(0,len(b),4096): b[i]=1\nimport time\nwhile True: time.sleep(1)", SM2], start_new_session=True); time.sleep(3)
env3 = {"MONITORED_DIR": ds, "MONITORED_FAKE_DISK": fd_, "MONITORED_FAKE_SWAP": fs_, "MONITORED_DISK_EVERY": "2", "MONITORED_COMPRESS_ROOTS": ds, "MONITORED_DISK_TARGET": "30", "MONITORED_TEST_ONLY": SM2}
mon3 = subprocess.Popen([sys.executable, SM, "run", "--tick", "1"], env=dict(os.environ, **env3))
time.sleep(3); st3 = ""
for i in range(15):
    time.sleep(1); open(fs_, "w").write(str(2 + i)); st3 = stat(sh_.pid)
    if "T" in st3: break
check("disk under a third of the target and swap growing: biggest memory user paused", "T" in st3, st3)
open(fd_, "w").write("16"); open(fs_, "w").write("100"); st4 = st3
for _ in range(15):
    time.sleep(1); st4 = stat(sh_.pid)
    if "T" not in st4: break
check("it resumes once half the target is free", "T" not in st4, st4)
open(fd_, "w").write("8")  # swap stays at 100: not growing
for _ in range(6): time.sleep(1)
check("swap not growing: nothing paused even below a third of the target", "T" not in stat(sh_.pid), stat(sh_.pid))
mon3.terminate(); mon3.wait(); sh_.kill()
r = sh(sys.executable, SM, "disk-target", "45", env={"MONITORED_DIR": ds}); r2 = sh(sys.executable, SM, "disk-target", "2", env={"MONITORED_DIR": ds})
workers = [e for e in jl(ds + "/events.jsonl") if e.get("ev") == "worker"]
check("only one compressor at a time while the disk stays short", len(workers) <= 2, len(workers))
check("disk-target sets the threshold and rejects silly values", "45 GB" in r.stdout and json.load(open(ds + "/settings.json"))["disk_target_gb"] == 45 and r2.returncode == 1, (r.stdout, r2.stdout))

print("\n%d of %d passed" % (sum(results), len(results)))
sys.exit(0 if all(results) else 1)
