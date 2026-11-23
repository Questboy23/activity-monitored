#!/usr/bin/env python3
"""Tests for Activity Monitored 1.1. Run: python3 test_monitored.py. Uses a temp state folder; touches no real program."""
import importlib.util, json, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp(); os.environ.update(MONITORED_DIR=tmp, MONITORED_NO_BANNER="1")
spec = importlib.util.spec_from_file_location("m", os.path.join(HERE, "monitored.py")); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ok = []
m.jsave(m.F_SET, dict(clean=False))  # tests never run the real cache cleanup
def check(name, cond): ok.append(bool(cond)); print(("PASS  " if cond else "FAIL  ") + name)

P = lambda cmd, uid=os.getuid(): dict(pid=99999, ppid=1, uid=uid, cpu=90.0, rss=1, stat="S", cmd=cmd)
check("a user script can be lowered", m.lowerable(P("/Users/x/.venv/bin/python train.py"), None))
check("apps and their helpers are never lowered", not m.lowerable(P("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"), None))
check("system programs are never lowered", not m.lowerable(P("/usr/libexec/syspolicyd"), None))
check("Logic and Claude are never lowered", not m.lowerable(P("/Applications/Logic Pro X.app/x"), None) and not m.lowerable(P("/Users/x/.local/share/claude/versions/2/claude"), None))
check("other users' programs are never lowered", not m.lowerable(P("/Users/x/bin/job", uid=0), None))
check("protect list is honored", not m.lowerable(P("/Users/x/bin/render"), __import__("re").compile("render")))

check("first warning is sent", m.notify("t", "hello"))
check("second warning within 30 minutes is held back", not m.notify("t", "hello"))

# lower and restore a real child process of this test
hog = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
m.lower(dict(pid=hog.pid, ppid=os.getpid(), uid=os.getuid(), cpu=50.0, rss=1, cmd="hog"))
check("lowering is recorded with an undo timer", str(hog.pid) in m.jload(m.F_JOURNAL, {}))
m.restore(hog.pid, "test")
check("restore clears the record", str(hog.pid) not in m.jload(m.F_JOURNAL, {}))
hog.kill()

# pause and resume a real child process
st_of = lambda pid: subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
kid = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
time.sleep(0.3)
check("pause stops the program", m.pause(dict(pid=kid.pid, ppid=os.getpid(), uid=os.getuid(), cpu=0.0, rss=6e8, stat="S", cmd="bigjob"), "test") and "T" in st_of(kid.pid))
e = m.jload(m.F_JOURNAL, {}).get(str(kid.pid), {})
check("pause is recorded with its OS resume timer", e.get("kind") == "pause" and subprocess.run(["ps", "-p", str(e.get("timer"))], capture_output=True).returncode == 0)
m.restore(kid.pid, "test"); time.sleep(0.3)
check("resume runs it again and stops the timer", "T" not in st_of(kid.pid) and str(kid.pid) not in m.jload(m.F_JOURNAL, {}))
m.MAX_PAUSE = 2  # crash safety: the OS timer alone resumes it
m.pause(dict(pid=kid.pid, ppid=os.getpid(), uid=os.getuid(), cpu=0.0, rss=6e8, stat="S", cmd="bigjob"), "test")
m.jsave(m.F_JOURNAL, {})  # as if Activity Monitored crashed and lost its record
time.sleep(3.5)
check("OS timer resumes it even with no monitor", "T" not in st_of(kid.pid))
kid.kill(); m.MAX_PAUSE = 1800

check("memory size counts swapped memory too (physical footprint)", m.footprint(os.getpid()) > 1e6 and m.footprint(1) == 0)

check("local AI model runners are unloaded, the ollama server is left alone", m.OLLAMA.search("/opt/homebrew/Cellar/ollama/0.40.6/libexec/ollama runner --model qwen3.5:9b") and not m.OLLAMA.search("/opt/homebrew/bin/ollama serve"))

