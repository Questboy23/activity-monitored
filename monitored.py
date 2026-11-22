#!/usr/bin/env python3
"""Activity Monitored 1.0: stops the macOS "Your system has run out of application memory" window. macOS only, Python 3.8+.

Why that window appears: macOS moves memory it can't fit in RAM into swap files on the disk. When the disk is nearly
full, it can't make more swap, so it pauses apps and lists them in red. Activity Monitored does three things:
  1. Keeps disk room for swap. Below the disk target (default 30 GB free) it runs the official cleanup commands of
     uv, pip and npm, which delete only download caches those tools fetch again when needed. Nothing else
     is ever deleted. Turn it off with `monitored clean off`.
  2. Warns you early with a macOS notification, at most once per 30 minutes per kind: when free disk drops below
     15 GB, and when memory is nearly exhausted (naming the 3 biggest programs so you can quit one). Under 10 GB
     free, or with memory exhausted, it also shows an alert that closes itself after 60 seconds, since Focus hides banners.
  3. Pauses before macOS does. When memory pressure turns critical (or warning with under 10 GB of disk, where swap
     cannot grow), it pauses the biggest program you are not using (SIGSTOP), 500 MB or more, scripts before apps,
     at most 3 at once. A paused program keeps all its work, stops taking more memory, and macOS compresses or swaps
     its memory to make room. Never paused: the app in front and everything it started, Claude and its tools,
     Logic, music and calls, Terminal, and system programs. Each one resumes when memory is normal for 30 seconds
     (one every 30 seconds), after 30 minutes at most, and by an OS timer even if Activity Monitored crashes.
     `monitored resume` resumes everything now.
  4. Keeps the front app smooth while the CPU is overloaded, by lowering the priority of busy background
     command-line programs (taskpolicy -b). Each one is restored when the CPU calms down, after 10 minutes at most,
     and by an OS timer even if Activity Monitored crashes.
Every action is temporary. It never quits, kills, deletes or rewrites any program or file (cache cleanup in 1 aside).
  monitored install | uninstall          install and start, or remove
  monitored status                       what it is doing now and in the last 24 hours
  monitored on | off                     off: stop and restore every lowered priority
  monitored clean on | off               automatic cache cleanup (on by default)
  monitored disk-target GB               free space to keep (default 30)
  monitored resume                       resume every program it paused, now
  monitored protect NAME                 never touch programs whose command contains NAME
  monitored uncompress                   undo the file compression older versions (0.x) did
"""
import json, os, re, shutil, signal, subprocess, sys, time
from collections import defaultdict

__version__ = "1.1.0"
HOME = os.path.expanduser("~")
DIR = os.environ.get("MONITORED_DIR", os.path.join(HOME, ".monitored"))
os.makedirs(DIR, exist_ok=True)
F_STATUS, F_JOURNAL, F_EVENTS, F_SET = DIR + "/status.json", DIR + "/journal.json", DIR + "/events.jsonl", DIR + "/settings.json"
F_COMP = DIR + "/compressed.jsonl"  # written by 0.x; read only by `uncompress`
LABEL, PLIST, SELF = "com.activitymonitored.agent", os.path.join(HOME, "Library/LaunchAgents/com.activitymonitored.agent.plist"), os.path.abspath(__file__)
NCPU, ME = os.cpu_count() or 8, os.getuid()
DEFAULTS = dict(mode="on", clean=True, disk_target_gb=30)
TICK, HOG_CPU, HOG_TICKS, BUSY, CALM, MAX_HOLD, WARN_EVERY, CLEAN_EVERY = float(os.environ.get("MONITORED_TICK", "5")), 20.0, 3, 0.75, 0.5, 600, 1800, 6 * 3600
MAX_PAUSE, PAUSE_MIN, MAX_PAUSED, HOT_TICKS, OK_TICKS = 1800, 500e6, 3, 2, 6
CLAUDE = re.compile(r"/Claude\.app/|\.local/share/claude/versions|(^|/)claude( |$)")
NEVER = re.compile(r"/(WindowServer|kernel_task|launchd|loginwindow|Finder|Dock|SystemUIServer|coreaudiod|sshd|tmux|Terminal|iTerm2?)(\.app|$| )|monitored\.py")
SYSTEM_PATH = re.compile(r"^(/System/|/usr/(?!local/)|/sbin/|/bin/|/Library/Apple/)")
APP_PART = re.compile(r"\.app/Contents/")  # an app or any of its helpers
PRIORITY = re.compile(r"Logic Pro|GarageBand|coreaudio|Spotify|zoom\.us|FaceTime|Discord|OBS|ffmpeg|xcodebuild|clang|swift-frontend|/Claude\.app/|\.local/share/claude/versions|(^|/)claude( |$)")
# Official cleanup commands: each deletes only that tool's download cache, which it fetches again on demand.
CLEANERS = [["uv", "cache", "prune"], ["pip3", "cache", "purge"], ["npm", "cache", "clean", "--force"]]

