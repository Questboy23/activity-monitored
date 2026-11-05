#!/usr/bin/env python3
"""A/B benchmark: a parallel test-like job (10 workers, named pytest_* so monitored treats it as priority work) competes with 24 busy background
processes on this Mac. Arms alternate OFF, ON, OFF, ON... Reports wall time of the job per arm. Synthetic overload = best case, not a typical day."""
import json, os, signal, statistics, subprocess, sys, tempfile, time
HERE = os.path.dirname(os.path.abspath(__file__))
SM = os.path.join(HERE, "monitored.py")
WORKER = "/tmp/pytest_bench_worker.py"
open(WORKER, "w").write("import sys\nn=int(sys.argv[1])\nx=0\nfor i in range(n): x+=i*i\n")
N_PER_ARM = int(sys.argv[1]) if len(sys.argv) > 1 else 4
def calibrate(secs):
    n = 3_000_000; t = time.time(); subprocess.run(["python3", WORKER, str(n)]); dt = time.time() - t
    return int(n * secs / dt)
def one(on, iters):
    d = tempfile.mkdtemp(prefix="sm_ab_")
    for _ in range(24): subprocess.run(["sh", "-c", "python3 -c 'while True: pass' monitored_bench_hog >/dev/null 2>&1 &"])  # detached from Claude.app's process tree, which monitored protects
    mon = subprocess.Popen(["python3", SM, "run", "--ai", "off", "--tick", "1", "--hog_ticks", "3"], env=dict(os.environ, MONITORED_DIR=d)) if on else None
    time.sleep(1.5)
    t0 = time.time()
    ws = [subprocess.Popen(["python3", WORKER, str(iters)]) for _ in range(10)]
    for w in ws: w.wait()
    dt = time.time() - t0
    if mon: mon.send_signal(signal.SIGTERM); mon.wait(15)
    subprocess.run(["pkill", "-f", "monitored_bench_hog"])
    ev = open(d + "/events.jsonl").read() if os.path.exists(d + "/events.jsonl") else ""
    left = json.load(open(d + "/journal.json")) if os.path.exists(d + "/journal.json") else {}
    time.sleep(2)
    return dt, ev.count('"ev":"demote"'), ev.count('"ev":"undo_demote"'), len(left)
if __name__ == "__main__":
    iters = calibrate(6)
    print("iterations per worker (about 6 s alone on a quiet core):", iters, flush=True)
    res = {"OFF": [], "ON": []}
    for i in range(N_PER_ARM):
        for arm in ("OFF", "ON"):
            dt, nd, nu, left = one(arm == "ON", iters)
            res[arm].append(dt)
            print("run %d %-3s  job wall time %.1f s  demotes %d undone %d left_in_journal %d" % (i + 1, arm, dt, nd, nu, left), flush=True)
    for arm in res:
        v = res[arm]; print("%s: n=%d median %.1f s, min %.1f, max %.1f" % (arm, len(v), statistics.median(v), min(v), max(v)))
    print("ratio ON/OFF of medians: %.2f" % (statistics.median(res["ON"]) / statistics.median(res["OFF"])))
    json.dump(res, open("/tmp/bench_ab_result.json", "w"))