procs = {1: dict(pid=1, ppid=0, uid=0, cpu=0, rss=1, stat="S", cmd="/sbin/launchd"),
         10: dict(pid=10, ppid=1, uid=os.getuid(), cpu=0, rss=9e9, stat="S", cmd="/Applications/Claude.app/Contents/MacOS/Claude"),
         11: dict(pid=11, ppid=10, uid=os.getuid(), cpu=0, rss=8e9, stat="S", cmd="/Users/x/.venv/bin/python tool.py"),
         20: dict(pid=20, ppid=1, uid=os.getuid(), cpu=0, rss=7e9, stat="S", cmd="/Applications/Safari.app/Contents/MacOS/Safari"),
         30: dict(pid=30, ppid=1, uid=os.getuid(), cpu=0, rss=3e9, stat="S", cmd="/Users/x/.venv/bin/python train.py"),
         31: dict(pid=31, ppid=1, uid=os.getuid(), cpu=0, rss=5e9, stat="S", cmd="/Applications/Slack.app/Contents/MacOS/Slack"),
         32: dict(pid=32, ppid=1, uid=os.getuid(), cpu=0, rss=6e9, stat="S", cmd="/Applications/Logic Pro X.app/Contents/MacOS/Logic Pro X"),
         33: dict(pid=33, ppid=1, uid=os.getuid(), cpu=0, rss=4e9, stat="T", cmd="/Users/x/bin/stopped-by-you"),
         34: dict(pid=34, ppid=1, uid=os.getuid(), cpu=0, rss=1e8, stat="S", cmd="/Users/x/bin/small")}
order = [p["pid"] for p in m.pausable(procs, None, front=20)]
check("pause order: scripts first, then background apps", order == [30, 31])
check("never paused: front app, Claude and its tools, Logic, already-stopped, small", not set(order) & {10, 11, 20, 32, 33, 34})
m.jsave(m.DIR + "/hold.json", dict(until=time.time() + 600))
check("no new pauses for 10 minutes after you resume by hand", m.pausable(procs, None, front=20) == [])
os.remove(m.DIR + "/hold.json")

# end to end: real loop, fake critical pressure, a real 600 MB program started outside Claude's process tree
fp = os.path.join(tmp, "pressure"); open(fp, "w").write("4")
mark = "monitored_e2e_%d" % os.getpid()
subprocess.run(["sh", "-c", "%s -c 'import time; b=bytearray(600*1024*1024); b[::4096]=b\"x\"*len(b[::4096]); time.sleep(300)' %s >/dev/null 2>&1 &" % (sys.executable, mark)])  # not /usr/bin: SYSTEM_PATH skips those
time.sleep(2)
big = int(subprocess.run(["pgrep", "-f", mark], capture_output=True, text=True).stdout.split()[0])
# MONITORED_ONLY: only the test program may be paused on this real Mac
mon = subprocess.Popen([sys.executable, os.path.join(HERE, "monitored.py"), "run"], env=dict(os.environ, MONITORED_FAKE_PRESSURE=fp, MONITORED_TICK="0.5", MONITORED_ONLY=mark))
def wait_for(cond, secs=15):
    end = time.time() + secs
    while time.time() < end and not cond(): time.sleep(0.25)
    return cond()
check("critical pressure pauses the big program", wait_for(lambda: "T" in st_of(big)))
open(fp, "w").write("1")
check("normal pressure for 6 ticks resumes it", wait_for(lambda: "T" not in st_of(big)))
mon.terminate(); mon.wait(); subprocess.run(["kill", str(big)])

m.cmd_run(["--once"])
st = m.jload(m.F_STATUS, {})
check("one loop writes a status with disk, swap and pressure", all(k in st for k in ("disk_gb", "swap", "pressure", "lowered")))
check("nothing is deleted except by the 3 cache commands", [c[0] for c in m.CLEANERS] == ["uv", "pip3", "npm"])
src = open(os.path.join(HERE, "monitored.py")).read()
check("it never kills or rewrites: no SIGKILL/SIGTERM on programs, no file rewrite", "os.kill(pid, 9)" not in src and "SIGTERM" not in src and "ditto" not in src)
print("%d of %d passed" % (sum(ok), len(ok))); sys.exit(0 if all(ok) else 1)