def run(args, t=10):
    try: return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=t).stdout
    except Exception: return ""
def now(): return time.time()
def jl(path, obj):
    try:
        if os.path.getsize(path) > 4_000_000: os.replace(path, path + ".1")
    except OSError: pass
    with open(path, "a") as f: f.write(json.dumps(obj, separators=(",", ":")) + "\n")
def jload(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default
def jsave(path, obj):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w") as f: json.dump(obj, f)
    os.replace(tmp, path)
def settings():
    s = dict(DEFAULTS); s.update({k: v for k, v in jload(F_SET, {}).items() if k in DEFAULTS}); return s

def disk_free_gb():
    fake = os.environ.get("MONITORED_FAKE_DISK")  # tests
    if fake: return float(open(fake).read())
    v = os.statvfs(HOME); return v.f_bavail * v.f_frsize / 1e9
def swap_frac():
    m = re.findall(r"total = ([\d.]+)M\s+used = ([\d.]+)M", run(["/usr/sbin/sysctl", "-n", "vm.swapusage"]))
    return float(m[0][1]) / float(m[0][0]) if m and float(m[0][0]) > 0 else 0.0
def pressure():
    if os.environ.get("MONITORED_FAKE_PRESSURE"): return int(open(os.environ["MONITORED_FAKE_PRESSURE"]).read())  # tests
    try: return int(run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip())  # 1 normal, 2 warning, 4 critical
    except ValueError: return 1
def sample():
    procs = {}
    for ln in run(["/bin/ps", "-axo", "pid=,ppid=,uid=,pcpu=,rss=,stat=,command="]).splitlines():
        p = ln.split(None, 6)
        try: procs[int(p[0])] = dict(pid=int(p[0]), ppid=int(p[1]), uid=int(p[2]), cpu=float(p[3]), rss=int(p[4]) * 1024, stat=p[5], cmd=p[6])
        except (ValueError, IndexError): continue
    return procs
def pname(cmd): return os.path.basename(cmd.split(" -")[0].split(" /")[0]).strip()[:40]

_warned = {}
def notify(kind, msg, urgent=False):
    """macOS notification banner, at most once per kind every 30 minutes. Focus mode hides banners, so an urgent
    warning (apps about to be paused) also shows an alert that closes itself after 60 seconds."""
    if now() - _warned.get(kind, 0) < WARN_EVERY: return False
    _warned[kind] = now(); jl(F_EVENTS, dict(t=int(now()), ev="warn", kind=kind, msg=msg))
    if not os.environ.get("MONITORED_NO_BANNER"):
        run(["/usr/bin/osascript", "-e", 'display notification %s with title "Activity Monitored"' % json.dumps(msg)])
        if urgent:
            subprocess.Popen(["/usr/bin/osascript", "-e", 'display alert "Activity Monitored" message %s giving up after 60' % json.dumps(msg)],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True

def protected():
    try: items = [l.strip() for l in open(DIR + "/protect.txt") if l.strip() and not l.startswith("#")]
    except OSError: items = []
    return re.compile("|".join(map(re.escape, items))) if items else None

def lowerable(p, prot):
    c = p["cmd"]
    return (p["uid"] == ME and p["pid"] != os.getpid() and not SYSTEM_PATH.match(c) and not APP_PART.search(c)
            and not NEVER.search(c) and not PRIORITY.search(c) and not (prot and prot.search(c)))

def lower(p):
    """Background priority now, plus an OS timer that restores it after MAX_HOLD even if this program dies."""
    pid = p["pid"]
    timer = subprocess.Popen(["/bin/sh", "-c", "sleep %d; /usr/sbin/taskpolicy -B -p %d" % (MAX_HOLD, pid)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid
    if subprocess.run(["/usr/sbin/taskpolicy", "-b", "-p", str(pid)], capture_output=True).returncode != 0:
        os.killpg(timer, 9); return
    J = jload(F_JOURNAL, {}); J[str(pid)] = dict(name=pname(p["cmd"]), t=now(), timer=timer); jsave(F_JOURNAL, J)
    jl(F_EVENTS, dict(t=int(now()), ev="lower", pid=pid, name=pname(p["cmd"]), cpu=p["cpu"]))

def restore(pid, why):
    J = jload(F_JOURNAL, {}); e = J.pop(str(pid), None); jsave(F_JOURNAL, J)
    if not e: return
    if e.get("kind") == "pause":
        try: os.kill(int(pid), signal.SIGCONT)  # harmless if the pid is gone or already running
        except OSError: pass
    else: subprocess.run(["/usr/sbin/taskpolicy", "-B", "-p", str(pid)], capture_output=True)
    try: os.killpg(int(e["timer"]), 9)
    except (OSError, ValueError, KeyError): pass
    jl(F_EVENTS, dict(t=int(now()), ev="resume" if e.get("kind") == "pause" else "restore", pid=int(pid), name=e.get("name"), why=why))
def restore_all(why="stop", kind=None):
    for pid, e in list(jload(F_JOURNAL, {}).items()):
        if kind is None or e.get("kind", "lower") == kind: restore(pid, why)

def front_pid():
    m = re.search(r"pid\D*(\d+)", run(["/usr/bin/lsappinfo", "info", "-only", "pid", run(["/usr/bin/lsappinfo", "front"]).strip()]))
    return int(m.group(1)) if m else None

def tree(procs, roots):
    kids = defaultdict(list)
    for p in procs.values(): kids[p["ppid"]].append(p["pid"])
    out, todo = set(), list(roots)
    while todo:
        x = todo.pop()
        if x not in out: out.add(x); todo += kids[x]
    return out

def pausable(procs, prot, front):
    """Biggest first, scripts before apps. Never: the front app and what it started, Claude and its tools, this monitor
    and its parents, Logic, music, calls, Terminal, system programs, other users' programs, anything already paused."""
    safe = tree(procs, [p["pid"] for p in procs.values() if CLAUDE.search(p["cmd"])] + ([front] if front else []))
    x = os.getpid()
    while x > 1 and x in procs: safe.add(x); x = procs[x]["ppid"]
    J = jload(F_JOURNAL, {})
    if now() < jload(DIR + "/hold.json", {}).get("until", 0): return []  # you resumed by hand: no new pauses for 10 minutes
    only = os.environ.get("MONITORED_ONLY")  # tests: never touch a real program
    c = [p for p in procs.values() if p["pid"] not in safe and "T" not in p.get("stat", "") and (not only or only in p["cmd"]) and str(p["pid"]) not in J and p["uid"] == ME and p["rss"] >= PAUSE_MIN
         and not SYSTEM_PATH.match(p["cmd"]) and not NEVER.search(p["cmd"]) and not PRIORITY.search(p["cmd"]) and not (prot and prot.search(p["cmd"]))]
    return sorted(c, key=lambda p: (bool(APP_PART.search(p["cmd"])), -p["rss"]))

def pause(p, why):
    """SIGSTOP now, plus an OS timer that sends SIGCONT after MAX_PAUSE even if this program dies."""
    pid = p["pid"]
    timer = subprocess.Popen(["/bin/sh", "-c", "sleep %d; /bin/kill -CONT %d" % (MAX_PAUSE, pid)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid
    try: os.kill(pid, signal.SIGSTOP)
    except OSError: os.killpg(timer, 9); return False
    J = jload(F_JOURNAL, {}); J[str(pid)] = dict(kind="pause", name=pname(p["cmd"]), t=now(), timer=timer, gb=round(p["rss"] / 1e9, 1)); jsave(F_JOURNAL, J)
    jl(F_EVENTS, dict(t=int(now()), ev="pause", pid=pid, name=pname(p["cmd"]), gb=round(p["rss"] / 1e9, 1), why=why))
    notify("pause-%d" % pid, "Paused %s (%.1f GB) so the Mac does not run out of memory. It resumes by itself when memory recovers, within 30 minutes. Resume now: monitored resume"
           % (pname(p["cmd"]), p["rss"] / 1e9))
    return True

def clean_caches():
    before, done = disk_free_gb(), []
    for c in CLEANERS:
        exe = shutil.which(c[0], path="/opt/homebrew/bin:/usr/local/bin:%s/.local/bin:/usr/bin" % HOME)
        if exe:
            os.environ["UV_LOCK_TIMEOUT"] = "30"  # another uv holding its lock must not stall warnings for 5 minutes
            run([exe] + c[1:], 600); done.append(" ".join(c))
    freed = disk_free_gb() - before
    jl(F_EVENTS, dict(t=int(now()), ev="clean", cmds=done, freed_gb=round(freed, 1)))
    return freed

def cmd_run(args):
    restore_all("start")  # a leftover from a crash
    s, streak, calm, t_disk, t_clean, last_mem, hot, ok = settings(), defaultdict(int), 0, 0.0, jload(F_STATUS, {}).get("last_clean", 0), 0, 0, 0
    while True:
        procs, prot = sample(), protected()
        cpu = sum(p["cpu"] for p in procs.values()) / (100.0 * NCPU)
        J = jload(F_JOURNAL, {})
        for pid in J:
            if int(pid) not in procs: restore(pid, "exited")
        if cpu > BUSY:
            calm = 0
            for p in procs.values():
                streak[p["pid"]] = streak[p["pid"]] + 1 if p["cpu"] >= HOG_CPU else 0
                if streak[p["pid"]] >= HOG_TICKS and str(p["pid"]) not in J and lowerable(p, prot): lower(p)
        else:
            streak.clear(); calm = calm + 1 if cpu < CALM else 0
            if calm >= 3: restore_all("calm", kind="lower")
        sw, lvl, gb_now = swap_frac(), pressure(), disk_free_gb()
        hot_now = lvl >= 4 or (lvl >= 2 and gb_now < 10)  # critical, or warning while swap has no disk room to grow
        hot, ok = (hot + 1, 0) if hot_now else (0, ok + 1 if lvl <= 1 else 0)
        paused = {k: e for k, e in jload(F_JOURNAL, {}).items() if e.get("kind") == "pause"}
        if hot >= HOT_TICKS and len(paused) < MAX_PAUSED:  # 10 seconds of real pressure: pause one more, then wait another 10
            c = pausable(procs, prot, front_pid())
            if c: pause(c[0], "memory pressure %d, swap %d%%, disk %.0f GB" % (lvl, 100 * sw, gb_now))
            hot = 0
        if ok >= OK_TICKS and paused:  # normal for 30 seconds: resume the most recent one
            restore(max(paused, key=lambda k: paused[k]["t"]), "memory recovered"); ok = 0
        last_mem = last_mem + 1 if (lvl >= 4 or (lvl >= 2 and sw > 0.9)) else 0
        if last_mem >= 6:  # 30 seconds of real memory exhaustion
            top = sorted(procs.values(), key=lambda p: -p["rss"])[:3]
            notify("memory", "Memory almost full even after pausing. Biggest: %s. Quit one." % ", ".join("%s %.1f GB" % (pname(p["cmd"]), p["rss"] / 1e9) for p in top), urgent=True)
        if now() - t_disk >= 60:
            t_disk, gb, target = now(), disk_free_gb(), float(settings()["disk_target_gb"])
            if gb < target and settings()["clean"] and now() - t_clean > CLEAN_EVERY:
                t_clean = now(); freed = clean_caches(); gb = disk_free_gb()
                if freed >= 0.5: notify("clean", "Freed %.1f GB of download caches (uv, pip, npm). %.0f GB free now." % (freed, gb))
            if gb < 15: notify("disk", "Only %.0f GB free on the disk. Below about 10 GB macOS starts pausing apps. Free up space." % gb, urgent=gb < 10)
        jsave(F_STATUS, dict(ts=now(), cpu=round(cpu, 2), swap=round(sw, 2), pressure=lvl, disk_gb=round(disk_free_gb(), 1), last_clean=t_clean,
                             lowered=[e["name"] for e in jload(F_JOURNAL, {}).values() if e.get("kind") != "pause"],
                             paused=["%s (%s GB)" % (e["name"], e.get("gb")) for e in jload(F_JOURNAL, {}).values() if e.get("kind") == "pause"]))
        if args and args[0] == "--once": return
        time.sleep(TICK)

# ---------- undo for 0.x: it rewrote big idle files with macOS transparent compression ----------
def uncompress_file(path):
    tmp = os.path.join(os.path.dirname(path), ".%s.am-tmp" % os.path.basename(path))
    st = os.lstat(path)
    if os.path.lexists(tmp) or disk_free_gb() * 1e9 < st.st_size + 5e9: return False
    try:
        if subprocess.run(["/bin/cp", "-p", path, tmp], capture_output=True, timeout=900).returncode != 0: return False
        if subprocess.run(["/usr/bin/cmp", "-s", path, tmp]).returncode != 0: return False
        n = os.lstat(path)
        if (n.st_mtime, n.st_size, n.st_ino) != (st.st_mtime, st.st_size, st.st_ino): return False
        os.replace(tmp, path); return True
    finally:
        if os.path.isfile(tmp): os.remove(tmp)  # our own temporary copy only
def cmd_uncompress(args):
    log = {}
    if os.path.exists(F_COMP):
        for x in open(F_COMP):
            try: e = json.loads(x)
            except ValueError: continue
            if e.get("ev") == "compress": log[e["path"]] = e
            elif e.get("ev") == "restore": log.pop(e["path"], None)
    n = left = 0
    for p in log:
        try: comp = os.lstat(p).st_flags & 0x20
        except OSError: jl(F_COMP, dict(t=int(now()), ev="restore", path=p, gone=True)); continue
        if not comp or uncompress_file(p): n += 1; jl(F_COMP, dict(t=int(now()), ev="restore", path=p))
        else: left += 1
    print("Uncompressed %d files. %d left (each needs its size + 5 GB free; run again after freeing space)." % (n, left))

# ---------- install, settings, status ----------
def lc(*a): return subprocess.run(["/bin/launchctl"] + list(a), capture_output=True, text=True)
def running(): return lc("list", LABEL).returncode == 0
def write_plist():
    import plistlib
    d = {"Label": LABEL, "ProgramArguments": ["/usr/bin/python3", "-I", DIR + "/monitored.py", "run"], "RunAtLoad": True,
         "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, "LowPriorityIO": True, "ProcessType": "Background",
         "StandardOutPath": DIR + "/launchd.log", "StandardErrorPath": DIR + "/launchd.log",
         "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:" + HOME + "/.local/bin"}}
    with open(PLIST, "wb") as f: plistlib.dump(d, f)
def apply_settings():
    if running(): lc("bootout", "gui/%d/%s" % (ME, LABEL)); time.sleep(1)
    restore_all("stop")
    if settings()["mode"] == "off":
        if os.path.exists(PLIST): os.remove(PLIST)
        print("Activity Monitored is off. Every lowered priority is restored."); return 0
    write_plist(); r = lc("bootstrap", "gui/%d" % ME, PLIST)
    print("Activity Monitored is on." if r.returncode == 0 else "macOS refused to start it: " + r.stderr.strip()); return r.returncode
def set_key(k, v):
    s = jload(F_SET, {}); s[k] = v; jsave(F_SET, s)
def cmd_install(args):
    if os.path.abspath(DIR + "/monitored.py") != SELF: shutil.copy(SELF, DIR + "/monitored.py")
    with open(DIR + "/monitored", "w") as f: f.write('#!/bin/bash\nexec /usr/bin/python3 -I "%s/monitored.py" "$@"\n' % DIR)
    os.chmod(DIR + "/monitored", 0o755)
    link = os.path.join(HOME, ".local/bin/monitored")
    if os.path.isdir(os.path.dirname(link)) and not os.path.lexists(link): os.symlink(DIR + "/monitored", link)
    set_key("mode", "on"); return apply_settings()
def cmd_uninstall(args):
    set_key("mode", "off"); apply_settings()
    for f in ("monitored.py", "monitored"):
        if os.path.exists(DIR + "/" + f): os.remove(DIR + "/" + f)
    link = os.path.join(HOME, ".local/bin/monitored")
    if os.path.islink(link): os.remove(link)
    print("Removed. Logs stay in %s." % DIR)
def cmd_status(args):
    s, st = settings(), jload(F_STATUS, {})
    fresh = now() - st.get("ts", 0) < 30
    print("Activity Monitored %s is %s. %s" % (__version__, s["mode"], "Running." if fresh else "Not running."))
    if fresh:
        print("Disk free: %.0f GB (target %d GB, cache cleanup %s). Swap %d%% used. Memory pressure: %s. CPU %d%% busy."
              % (st["disk_gb"], s["disk_target_gb"], "on" if s["clean"] else "off", 100 * st["swap"], {1: "normal", 2: "warning", 4: "critical"}.get(st["pressure"], "?"), 100 * st["cpu"]))
        print("Paused right now: %s." % (", ".join(st.get("paused", [])) or "nothing"))
        print("Lowered priority right now: %s." % (", ".join(st["lowered"]) or "nothing"))
    ev = [json.loads(x) for x in open(F_EVENTS)] if os.path.exists(F_EVENTS) else []
    ev = [e for e in ev if now() - e.get("t", 0) < 86400]
    print("Last 24 hours: %d pauses, %d priority lowerings, %d cache cleanups (%.1f GB freed), %d warnings."
          % (sum(e.get("ev") == "pause" for e in ev), sum(e.get("ev") == "lower" for e in ev), sum(e.get("ev") == "clean" for e in ev), sum(e.get("freed_gb", 0) for e in ev if e.get("ev") == "clean"), sum(e.get("ev") == "warn" for e in ev)))
    for e in [e for e in ev if e.get("ev") == "warn"][-3:]: print("  warned: " + e["msg"])

if __name__ == "__main__":
    a = sys.argv[1:]; c = a[0] if a else ""
    def toggle(k):
        def f(x):
            if x[:1] not in (["on"], ["off"]): print("usage: monitored %s on|off" % k); return 1
            set_key(k, x[0] == "on"); print("%s %s" % (k, x[0]))
        return f
    def target(x):
        try: v = float(x[0]); assert 5 <= v <= 500
        except (IndexError, ValueError, AssertionError): print("usage: monitored disk-target GB (5 to 500)"); return 1
        set_key("disk_target_gb", v); print("Keeps at least %.0f GB free." % v)
    def protect(x):
        if not x: print("usage: monitored protect NAME"); return 1
        with open(DIR + "/protect.txt", "a") as f: f.write(" ".join(x) + "\n")
        print("Never touches programs whose command contains '%s'." % " ".join(x))
    table = {"run": cmd_run, "status": cmd_status, "install": cmd_install, "uninstall": cmd_uninstall, "uncompress": cmd_uncompress,
             "on": lambda x: (set_key("mode", "on"), apply_settings())[1], "off": lambda x: (set_key("mode", "off"), apply_settings())[1],
             "clean": toggle("clean"), "disk-target": target, "protect": protect, "restore-all": lambda x: restore_all(), "resume": lambda x: (jsave(DIR + "/hold.json", dict(until=now() + 600)), restore_all("resumed by you", kind="pause"), print("Every paused program is running again. No new pauses for 10 minutes."))[2], "version": lambda x: print(__version__)}
    if c not in table: print(__doc__); sys.exit(0 if c in ("", "help", "-h", "--help") else 1)
    sys.exit(table[c](a[1:]) or 0)
