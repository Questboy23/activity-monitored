#!/usr/bin/env python3
"""Activity Monitored: keeps a Mac responsive when it is overloaded. macOS only, Python 3.8+, no packages to install.
While the CPU is oversubscribed, it lowers the priority of busy background programs (taskpolicy -b). While memory runs out, it
pauses the biggest background command-line program (never an app with a window) and resumes it when memory recovers.
Every action is undone by an OS-level timer, even if Activity Monitored crashes. It never shows a pop-up, quits or deletes anything.
While the disk runs short it frees space by turning on macOS transparent compression for big idle files (listed in
compressed.jsonl, reversible), and below 3 GB it pauses the program writing most to disk until 6 GB are free.
  monitored install           install and start (one command; see README)
  monitored status            what Activity Monitored is doing right now and in the last 24 hours
  monitored on | watch | off  on: act. watch: log what it would do, change nothing. off: stop and undo everything
  monitored battery on|off    battery saver: checks less often while on battery
  monitored protect NAME      never touch programs whose command contains NAME
  monitored gate [--max-wait S] -- CMD   start CMD once memory is not thrashing (waits at most S seconds)
  monitored disk-target GB    free space to keep (default 30 GB); below it, it compresses idle files and stops swap growth
  monitored uncompress        undo the space-saving compression (it also runs by itself at target + 30 GB free)
  monitored uninstall [--purge] | version
"""
import ctypes, json, os, re, shutil, signal, subprocess, sys, time
from collections import defaultdict

HOME = os.path.expanduser("~")
DIR = os.environ.get("MONITORED_DIR", os.path.join(HOME, ".monitored"))
os.makedirs(DIR, exist_ok=True)
try: os.chmod(DIR, 0o700)
except OSError: pass
F_STATUS, F_JOURNAL = DIR + "/status.json", DIR + "/journal.json"
F_LOG, F_EVENTS, F_FG = DIR + "/log.jsonl", DIR + "/events.jsonl", DIR + "/foreground.jsonl"
NCPU, ME = os.cpu_count() or 8, os.getuid()
CFG = dict(tick=3.0, hog_cpu=20.0, hog_ticks=3, cpu_busy=0.75, calm_ticks=3, demote_ttl=90,
           max_demote=40, pageout_hot=60.0, adaptive=True, scan_at=0.72, saver=False)

# Never touched, by command text. System daemons are excluded by path unless on the BG_OK list.
NEVER = re.compile(r"/(WindowServer|kernel_task|launchd|loginwindow|Finder|Dock|SystemUIServer|coreaudiod|bluetoothd|sshd|tmux|Terminal|iTerm2?|Activity Monitor)(\.app|$| )|monitored\.py")
SYSTEM_PATH = re.compile(r"^(/System/|/usr/(?!local/)|/sbin/|/bin/|/Library/Apple/)")  # /usr/local holds user-installed node, python
BG_OK = {"photoanalysisd", "mediaanalysisd", "mdworker_shared", "mds_stores", "backupd", "suggestd", "parsecd", "siriknowledged", "cloudd"}
# Work that matters: never demoted, and so is everything under it. Add your own with `monitored protect NAME`.
PRIORITY = re.compile(r"rsync|xcodebuild|swift-frontend|clang|ffmpeg|HandBrake|Final Cut|Premiere|DaVinci|Blender|Compressor|Docker|Parallels|VMware|UTM|qemu|postgres|mysqld|mongod|redis-server|nginx|gradle|\bcmake\b|\bninja\b|\bmake\b|ollama|llama-server|Spotify|zoom\.us|FaceTime|Discord|OBS|Logic Pro|GarageBand|Ableton|/Music\.app/|Microsoft Teams|/Claude\.app/|\.local/share/claude/versions|(^|/)claude( |$)|pytest|cargo|rustc|\bnpm run\b|vite|webpack|esbuild|\btsc\b")

def _user_protect():
    """Programs the user asked Activity Monitored never to touch: one name per line in protect.txt (a command containing the name is protected)."""
    try: items = [l.strip() for l in open(DIR + "/protect.txt") if l.strip() and not l.startswith("#")]
    except OSError: return None
    return re.compile("|".join(re.escape(i) for i in items)) if items else None

def run(args, t=5):
    try:
        return subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=t).stdout
    except Exception:
        return ""

def now(): return time.time()
def jl(path, obj):
    try:
        if os.path.getsize(path) > 4_000_000 and not path.endswith("compressed.jsonl"): os.replace(path, path + ".1")  # at most two files of 4 MB per log; the compression record is never rotated, or uncompress would lose files
    except OSError: pass
    with open(path, "a") as f: f.write(json.dumps(obj, separators=(",", ":")) + "\n")
