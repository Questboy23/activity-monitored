#!/usr/bin/env python3
"""Tests for Activity Monitored 1.0. Run: python3 test_monitored.py. Uses a temp state folder; touches no real program."""
import importlib.util, json, os, subprocess, sys, tempfile, time

HERE = os.path.dirname(os.path.abspath(__file__))
tmp = tempfile.mkdtemp(); os.environ.update(MONITORED_DIR=tmp, MONITORED_NO_BANNER="1")
spec = importlib.util.spec_from_file_location("m", os.path.join(HERE, "monitored.py")); m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
ok = []
def check(name, cond): ok.append(bool(cond)); print(("PASS  " if cond else "FAIL  ") + name)

P = lambda cmd, uid=os.getuid(): dict(pid=99999, ppid=1, uid=uid, cpu=90.0, rss=1, cmd=cmd)
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

m.cmd_run(["--once"])
st = m.jload(m.F_STATUS, {})
check("one loop writes a status with disk, swap and pressure", all(k in st for k in ("disk_gb", "swap", "pressure", "lowered")))
check("nothing is deleted except by the 3 cache commands", [c[0] for c in m.CLEANERS] == ["uv", "pip3", "npm"])
src = open(os.path.join(HERE, "monitored.py")).read()
check("no kill, pause or file rewrite left in the loop", "SIGSTOP" not in src and "SIGKILL" not in src and "ditto" not in src)
print("%d of %d passed" % (sum(ok), len(ok))); sys.exit(0 if all(ok) else 1)