def jload(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default
def jsave(path, obj):
    tmp = "%s.%d.tmp" % (path, os.getpid())  # CLI and agent may save at once
    with open(tmp, "w") as f: json.dump(obj, f)
    os.replace(tmp, path)

def sample():
    out = run(["/bin/ps", "-axo", "pid=,ppid=,uid=,pcpu=,rss=,stat=,command="], 8)
    procs = {}
    for ln in out.splitlines():
        p = ln.split(None, 6)
        if len(p) < 7: continue
        try:
            pid = int(p[0]); procs[pid] = dict(pid=pid, ppid=int(p[1]), uid=int(p[2]), cpu=float(p[3]), rss=int(p[4]) * 1024, stat=p[5], cmd=p[6])
        except ValueError: continue
    return procs

def pname(cmd):
    head = re.split(r" --?\w", cmd)[0].split(" /")[0]  # drop trailing path arguments: a scanner pointed at ~/x/edge is not called edge
    return os.path.basename(head).strip()[:40]

_last_po = [None, None]
def memory_slow():
    if os.environ.get("MONITORED_FAKE_LEVEL"):
        return int(os.environ["MONITORED_FAKE_LEVEL"]), 0.9, float(os.environ.get("MONITORED_FAKE_PAGEOUT", "0"))
    lvl = int(run(["/usr/sbin/sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip() or 1)
    m = re.findall(r"([\d.]+)M", run(["/usr/sbin/sysctl", "-n", "vm.swapusage"]))
    swap = float(m[1]) / float(m[0]) if len(m) >= 2 and float(m[0]) else 0.0
    mo = re.search(r"Pageouts:\s+(\d+)", run(["/usr/bin/vm_stat"]))
    po, t, rate = (int(mo.group(1)) if mo else 0), now(), 0.0
    if _last_po[0] is not None and t > _last_po[1]: rate = max(0.0, (po - _last_po[0]) / (t - _last_po[1]))
    _last_po[0], _last_po[1] = po, t
    return lvl, swap, rate

# ---- cheap readings straight from the kernel (about 0.01 ms for all three; the old way spawned 6 programs, about 160 ms) ----
try:
    _LIB = ctypes.CDLL("/usr/lib/libSystem.B.dylib"); _LIB.mach_host_self.restype = ctypes.c_uint; _HOST = _LIB.mach_host_self()
except Exception:
    _LIB = None
def cpu_ticks():
    info = (ctypes.c_int32 * 4)(); cnt = ctypes.c_uint32(4)
    return list(info) if _LIB and _LIB.host_statistics(_HOST, 3, info, ctypes.byref(cnt)) == 0 else None  # user, system, idle, nice
def vm_pageouts():
    buf = (ctypes.c_uint8 * 256)(); cnt = ctypes.c_uint32(38)
    return int.from_bytes(bytes(buf[40:48]), "little") if _LIB and _LIB.host_statistics64(_HOST, 4, buf, ctypes.byref(cnt)) == 0 else None
def pressure_level():
    v = ctypes.c_int(0); sz = ctypes.c_size_t(4)
    return v.value if _LIB and _LIB.sysctlbyname(b"kern.memorystatus_vm_pressure_level", ctypes.byref(v), ctypes.byref(sz), None, 0) == 0 else None
_swap_c = [0.0, 0.0]
def memory():
    if os.environ.get("MONITORED_FAKE_LEVEL") or _LIB is None: return memory_slow()
    lvl, po_total = pressure_level(), vm_pageouts()
    if lvl is None or po_total is None: return memory_slow()
    t = now()
    if t - _swap_c[0] > 30:  # swap fill changes slowly: read it twice a minute
        m = re.findall(r"([\d.]+)M", run(["/usr/sbin/sysctl", "-n", "vm.swapusage"]))
        _swap_c[0], _swap_c[1] = t, (float(m[1]) / float(m[0]) if len(m) >= 2 and float(m[0]) else 0.0)
    rate = 0.0
    if _last_po[0] is not None and t > _last_po[1]: rate = max(0.0, (po_total - _last_po[0]) / (t - _last_po[1]))
    _last_po[0], _last_po[1] = po_total, t
    return lvl, _swap_c[1], rate

def fg_light():
    a = run(["/usr/bin/lsappinfo", "front"]).strip()
    m = re.search(r'"LSDisplayName"="([^"]*)"', run(["/usr/bin/lsappinfo", "info", "-only", "name", a])) if a else None
    return m.group(1) if m else None

def foreground(procs):
    a = run(["/usr/bin/lsappinfo", "front"]).strip()
    m = re.search(r'"pid"=(\d+)', run(["/usr/bin/lsappinfo", "info", "-only", "pid", a])) if a else None
    pid = int(m.group(1)) if m else None
    return pid, (pname(procs[pid]["cmd"]) if pid in procs else None)

def tree(kids, root):
    s, st = set(), [root]
    while st:
        x = st.pop()
        if x in s: continue
        s.add(x); st.extend(kids.get(x, []))
    return s

def classify(procs, fg_pid):
    kids = defaultdict(list)
    for p in procs.values(): kids[p["ppid"]].append(p["pid"])
    protected, priority = set(), set()
    up = _user_protect()
    if fg_pid in procs: protected |= tree(kids, fg_pid)
    x = os.getpid()
    while x > 1 and x in procs: protected.add(x); x = procs[x]["ppid"]
    for p in procs.values():
        if NEVER.search(p["cmd"]): protected.add(p["pid"])
        if (PRIORITY.search(p["cmd"]) or (up and up.search(p["cmd"]))) and p["pid"] != os.getpid(): priority |= tree(kids, p["pid"])
    return protected | priority, priority

TEST_ONLY = os.environ.get("MONITORED_TEST_ONLY")  # tests: touch only processes whose command contains this mark

def eligible(p):
    if p["uid"] != ME or (TEST_ONLY and TEST_ONLY not in p["cmd"]): return False
    n = pname(p["cmd"])
    if SYSTEM_PATH.match(p["cmd"]) and n not in BG_OK: return False
    return "T" not in p["stat"]

# ---------- executor: every action is undoable and carries its own OS-level undo timer ----------
def dead_man(undo, ttl):
    """Starts an OS-level undo timer in its own process group and returns its id, so a refresh or undo can cancel it."""
    return subprocess.Popen(["/bin/sh", "-c", "sleep %d; %s" % (int(ttl), undo)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).pid

def cancel_timer(e):
    """Stops an old undo timer. Without this, a timer from an earlier refresh restores a still-demoted program early,
    or later runs on a reused pid."""
    e = e or {}; tp = e.get("timer")
    if tp and now() - e.get("t", 0) < e.get("ttl", 1e9) + 5:  # an expired timer's pid may now belong to another program
        try: os.killpg(int(tp), signal.SIGKILL)
        except (OSError, ValueError): pass

def journal(): return jload(F_JOURNAL, {})

DRY, _would = False, {}

# Warnings: things slowing a process cannot fix (full disk, memory exhausted). It tells you; it never kills or deletes.
WARN = dict(swap_full=0.9, mem_ticks=10, every=1800)
_warned, _wstate = {}, dict(disk_t=0.0, mem=0)
def disk_free_gb():
    fake = os.environ.get("MONITORED_FAKE_DISK")  # tests: a file holding the free GB to report
    if fake:
        try: return float(open(fake).read())
        except (OSError, ValueError): return None
    try: v = os.statvfs(HOME); return v.f_bavail * v.f_frsize / 1e9
    except OSError: return None
def notify(key, msg):  # written to the status log only, at most once per kind every 30 minutes: Activity Monitored never shows a pop-up
    t = now()
    if t - _warned.get(key, 0) < WARN["every"]: return False
    _warned[key] = t; jl(F_EVENTS, dict(t=int(t), ev="warn", kind=key, msg=msg))
    return True
def warn_check(lvl, swap, mem_hot):
    t = now()
    if t - _wstate["disk_t"] >= 60:
        _wstate["disk_t"] = t; gb = disk_free_gb()
        if gb is not None and gb < disk_target():
            notify("disk", "Disk below the %.0f GB target: %.0f GB free. macOS needs free space for swap. Compressing big idle files to make room." % (disk_target(), gb))
    _wstate["mem"] = _wstate["mem"] + 1 if (mem_hot or (lvl is not None and lvl >= 2 and swap >= WARN["swap_full"])) else 0
    if _wstate["mem"] >= WARN["mem_ticks"] and t - _warned.get("memory", 0) >= WARN["every"]:  # check the limit first: ps is costly while memory is short
        top = sorted(sample().values(), key=lambda p: -p["rss"])[:3]
        if notify("memory", "Memory exhausted (swap %d%% full). Biggest: %s." % (round(100 * swap), ", ".join("%s %.1f GB" % (pname(p["cmd"]), p["rss"] / 1e9) for p in top))): _wstate["mem"] = 0
def apply(act, p, ttl):
    pid, J = p["pid"], journal()
    if DRY:  # log what would happen, change nothing
        if now() - _would.get((pid, act), 0) > 60:
            _would[(pid, act)] = now(); jl(F_EVENTS, dict(t=int(now()), ev="would_" + act, pid=pid, name=pname(p["cmd"]), cpu=p["cpu"]))
        return False
    timer = dead_man("/usr/sbin/taskpolicy -B -p %d" % int(pid), ttl)  # undo timer first: a crash right after the action still restores it
    if subprocess.run(["/usr/sbin/taskpolicy", "-b", "-p", str(pid)], capture_output=True).returncode != 0: cancel_timer({"timer": timer}); return False
    old = J.get(str(pid)); refresh = bool(old and old["act"] == act)
    cancel_timer(old)  # only the newest timer may end this action
    J[str(pid)] = dict(act=act, name=pname(p["cmd"]), t=now(), ttl=ttl, cpu=(old.get("cpu", p["cpu"]) if refresh else p["cpu"]), timer=timer)
    jsave(F_JOURNAL, J)
    if not refresh:
        jl(F_EVENTS, dict(t=int(now()), ev=act, pid=pid, name=pname(p["cmd"]), cpu=p["cpu"]))
    return True

MEM = dict(min_rss=100e6, max_paused=3, ttl=120, max_hold=600, hot_ticks=2, calm_ticks=3)

APP_PART = re.compile(r"\.app/Contents/(Frameworks|Helpers|XPCServices|PlugIns|Library)/| Helper\b")

def footprint(pid):
    """Memory a program really uses, including the part macOS compressed or swapped out (Activity Monitor's Memory
    column). Resident size alone drops to almost nothing for the very program that is filling swap."""
    buf = (ctypes.c_uint8 * 96)()
    if _LIB is None or _LIB.proc_pid_rusage(int(pid), 0, buf) != 0: return 0
    return int.from_bytes(bytes(buf[72:80]), "little")  # rusage_info_v0.ri_phys_footprint

def gui_pids():
    """Processes macOS lists as apps (Dock icon or menu bar item)."""
    return {int(x) for x in re.findall(r"pid = (\d+)", run(["/usr/bin/lsappinfo", "list"], 8))}

def pausable(p, protected, apps):
    """Memory relief only pauses your own background work (scripts, builds, servers): never an app, an app's helper,
    a system program, or anything protected. A paused program keeps its memory and state."""
    return p["pid"] not in protected and p["pid"] not in apps and eligible(p) and not APP_PART.search(p["cmd"])

def emergency_ok(p, fg_pid, apps):
    """Emergency rule, used only when nothing else can be paused: a script that an app started (Python run by Claude, say)
    may be paused. Never the app itself, its bundled helpers, the monitor, or work protected by name."""
    c, up = p["cmd"], _user_protect()
    return (eligible(p) and not SYSTEM_PATH.match(c) and p["pid"] not in (fg_pid, os.getpid(), os.getppid()) and p["pid"] not in apps and not re.search(r"\.app/", re.sub(r"/Python\.app/Contents/MacOS/Python\b", "", c))  # python3 runs from Python.app; it is still a script
            and not APP_PART.search(c) and not NEVER.search(c) and not PRIORITY.search(c) and not (up and up.search(c)))

def pause(p, since=None, act="pause", ttl=None, emg=False):
    """Freezes one program so it stops taking memory (act pause) or filling the disk (act dpause). An OS-level timer
    unfreezes it after 2 minutes even if Activity Monitored crashes; while the shortage lasts the timer is renewed."""
    pid, J, ttl = p["pid"], journal(), ttl or MEM["ttl"]
    if DRY:
        jl(F_EVENTS, dict(t=int(now()), ev="would_" + act, pid=pid, name=pname(p["cmd"]), rss=p["rss"])); return False
    timer = dead_man("/bin/kill -CONT %d" % int(pid), ttl)  # undo timer first, as for demote
    try: os.kill(pid, signal.SIGSTOP)
    except OSError: cancel_timer({"timer": timer}); return False
    old = J.get(str(pid)); cancel_timer(old)
    J[str(pid)] = dict(act=act, name=pname(p["cmd"]), t=now(), ttl=ttl, since=since or now(), rss=p["rss"], timer=timer, emg=emg)
    jsave(F_JOURNAL, J)
    if not since: jl(F_EVENTS, dict(t=int(now()), ev=act, pid=pid, name=pname(p["cmd"]), rss=p["rss"]))
    return True

def undo(pid, why):
    J = journal(); e = J.pop(str(pid), None)
    if e is None: return
    cancel_timer(e)
    if e["act"] in ("pause", "dpause"):
        try: os.kill(int(pid), signal.SIGCONT)
        except OSError: pass
    else: subprocess.run(["/usr/sbin/taskpolicy", "-B", "-p", str(pid)], capture_output=True)
    jsave(F_JOURNAL, J)
    jl(F_EVENTS, dict(t=int(now()), ev="undo_" + e["act"], pid=int(pid), name=e["name"], why=why))

def revert_all(why="revert-all"):
    for pid in list(journal()): undo(pid, why)


# ---------- disk relief: frees space without deleting anything, and records every change so it can be reversed ----------
DISK = dict(max_hold=float(os.environ.get("MONITORED_DISK_MAX_HOLD", str(3 * 3600))), pause_below=3.0, min_rate=1e6, swap_grow=0.25, min_mem=float(os.environ.get("MONITORED_DISK_MIN_MEM", "500e6")), every=float(os.environ.get("MONITORED_DISK_EVERY", "60")), max_paused=2, ttl=150, end_after=float(os.environ.get("MONITORED_DISK_END_AFTER", "900")), end_mem=float(os.environ.get("MONITORED_DISK_END_MEM", "2e9")),
            min_size=8e6, min_age=3 * 86400, retry=6 * 3600)
def disk_target():
    """Free space to keep, in GB: `monitored disk-target GB` (default 30)."""
    for k in ("MONITORED_DISK_TARGET", "MONITORED_DISK_MIN_GB"):  # tests
        if os.environ.get(k): return float(os.environ[k])
    try: return float(settings().get("disk_target_gb", 30))
    except (TypeError, ValueError): return 30.0

def swap_used_gb():
    fake = os.environ.get("MONITORED_FAKE_SWAP")  # tests: a file holding the used GB to report
    if fake:
        try: return float(open(fake).read())
        except (OSError, ValueError): return None
    m = re.findall(r"used = ([\d.]+)M", run(["/usr/sbin/sysctl", "-n", "vm.swapusage"]))
    return float(m[0]) / 1024 if m else None

F_COMP, F_CSTATE = DIR + "/compressed.jsonl", DIR + "/compress-state.json"
F_STOPPED = DIR + "/stopped.jsonl"  # programs ended to save the Mac, with the command and folder to rerun them

def end_paused(q, e, f):
    """A paused program keeps all its memory, and macOS keeps it in swap files on this disk. Nov 19: run_bench.py
    sat paused for 4 hours holding 22 GB of swap, the disk hit 0 GB free and apps froze. Ending it is the only way to
    free that room. Records the exact command and folder first, so it can be run again."""
    pid = q["pid"]
    cwd = re.findall(r"^n(.*)$", run(["/usr/sbin/lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"]), re.M)
    jl(F_STOPPED, dict(t=int(now()), name=e["name"], cmd=q["cmd"], cwd=cwd[0] if cwd else "", gb=round(f / 1e9, 1)))
    J = journal(); J.pop(str(pid), None); cancel_timer(e); jsave(F_JOURNAL, J)
    try: os.kill(pid, signal.SIGTERM); os.kill(pid, signal.SIGCONT)  # a stopped program only sees SIGTERM once resumed
    except OSError: return
    for _ in range(20):
        time.sleep(0.5)
        try: os.kill(pid, 0)
        except OSError: break
    else:
        if pname(run(["/bin/ps", "-o", "command=", "-p", str(pid)]).strip()) == e["name"]: os.kill(pid, signal.SIGKILL)
    jl(F_EVENTS, dict(t=int(now()), ev="end", pid=pid, name=e["name"], rss=int(f)))
    notify("end", "Ended %s: it was paused and held %.0f GB of swap while the disk was nearly full. Rerun: cd %s && %s" % (e["name"], f / 1e9, cwd[0] if cwd else "~", q["cmd"]))

SKIP_EXT = set("zip gz tgz xz bz2 zst lz4 7z rar dmg iso pkg sparseimage sparsebundle jpg jpeg png heic gif webp avif mp4 mov m4v mkv avi webm mp3 m4a aac flac ogg opus "
               "gguf safetensors pt pth onnx bin ckpt npz parquet sqlite db sqlite3 wal shm lock pack idx".split())

def proc_written(pid):
    """Bytes a program has written to disk since it started (rusage_info_v2.ri_diskio_byteswritten)."""
    buf = (ctypes.c_uint8 * 256)()
    if _LIB is None or _LIB.proc_pid_rusage(int(pid), 2, buf) != 0: return None
    return int.from_bytes(bytes(buf[152:160]), "little")

def comp_roots():
    r = os.environ.get("MONITORED_COMPRESS_ROOTS")
    if r: return r.split(":")  # cloud-synced folders are skipped: a rewritten file could be uploaded again
    icloud = HOME + "/Library/Mobile Documents/com~apple~CloudDocs/"
    skip = {"Library", "Applications", "Creative Cloud Files", "Dropbox", "Google Drive", "OneDrive", "Box"} | {d for d in ("Desktop", "Documents") if os.path.exists(icloud + d)}
    try: return sorted(os.path.join(HOME, d) for d in os.listdir(HOME) if not d.startswith(".") and d not in skip and os.path.isdir(os.path.join(HOME, d)) and not os.path.islink(os.path.join(HOME, d)))
    except OSError: return []

def compressible(path, size):
    """Reads 1 MB from the middle and zlib-compresses it: media, models and archives come out at about 100%, so skip them."""
    import zlib
    try:
        with open(path, "rb") as f: f.seek(max(0, size // 2 - (1 << 19))); b = f.read(1 << 20)
    except OSError: return None
    r = len(zlib.compress(b, 1)) / len(b) if b else 1.0
    return r if r < 0.7 else None

def open_paths():
    """Every file some program has open right now: rewriting one of those could lose that program's writes."""
    try: r = subprocess.run(["/usr/sbin/lsof", "-n", "-w", "-Fn", "-u", str(ME)], capture_output=True, text=True, errors="replace", timeout=120)
    except (OSError, subprocess.SubprocessError): return None  # unknown means everything might be open: compress nothing
    out = {l[1:] for l in r.stdout.splitlines() if l.startswith("n/")}
    return out or None

def is_open(path):
    """True unless lsof answers clearly that nothing has the file open; a timeout counts as open."""
    try: r = subprocess.run(["/usr/sbin/lsof", "-t", "--", path], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError): return True
    return not (r.returncode == 1 and not r.stdout.strip())

F_CSKIP = DIR + "/compress-skip.json"
def ok_file(st, t, dev):
    """Regular, single-link, not compressed, not an iCloud placeholder, on the home disk, yours, idle 3 days, 8 MB or more."""
    return ((st.st_mode & 0o170000) == 0o100000 and st.st_size >= DISK["min_size"] and st.st_nlink == 1 and not st.st_flags & 0x40000020
            and st.st_dev == dev and st.st_uid == ME and t - st.st_mtime > DISK["min_age"])

def spotlight(root):
    """Big files under root from the Spotlight index, or None when Spotlight has nothing for it (not indexed, or off)."""
    out = run(["/usr/bin/mdfind", "-onlyin", root, "kMDItemFSSize >= %d" % int(DISK["min_size"])], 300)
    paths = [l for l in out.splitlines() if l.startswith(root + "/")]
    return paths or None

def candidates():
    """Big, idle, compressible files in your home folder, one top folder at a time, largest first. Library, hidden folders
    (.git, .venv, .ollama) and anything open are skipped."""
    t, busy = now(), open_paths()
    if busy is None: return
    dev, rejected = os.lstat(HOME).st_dev, jload(F_CSKIP, {})
    skip_dir = lambda x: x.startswith(".") or x.endswith((".app", ".photoslibrary", ".bundle"))
    for root in comp_roots():
        out, found = [], spotlight(root)  # the index answers in seconds; walking 11 million files at low priority took hours
        if found is None:
            found = []
            for d, dirs, files in os.walk(root):
                dirs[:] = [x for x in dirs if not skip_dir(x)]
                found += [os.path.join(d, f) for f in files]
        for p in found:
            f = os.path.basename(p)
            if any(skip_dir(x) for x in p[len(root):].split("/")[1:-1]): continue
            if f.startswith(".") or f.rsplit(".", 1)[-1].lower() in SKIP_EXT or p in busy: continue
            try: st = os.lstat(p)
            except OSError: continue
            if ok_file(st, t, dev) and rejected.get(p) != [st.st_mtime, st.st_size]: out.append((st.st_size, p))
        for size, p in sorted(out, reverse=True):
            r = compressible(p, size)
            if r: yield p, r

def rewrite(path, compress, ratio=1.0):
    """Rewrites one file with macOS transparent compression on (or off). Programs read it exactly as before. The copy
    is checked byte for byte, and the file is left alone if it changed, is open, or would not shrink."""
    tmp = None
    try:
        st = os.lstat(path)
        if (st.st_mode & 0o170000) != 0o100000 or st.st_nlink != 1: return None
        need = st.st_size * (min(1.0, ratio * 1.5) if compress else 1.0) + 2e9  # the temporary copy must never fill the disk
        if (disk_free_gb() or 0) * 1e9 < need: return None
        tmp = os.path.join(os.path.dirname(path), ".%s.am-tmp" % os.path.basename(path))
        if os.path.lexists(tmp): return None
        with open(DIR + "/compress.current", "w") as f: f.write(tmp)  # a killed worker's copy is removed by the next one
        r = subprocess.run(["/usr/bin/ditto", "--hfsCompression", path, tmp] if compress else ["/bin/cp", "-p", path, tmp], capture_output=True, timeout=900)  # ditto --nohfsCompression keeps it compressed; cp -p writes it out plain
        if r.returncode != 0: return None
        after = os.lstat(tmp).st_blocks * 512
        if compress and after > 0.8 * st.st_blocks * 512: return None
        if subprocess.run(["/usr/bin/cmp", "-s", path, tmp]).returncode != 0: return None
        if is_open(path): return None
        now_st = os.lstat(path)
        if (now_st.st_mtime, now_st.st_size, now_st.st_ino, now_st.st_nlink) != (st.st_mtime, st.st_size, st.st_ino, 1): return None
        os.replace(tmp, path); tmp = None
        return st.st_blocks * 512, after
    except (OSError, subprocess.SubprocessError): return None
    finally:
        try:
            if tmp and os.path.isfile(tmp) and not os.path.islink(tmp): os.remove(tmp)  # our own temporary copy, never the original
        except OSError: pass

def comp_log():
    out = {}
    if os.path.exists(F_COMP):
        for x in open(F_COMP):
            try: e = json.loads(x)
            except ValueError: continue
            if e.get("ev") == "compress": out[e["path"]] = e
            elif e.get("ev") == "restore": out.pop(e["path"], None)
    return out

def cmd_compress(args):
    """Background worker the monitor starts while disk is short: compresses files, largest first, until 25 GB are free."""
    import fcntl
    lk = open(DIR + "/compress.lock", "w")
    try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: return
    saved = n = 0
    try:
        left = open(DIR + "/compress.current").read().strip()
        if left.endswith(".am-tmp") and os.path.basename(left).startswith("."): os.remove(left)
    except OSError: pass
    if "com.apple.TimeMachine." in run(["/usr/bin/tmutil", "listlocalsnapshots", "/System/Volumes/Data"], 30):
        # a local snapshot keeps the old copy of every rewritten file, so compressing would use space; macOS deletes these snapshots itself when space runs low
        jsave(F_CSTATE, dict(exhausted=now() - DISK["retry"] + 1800)); return jl(F_EVENTS, dict(t=int(now()), ev="compressed", files=0, saved=0, why="snapshots"))
    if open_paths() is None:  # lsof failed (very busy Mac): try again in 10 minutes
        jsave(F_CSTATE, dict(exhausted=now() - DISK["retry"] + 600)); return
    rejected = jload(F_CSKIP, {})
    for p, ratio in candidates():
        gb = disk_free_gb()
        if gb is None or gb >= disk_target() + 5: break
        r = rewrite(p, True, ratio)
        try: st = os.lstat(p)
        except OSError: continue
        if r: n += 1; saved += r[0] - r[1]; jl(F_COMP, dict(t=int(now()), ev="compress", path=p, before=r[0], after=r[1], mtime=st.st_mtime))
        elif not st.st_flags & 0x20: rejected[p] = [st.st_mtime, st.st_size]; jsave(F_CSKIP, rejected)  # do not copy it again unless it changes
    else:
        jsave(F_CSTATE, dict(exhausted=now()))  # nothing left to compress: do not walk the disk again for 6 hours
    jl(F_EVENTS, dict(t=int(now()), ev="compressed", files=n, saved=saved))

def cmd_restore(args):
    """Undoes compression (`monitored uncompress`). Only while at least 50 GB stay free afterwards."""
    import fcntl
    lk = open(DIR + "/compress.lock", "w")
    try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: return print("compressor is busy; try again in a minute")
    n = left = 0
    try:
        tl = open(DIR + "/compress.current").read().strip()
        if tl.endswith(".am-tmp") and os.path.basename(tl).startswith("."): os.remove(tl)
    except OSError: pass
    for p, e in comp_log().items():
        try: st = os.lstat(p)
        except OSError: jl(F_COMP, dict(t=int(now()), ev="restore", path=p, gone=True)); continue
        if not st.st_flags & 0x20: jl(F_COMP, dict(t=int(now()), ev="restore", path=p, already=True)); continue  # rewritten since by its program
        gb = disk_free_gb() or 0
        if gb - (e["before"] - e["after"]) / 1e9 < disk_target() + 20: left += 1; continue
        if rewrite(p, False): n += 1; jl(F_COMP, dict(t=int(now()), ev="restore", path=p))
        else: left += 1
    if left and not n: jsave(F_CSTATE, dict(jload(F_CSTATE, {}), restore_blocked=now()))
    if "--quiet" not in args: print("restored %d files; %d still compressed (they stay compressed until the disk target + 20 GB would remain free)" % (n, left))

_worker = [None]
def start_worker(mode):
    """One worker at a time. The lock alone is not enough: at load average 300 a new worker took minutes to reach its
    lock, and a test monitor checking every 2 s started 268 of them."""
    import fcntl
    w = _worker[0]
    if w is not None and w.poll() is None: return False  # the one we started is still running (or still starting)
    try:
        with open(DIR + "/compress.lock", "w") as lk: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB); fcntl.flock(lk, fcntl.LOCK_UN)
    except OSError: return False  # one started by an earlier monitor is running
    urgent = (disk_free_gb() or 0) < disk_target() / 3  # background priority made no progress at load 50; when it is urgent, run at normal priority
    _worker[0] = subprocess.Popen((["/usr/sbin/taskpolicy", "-b"] if not urgent else []) + [sys.executable, "-I", os.path.abspath(__file__), mode] + (["--quiet"] if mode == "uncompress" else []),
                     start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    jl(F_EVENTS, dict(t=int(now()), ev="worker", mode=mode))
    return True

class Mon:
    def __init__(self):
        self.streak, self.calm, self.n = defaultdict(int), 0, 0
        self.last_fg, self.t_prev, self.c_prev = None, now(), sum(os.times()[:4])
        self.sleep, self.batt, self.tk0, self.fg_t = CFG["tick"], (0.0, False), None, 0.0
        self.mhot = self.mcalm = 0
        self.severe, self.fg_pid, self.disk_t, self.dw, self.swap_prev, self.held = False, None, 0.0, {}, None, {}

    def disk(self, t, procs=None, fg_pid=None):
        """Keeps free disk at or above the target (30 GB unless set with `monitored disk-target GB`), once a minute.
        Below the target: compress big idle files until target + 5 GB. Below a third of it while swap keeps growing:
        pause the program using the most memory. Below 3 GB: pause the program writing most to disk. Paused programs
        resume once half the target is free. Above target + 30 GB: undo compression while target + 20 GB stay free."""
        if t - self.disk_t < DISK["every"]: return
        dt, self.disk_t, gb = t - self.disk_t, t, disk_free_gb()
        if gb is None or DRY: return
        T, cs = disk_target(), jload(F_CSTATE, {})
        safe = not TEST_ONLY or os.environ.get("MONITORED_COMPRESS_ROOTS")  # tests never compress real folders
        if safe and gb < T and t - cs.get("exhausted", 0) > DISK["retry"]: start_worker("compress")
        elif safe and gb >= T + 30 and t - cs.get("restore_blocked", 0) > DISK["retry"] and comp_log(): start_worker("uncompress")
        dp = {k: e for k, e in journal().items() if e["act"] == "dpause"}
        self.held = {k: v for k, v in self.held.items() if t - v < 3600}  # released after max-hold: not paused again for an hour
        sw = swap_used_gb()
        grew, self.swap_prev = (sw is not None and self.swap_prev is not None and sw - self.swap_prev >= DISK["swap_grow"]), sw
        if gb >= T / 2:
            for k in dp: undo(int(k), "disk-ok")
            self.dw = {}; return
        procs = procs or sample()
        if not procs: return
        for k, e in dp.items():  # still short: renew the pause (the OS timer resumes it if this monitor dies)
            q = procs.get(int(k))
            if not q or pname(q["cmd"]) != e["name"]: undo(int(k), "gone")  # exited; its pid may now belong to another program
            elif gb < T / 3 and t - e["since"] >= DISK["end_after"] and footprint(int(k)) >= DISK["end_mem"]: end_paused(q, e, footprint(int(k)))
            elif t - e["since"] >= DISK["max_hold"]: undo(int(k), "max-hold"); self.held[int(k)] = t  # never hold anything for days
            else: pause(q, since=e["since"], act="dpause", ttl=DISK["ttl"])
        w = {pid: proc_written(pid) for pid, p in procs.items() if p["uid"] == ME}
        prev, self.dw = self.dw, {k: v for k, v in w.items() if v is not None}
        if len(dp) >= DISK["max_paused"] or not (gb < DISK["pause_below"] or (gb < T / 3 and grew)): return
        if fg_pid is None: fg_pid = foreground(procs)[0]
        if fg_pid is None: return
        apps = gui_pids()
        if gb < DISK["pause_below"]:
            c = [((self.dw[k] - prev[k]) / max(dt, 1e-3), procs[k]) for k in self.dw if k in prev and str(k) not in dp and k not in self.held and emergency_ok(procs[k], fg_pid, apps)]
            c = [x for x in c if x[0] >= DISK["min_rate"]]  # bytes per second written since the last check
            if c:
                d, p = max(c, key=lambda x: x[0])
                return pause(dict(p, rss=footprint(p["pid"])), act="dpause", ttl=DISK["ttl"])
        if gb < T / 3 and grew:  # swap files live on this disk: a growing one eats the room compression made (Nov 19: 24 GB run_bench.py)
            protected, _ = classify(procs, fg_pid)
            c = [(footprint(k), p) for k, p in procs.items() if str(k) not in dp and k not in self.held and not SYSTEM_PATH.match(p["cmd"]) and (pausable(p, protected, apps) or emergency_ok(p, fg_pid, apps))]
            c = [x for x in c if x[0] >= DISK["min_mem"]]
            if c:
                f, p = max(c, key=lambda x: x[0])
                pause(dict(p, rss=f), act="dpause", ttl=DISK["ttl"])

    def on_battery(self):  # checked at most once a minute
        if now() - self.batt[0] > 60: self.batt = (now(), "Battery Power" in run(["/usr/bin/pmset", "-g", "batt"]))
        return self.batt[1]

    def cheap(self):  # whole-machine CPU use since the last tick, from kernel counters
        tk, use = cpu_ticks(), None
        if tk and self.tk0:
            d = [a - b for a, b in zip(tk, self.tk0)]; tot = sum(d)
            use = 1.0 - d[2] / tot if tot > 0 else None
        self.tk0 = tk
        return use

    def light(self, use, lvl, swap, po, t):  # nothing is contended: no process scan, no program spawned except one foreground check every 10 s
        warn_check(lvl, swap, False); self.disk(t); self.streak.clear()  # hot counts from before a calm stretch must not carry over
        if t - self.fg_t >= 10:
            self.fg_t = t; fg = fg_light()
            if fg != self.last_fg: jl(F_FG, dict(t=int(t), app=fg)); self.last_fg = fg
        self.sleep = ((10.0 if self.on_battery() else 6.0) if CFG["adaptive"] else CFG["tick"])
        ct = sum(os.times()[:4]); ovh = 100.0 * (ct - self.c_prev) / max(1e-6, t - self.t_prev); self.c_prev, self.t_prev = ct, t
        st = dict(ts=t, level=lvl, swap=round(swap, 3), pageout_rate=round(po, 1), cpu_frac=round(use or 0.0, 3), cpu_hot=False, mem_hot=False, hold=False, dry=DRY, fg=self.last_fg,
                  demoted=[], own_cpu_pct=round(ovh, 2), mode="light")
        jsave(F_STATUS, st)
        if self.n % 5 == 0: jl(F_LOG, dict(t=int(t), lv=lvl, sw=round(swap, 2), po=round(po), cpu=round(use or 0.0, 2), hold=False, nd=0, own=round(ovh, 1)))
        return st

    def relieve(self, procs, protected, mem_hot, t):
        """Memory short for 2 checks: pause the biggest background program. Memory fine for 3 checks: resume one per check."""
        self.mhot, self.mcalm = (self.mhot + 1, 0) if mem_hot else (0, self.mcalm + 1)
        paused = {k: e for k, e in journal().items() if e["act"] == "pause"}
        for k, e in paused.items():
            if int(k) in procs and pname(procs[int(k)]["cmd"]) != e["name"]: undo(int(k), "gone"); continue  # pid reused by another program
            if int(k) in procs and mem_hot and t - e["t"] >= 60:
                if t - e["since"] < MEM["max_hold"]: pause(procs[int(k)], since=e["since"], emg=e.get("emg", False))
                else: undo(int(k), "max-hold")  # never keep anything frozen for long
        if self.mhot >= MEM["hot_ticks"] and len(paused) < MEM["max_paused"]:
            apps = gui_pids()
            cands = [(footprint(p["pid"]), p) for p in procs.values() if str(p["pid"]) not in paused and pausable(p, protected, apps)]
            cands = sorted((c for c in cands if c[0] >= MEM["min_rss"]), key=lambda c: -c[0])
            if not cands and self.severe:  # swap nearly full and nothing ordinary to pause: the 2.3 GB script under Claude froze the Mac on Nov 19
                cands = sorted((c for c in ((footprint(p["pid"]), p) for p in procs.values() if str(p["pid"]) not in paused and emergency_ok(p, self.fg_pid, apps)) if c[0] >= MEM["min_rss"]), key=lambda c: -c[0])
                emg = bool(cands)
            else: emg = False
            if cands: p = dict(cands[0][1], rss=cands[0][0]); pause(p, emg=emg); self.mhot = 0  # one at a time: give the Mac a check to recover before the next
        elif self.mcalm >= MEM["calm_ticks"] and paused:
            undo(int(min(paused, key=lambda k: paused[k]["rss"])), "memory-ok"); self.mcalm = 0  # smallest first: least likely to refill memory

    def tick(self):
        if os.environ.get("MONITORED_FAULT"): raise RuntimeError("fault injection (tests only)")
        use = self.cheap(); lvl, swap, po = memory(); t = now(); self.n += 1
        if not (use is None or use >= CFG["scan_at"] or journal() or lvl >= 4 or (lvl >= 2 and po >= CFG["pageout_hot"])): return self.light(use, lvl, swap, po, t)
        procs = sample()
        if not procs: return  # ps timed out: an empty table would mark every journal pid dead and drop its undo
        fg_pid, fg = foreground(procs)
        if fg_pid is None: return  # front app unknown (lsappinfo timed out): acting now could slow the app you are using
        if fg != self.last_fg: jl(F_FG, dict(t=int(t), app=fg)); self.last_fg = fg
        # Demand, not usage: a demoted process is throttled hard, so count what it was using when we demoted it (valid 120 s). Without this the monitor flaps.
        J0 = journal(); dem = {int(k): e for k, e in J0.items() if int(k) in procs and t - e["t"] < 120}
        cpu_frac = (sum(p["cpu"] for p in procs.values()) - sum(procs[k]["cpu"] for k in dem) + sum(e.get("cpu", 0.0) for e in dem.values())) / (100.0 * NCPU)
        cpu_hot, mem_hot = cpu_frac >= CFG["cpu_busy"], (lvl >= 4 or (lvl >= 2 and po >= CFG["pageout_hot"]))
        protected, priority = classify(procs, fg_pid)
        self.severe, self.fg_pid = (lvl >= 4 or swap >= WARN["swap_full"]), fg_pid
        for k, e in list(journal().items()):  # a slowed program that is now protected (you switched to it) is restored at once
            if int(k) in protected and not e.get("emg") and e["act"] != "dpause": undo(int(k), "protected")  # emergency pauses end by their own rules
        for x in [x for x in self.streak if x not in procs]: del self.streak[x]
        cands = []
        for p in procs.values():
            if p["pid"] in protected or not eligible(p): self.streak.pop(p["pid"], None); continue
            self.streak[p["pid"]] = self.streak[p["pid"]] + 1 if p["cpu"] >= CFG["hog_cpu"] else 0
            if p["cpu"] >= 5: cands.append(p)
        cands.sort(key=lambda p: -p["cpu"])
        hogs = [p for p in cands if self.streak[p["pid"]] >= CFG["hog_ticks"]]
        J = journal()
        for pid in list(J):  # housekeeping: forget dead pids
            if int(pid) not in procs: cancel_timer(J.pop(pid)); jsave(F_JOURNAL, J)
        if cpu_hot:
            self.calm = 0
            nd = len(J)
            for p in hogs:
                e = J.get(str(p["pid"]))
                if e and (e["act"] != "demote" or t - e["t"] < 60): continue
                if not e and nd >= CFG["max_demote"]: continue
                if apply("demote", p, CFG["demote_ttl"]) and not e: nd += 1
            for k, e in list(J.items()):  # keep demotions alive while demand stays high (the OS-level timer would otherwise end them at 90 s)
                if e["act"] == "demote" and int(k) in procs and t - e["t"] >= 60: apply("demote", procs[int(k)], CFG["demote_ttl"])
        else:
            self.calm += 1
            if self.calm >= CFG["calm_ticks"] and J:
                for pid, e in list(J.items()):
                    if e["act"] == "demote" and t - e["t"] >= 30: undo(int(pid), "calm")  # minimum hold 30 s
        self.relieve(procs, protected, mem_hot, t)
        self.disk(t, procs, fg_pid)
        J = journal(); warn_check(lvl, swap, mem_hot)
        calm = cpu_frac < CFG["scan_at"] and not mem_hot and not J  # calm: check less often (6 s on power, 10 s on battery); busy: every 3 s
        self.sleep = ((10.0 if self.on_battery() else 6.0) if calm else (5.0 if CFG["saver"] and self.on_battery() else CFG["tick"])) if CFG["adaptive"] else CFG["tick"]
        ct = sum(os.times()[:4]); ovh = 100.0 * (ct - self.c_prev) / max(1e-6, t - self.t_prev); self.c_prev, self.t_prev = ct, t
        st = dict(ts=t, level=lvl, swap=round(swap, 3), pageout_rate=round(po, 1), cpu_frac=round(cpu_frac, 3), cpu_hot=cpu_hot, mem_hot=mem_hot, hold=mem_hot, dry=DRY,
                  fg=fg, demoted=sorted({e["name"] for e in J.values()}), own_cpu_pct=round(ovh, 2))
        jsave(F_STATUS, st)
        if cpu_hot or mem_hot or J or self.n % 5 == 0:
            jl(F_LOG, dict(t=int(t), lv=lvl, sw=round(swap, 2), po=round(po), cpu=round(cpu_frac, 2), hold=mem_hot, nd=len(st["demoted"]), own=round(ovh, 1)))
        return st

def cmd_run(args):
    global DRY
    ticks = None
    DRY = "--dry" in args
    if "--saver" in args: CFG["saver"] = True
    if "--ticks" in args: ticks = int(args[args.index("--ticks") + 1])
    if "--tick" in args: CFG["tick"] = float(args[args.index("--tick") + 1]); CFG["adaptive"] = False
    for k in ("hog_ticks", "hog_cpu"):  # hog_cpu: tests lower it, because macOS ps shows a new process's CPU use only after about 10 s
        if "--" + k in args: CFG[k] = float(args[args.index("--" + k) + 1])
    import fcntl
    lk = open(DIR + "/lock", "w")
    try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: return print("monitored is already running for", DIR)  # one monitor per state folder
    revert_all("startup")
    def bye(*a): revert_all("exit"); sys.exit(0)
    signal.signal(signal.SIGINT, bye); signal.signal(signal.SIGTERM, bye)
    m, n = Mon(), 0
    try:
        errs = 0
        while ticks is None or n < ticks:
            t0 = now()
            try: m.tick(); errs = 0
            except Exception as e:  # one bad tick (full disk, odd process line) must not stop the monitor; 10 in a row means something is wrong, so exit and let the login item restart it
                errs += 1
                try: jl(F_EVENTS, dict(t=int(now()), ev="error", msg=repr(e)[:200]))
                except Exception: pass
                if errs >= 10: sys.exit(1)
            n += 1
            time.sleep(max(0.2, m.sleep - (now() - t0)))
    finally:
        revert_all("exit")

def status_json(args):
    s = jload(F_STATUS, {})
    print(json.dumps(s, indent=1) if s and now() - s.get("ts", 0) < 30 else "monitor not running (no fresh status)")
    print("journal:", journal())

def hold_now():
    s = jload(F_STATUS, {})
    if s and now() - s.get("ts", 0) < 15: return bool(s.get("hold")) and not s.get("dry"), "monitor"
    lvl, _, po = memory()  # no monitor running: only the kernel's critical level counts, and never add a wait to a calm start
    return lvl >= 4, "direct"

def cmd_gate(args):
    mw = 120.0
    head = args[:args.index("--")] if "--" in args else args  # flags after -- belong to the command
    if "--max-wait" in head: mw = float(head[head.index("--max-wait") + 1])
    cmd = args[args.index("--") + 1:] if "--" in args else []
    if not cmd: sys.exit("usage: monitored.py gate [--max-wait S] -- CMD ...")
    t0, held = now(), False
    while True:
        h, src = hold_now()
        if not h: break
        held = True
        if now() - t0 >= mw: sys.stderr.write("monitored gate: still thrashing after %.0f s, starting anyway\n" % (now() - t0)); break
        time.sleep(2)
    jl(F_EVENTS, dict(t=int(now()), ev="gate", waited=round(now() - t0, 1), held=held, cmd=cmd[0]))
    if held: sys.stderr.write("monitored gate: waited %.0f s\n" % (now() - t0))
    try: os.execvp(cmd[0], cmd)
    except OSError as e: sys.exit("monitored gate: cannot run %s: %s" % (cmd[0], e.strerror))

# ---------- install, settings, friendly commands ----------
__version__ = "0.6.0"
LABEL = os.environ.get("MONITORED_LABEL", "com.activitymonitored.agent")
PLIST = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")
F_SET = DIR + "/settings.json"
SELF = os.path.abspath(__file__)
DEFAULTS = {"mode": "on", "battery_saver": True, "disk_target_gb": 30}

def settings():
    s = dict(DEFAULTS); s.update(jload(F_SET, {})); return s

def lc(*a): return subprocess.run(["/bin/launchctl"] + list(a), capture_output=True, text=True)
def running(): return lc("list", LABEL).returncode == 0

def stable_python():
    for p in ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(p) and subprocess.run([p, "-c", "import sys; sys.exit(sys.version_info < (3, 8))"], capture_output=True).returncode == 0: return p
    return sys.executable

def stop_agent():
    if running(): lc("bootout", "gui/%d/%s" % (ME, LABEL)); time.sleep(1)
    revert_all("stop")  # undo every slowdown

def write_plist(s):
    import plistlib
    args = [stable_python(), "-I", DIR + "/monitored.py", "run"]
    if s["mode"] == "watch": args.append("--dry")
    if s["battery_saver"]: args.append("--saver")
    d = {"Label": LABEL, "ProgramArguments": args, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, "ProcessType": "Interactive",
         "LowPriorityIO": True, "StandardOutPath": DIR + "/launchd.log", "StandardErrorPath": DIR + "/launchd.log",
         "EnvironmentVariables": {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:" + os.path.join(HOME, ".local", "bin")}}
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f: plistlib.dump(d, f)

def apply_settings():
    s = settings(); stop_agent()
    if s["mode"] == "off":
        if os.path.exists(PLIST): os.remove(PLIST)  # otherwise launchd starts it again at the next login
        print("Activity Monitored is off. Nothing is running and nothing is slowed."); return 0
    if not os.path.exists(DIR + "/monitored.py"): print("Activity Monitored is not installed. Run: python3 monitored.py install"); return 1
    write_plist(s); r = lc("bootstrap", "gui/%d" % ME, PLIST)
    if r.returncode != 0: print("macOS refused to start Activity Monitored:", r.stderr.strip()); return 1
    time.sleep(3)
    if s["mode"] == "watch": print("Activity Monitored is watching only. It logs what it would do and changes nothing.")
    else: print("Activity Monitored is on. It slows busy background programs only while your Mac is overloaded, then restores them.")
    print("Battery saver: %s. Running: %s." % ("on" if s["battery_saver"] else "off", "yes" if running() else "no"))
    return 0

def cmd_install(args):
    s = settings()
    if "--mode" in args: s["mode"] = args[args.index("--mode") + 1]
    if s["mode"] not in ("on", "watch", "off"): print("mode must be on, watch or off"); return 1
    os.makedirs(DIR, exist_ok=True); os.chmod(DIR, 0o700)
    if os.path.abspath(DIR + "/monitored.py") != SELF: shutil.copy(SELF, DIR + "/monitored.py")
    shim = DIR + "/monitored"
    with open(shim, "w") as f: f.write('#!/bin/bash\nexec "%s" -I "%s/monitored.py" "$@"\n' % (stable_python(), DIR))
    os.chmod(shim, 0o755)
    link = os.path.join(HOME, ".local", "bin", "monitored")
    try:
        if os.path.isdir(os.path.dirname(link)) and not os.path.lexists(link): os.symlink(shim, link)
    except OSError: pass
    jsave(F_SET, s); rc = apply_settings()
    cmd = "monitored" if shutil.which("monitored") else shim.replace(HOME, "~")
    print("\nCheck it any time:  %s status\nTurn it off:        %s off\nRemove it:          %s uninstall" % (cmd, cmd, cmd))
    return rc

def cmd_uninstall(args):
    stop_agent()
    if os.path.exists(PLIST): os.remove(PLIST)
    link = os.path.join(HOME, ".local", "bin", "monitored")
    try:
        if os.path.islink(link) and os.path.realpath(link) == os.path.realpath(DIR + "/monitored"): os.remove(link)
    except OSError: pass
    for f in ("monitored.py", "monitored", "settings.json"):
        try: os.remove(DIR + "/" + f)
        except OSError: pass
    if "--purge" in args: shutil.rmtree(DIR, ignore_errors=True)
    print("Activity Monitored is removed. Nothing is running and nothing is slowed." + ("" if "--purge" in args else " Logs stay in %s (add --purge to delete them)." % DIR.replace(HOME, "~")))

def set_key(k, v):
    s = settings(); s[k] = v; os.makedirs(DIR, exist_ok=True); jsave(F_SET, s); return apply_settings()

def cmd_toggle(key, args):
    if args[:1] not in (["on"], ["off"]): print("usage: monitored battery on|off"); return 1
    return set_key(key, args[0] == "on")

def cmd_disk_target(args):
    try: v = float(args[0]); assert 5 <= v <= 500
    except (IndexError, ValueError, AssertionError): print("usage: monitored disk-target GB  (5 to 500; now %.0f)" % disk_target()); return 1
    s = settings(); s["disk_target_gb"] = v; jsave(F_SET, s); print("Activity Monitored keeps at least %.0f GB free." % v)

def cmd_protect(args):
    if not args: print("usage: monitored protect NAME"); return 1
    with open(DIR + "/protect.txt", "a") as f: f.write(" ".join(args) + "\n")
    print("Activity Monitored will never touch programs whose command contains '%s'." % " ".join(args)); return 0

def cmd_status(args):
    s, st = settings(), jload(F_STATUS, {})
    fresh = bool(st) and now() - st.get("ts", 0) < 30
    word = {"on": "on", "watch": "watching only", "off": "off"}.get(s["mode"], s["mode"])
    print("Activity Monitored is %s (battery saver %s). %s" % (word, "on" if s["battery_saver"] else "off", "Running." if (fresh or running()) else "Not running."))
    if fresh:
        lvl = {1: "normal", 2: "warning", 4: "critical"}.get(st.get("level"), str(st.get("level")))
        print("CPU busy: %d%%. Memory pressure: %s. Front app: %s." % (round(100 * st.get("cpu_frac", 0)), lvl, st.get("fg") or "unknown"))
        print("Slowed or paused right now: %s." % (", ".join(st.get("demoted", [])) or "nothing"))
    ev = []
    if os.path.exists(F_EVENTS):
        for x in open(F_EVENTS):
            try: e = json.loads(x)
            except ValueError: continue
            if now() - e.get("t", 0) < 86400: ev.append(e)
    names = defaultdict(int)
    for e in ev:
        if e.get("ev") in ("demote", "would_demote", "pause", "would_pause", "dpause", "would_dpause"): names[e.get("name", "?")] += 1
    print("Last 24 hours: %d slowdowns or pauses%s." % (sum(names.values()), (", most often " + ", ".join("%s (%d)" % kv for kv in sorted(names.items(), key=lambda kv: -kv[1])[:3])) if names else ""))
    gb = disk_free_gb()
    if gb is not None: print("Disk free: %.0f GB (target %.0f GB%s)." % (gb, disk_target(), "; below it, making room" if gb < disk_target() else ""))
    cl = comp_log()
    if cl: print("Compressed to free space: %d files, %.1f GB saved. Nothing was deleted; files open exactly as before. Undo: monitored uncompress (runs by itself above 60 GB free)."
                 % (len(cl), sum(e["before"] - e["after"] for e in cl.values()) / 1e9))
    for e in [e for e in ([json.loads(x) for x in open(F_STOPPED)] if os.path.exists(F_STOPPED) else []) if now() - e.get("t", 0) < 7 * 86400][-3:]:
        print("Ended to keep the Mac running: %s (%.0f GB). Rerun: cd %s && %s" % (e["name"], e["gb"], e["cwd"] or "~", e["cmd"]))
    for e in [e for e in ev if e.get("ev") == "warn"][-3:]:
        m = int((now() - e.get("t", 0)) // 60)
        print("Noted (%s ago): %s" % ("%d h" % (m // 60) if m >= 60 else "%d min" % m, e.get("msg", "")))

if __name__ == "__main__":
    a = sys.argv[1:]; c = a[0] if a else ""
    table = {"run": cmd_run, "gate": cmd_gate, "revert-all": lambda x: revert_all(), "status": cmd_status, "install": cmd_install,
             "uninstall": cmd_uninstall, "protect": cmd_protect, "on": lambda x: set_key("mode", "on"), "watch": lambda x: set_key("mode", "watch"),
             "off": lambda x: set_key("mode", "off"), "battery": lambda x: cmd_toggle("battery_saver", x),
             "disk-target": cmd_disk_target,
             "version": lambda x: print(__version__), "status-json": status_json,
             "compress": cmd_compress, "uncompress": cmd_restore}
    if c not in table:
        print(__doc__)
        sys.exit(0 if c in ("", "help", "-h", "--help") else "monitored: unknown command: %s" % c)
    sys.exit(table[c](a[1:]) or 0)
