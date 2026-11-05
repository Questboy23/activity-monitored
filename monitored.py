#!/usr/bin/env python3
"""Activity Monitored: keeps a Mac responsive when it is overloaded. macOS only, Python 3.8+, no packages to install.
While the CPU is oversubscribed, Activity Monitored lowers the priority of busy background programs (taskpolicy -b), then restores them. Every action is undone by a timer.
  monitored install           install and start (one command; see README)
  monitored status            what Activity Monitored is doing right now and in the last 24 hours
  monitored on | watch | off  on: act. watch: log what it would do, change nothing. off: stop and undo everything
  monitored ai on|off         let a Claude model judge unfamiliar situations (default off; uses your own claude login)
  monitored battery on|off    battery saver: checks less often and makes no model calls while on battery
  monitored protect NAME      never touch programs whose command contains NAME
  monitored gate [--max-wait S] -- CMD   start CMD once memory is not thrashing (waits at most S seconds)
  monitored report | privacy | uninstall [--purge] | version
"""
import ctypes, json, os, re, shutil, signal, subprocess, sys, threading, time
from collections import defaultdict

HOME = os.path.expanduser("~")
DIR = os.environ.get("MONITORED_DIR", os.path.join(HOME, ".monitored"))
os.makedirs(DIR, exist_ok=True)
try: os.chmod(DIR, 0o700)
except OSError: pass
F_STATUS, F_JOURNAL, F_CACHE = DIR + "/status.json", DIR + "/journal.json", DIR + "/decisions.json"
F_LOG, F_EVENTS, F_FG = DIR + "/log.jsonl", DIR + "/events.jsonl", DIR + "/foreground.jsonl"
NCPU, ME = os.cpu_count() or 8, os.getuid()
CFG = dict(tick=3.0, hog_cpu=20.0, hog_ticks=3, cpu_busy=0.75, calm_ticks=3, demote_ttl=90, pause_ttl=20,
           pause_cooldown=120, max_demote=40, max_pause=0, pageout_hot=60.0, ai_every=120, ai_timeout=40, adaptive=True, scan_at=0.72, saver=False)

# Never touched, by command text. System daemons are excluded by path unless on the BG_OK list.
NEVER = re.compile(r"/(WindowServer|kernel_task|launchd|loginwindow|Finder|Dock|SystemUIServer|coreaudiod|bluetoothd|sshd|tmux|Terminal|iTerm2?|Activity Monitor)(\.app|$| )|monitored\.py")
SYSTEM_PATH = re.compile(r"^(/System/|/usr/|/sbin/|/bin/|/Library/Apple/)")
BG_OK = {"photoanalysisd", "mediaanalysisd", "mdworker_shared", "mds_stores", "backupd", "suggestd", "parsecd", "siriknowledged", "cloudd"}
# Work that matters: never demoted, and so is everything under it. Extend as needed (the AI can add names for the session).
PRIORITY = re.compile(r"xcodebuild|swift-frontend|clang|ffmpeg|HandBrake|Final Cut|Premiere|DaVinci|Blender|Compressor|Docker|Parallels|VMware|UTM|qemu|postgres|mysqld|mongod|redis-server|nginx|gradle|\bcmake\b|\bninja\b|\bmake\b|ollama|llama-server|Spotify|zoom\.us|FaceTime|Discord|OBS|Logic Pro|GarageBand|Ableton|/Music\.app/|Microsoft Teams|/Claude\.app/|\.local/share/claude/versions|/claude( |$)|pytest|cargo|rustc|\bnpm run\b|vite|webpack|esbuild|\btsc\b")

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
        if os.path.getsize(path) > 4_000_000: os.replace(path, path + ".1")  # at most two files of 4 MB per log
    except OSError: pass
    with open(path, "a") as f: f.write(json.dumps(obj, separators=(",", ":")) + "\n")
def jload(path, default):
    try:
        with open(path) as f: return json.load(f)
    except Exception: return default
def jsave(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w") as f: json.dump(obj, f)
    os.replace(tmp, path)

def sample():
    out = run(["ps", "-axo", "pid=,ppid=,uid=,pcpu=,rss=,stat=,command="], 8)
    procs = {}
    for ln in out.splitlines():
        p = ln.split(None, 6)
        if len(p) < 7: continue
        try:
            pid = int(p[0]); procs[pid] = dict(pid=pid, ppid=int(p[1]), uid=int(p[2]), cpu=float(p[3]), rss=int(p[4]) * 1024, stat=p[5], cmd=p[6])
        except ValueError: continue
    return procs

def pname(cmd):
    return os.path.basename(re.split(r" --?\w", cmd)[0]).strip()[:40]

_last_po = [None, None]
def memory_slow():
    if os.environ.get("MONITORED_FAKE_LEVEL"):
        return int(os.environ["MONITORED_FAKE_LEVEL"]), 0.9, float(os.environ.get("MONITORED_FAKE_PAGEOUT", "0"))
    lvl = int(run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"]).strip() or 1)
    m = re.findall(r"([\d.]+)M", run(["sysctl", "-n", "vm.swapusage"]))
    swap = float(m[1]) / float(m[0]) if len(m) >= 2 and float(m[0]) else 0.0
    mo = re.search(r"Pageouts:\s+(\d+)", run(["vm_stat"]))
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
        m = re.findall(r"([\d.]+)M", run(["sysctl", "-n", "vm.swapusage"]))
        _swap_c[0], _swap_c[1] = t, (float(m[1]) / float(m[0]) if len(m) >= 2 and float(m[0]) else 0.0)
    rate = 0.0
    if _last_po[0] is not None and t > _last_po[1]: rate = max(0.0, (po_total - _last_po[0]) / (t - _last_po[1]))
    _last_po[0], _last_po[1] = po_total, t
    return lvl, _swap_c[1], rate

def fg_light():
    a = run(["lsappinfo", "front"]).strip()
    m = re.search(r'"LSDisplayName"="([^"]*)"', run(["lsappinfo", "info", "-only", "name", a])) if a else None
    return m.group(1) if m else None

def foreground(procs):
    a = run(["lsappinfo", "front"]).strip()
    m = re.search(r'"pid"=(\d+)', run(["lsappinfo", "info", "-only", "pid", a])) if a else None
    pid = int(m.group(1)) if m else None
    return pid, (pname(procs[pid]["cmd"]) if pid in procs else None)

def tree(kids, root):
    s, st = set(), [root]
    while st:
        x = st.pop()
        if x in s: continue
        s.add(x); st.extend(kids.get(x, []))
    return s

def classify(procs, fg_pid, ai_protect):
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
        if pname(p["cmd"]) in ai_protect: priority |= tree(kids, p["pid"])
    return protected | priority, priority

def eligible(p):
    if p["uid"] != ME: return False
    n = pname(p["cmd"])
    if SYSTEM_PATH.match(p["cmd"]) and n not in BG_OK: return False
    return "T" not in p["stat"]

# ---------- executor: every action is undoable and carries its own OS-level undo timer ----------
def dead_man(undo, ttl):
    subprocess.Popen(["/bin/sh", "-c", "sleep %d; %s" % (ttl, undo)], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def journal(): return jload(F_JOURNAL, {})

DRY, _would = False, {}
def apply(act, p, ttl):
    pid, J = p["pid"], journal()
    if DRY:  # log what would happen, change nothing
        if now() - _would.get((pid, act), 0) > 60:
            _would[(pid, act)] = now(); jl(F_EVENTS, dict(t=int(now()), ev="would_" + act, pid=pid, name=pname(p["cmd"]), cpu=p["cpu"]))
        return False
    if act == "demote":
        if subprocess.run(["taskpolicy", "-b", "-p", str(pid)], capture_output=True).returncode != 0: return False
        dead_man("taskpolicy -B -p %d" % pid, ttl)
    elif act == "pause":
        try: os.kill(pid, signal.SIGSTOP)
        except OSError: return False
        dead_man("kill -CONT %d" % pid, ttl)
    old = J.get(str(pid)); refresh = bool(old and old["act"] == act)
    J[str(pid)] = dict(act=act, name=pname(p["cmd"]), t=now(), ttl=ttl, cpu=(old.get("cpu", p["cpu"]) if refresh else p["cpu"]))
    jsave(F_JOURNAL, J)
    if not refresh: jl(F_EVENTS, dict(t=int(now()), ev=act, pid=pid, name=pname(p["cmd"]), cpu=p["cpu"]))
    return True

def undo(pid, why):
    J = journal(); e = J.pop(str(pid), None)
    if e is None: return
    if e["act"] == "demote": subprocess.run(["taskpolicy", "-B", "-p", str(pid)], capture_output=True)
    else:
        try: os.kill(int(pid), signal.SIGCONT)
        except OSError: pass
    jsave(F_JOURNAL, J)
    jl(F_EVENTS, dict(t=int(now()), ev="undo_" + e["act"], pid=int(pid), name=e["name"], why=why))

def revert_all(why="revert-all"):
    for pid in list(journal()): undo(pid, why)

# ---------- AI layer: decision cache first, one small model call only on a new situation ----------
# Without these flags one headless call loaded every plugin and hook: measured 15.7 s and 46 CPU-seconds. With them: 2 to 4 s and 3 to 6 CPU-seconds.
LEAN_FLAGS = ["--setting-sources", "", "--strict-mcp-config", "--exclude-dynamic-system-prompt-sections", "--tools", "", "--max-turns", "1", "--disable-slash-commands", "--no-session-persistence"]

def ai_cmd():
    if os.environ.get("MONITORED_AI_CMD"): return os.environ["MONITORED_AI_CMD"].split()
    for c in (os.path.join(HOME, ".local/bin/claude"), shutil.which("claude")):
        if c and os.path.exists(c): return [c, "-p", "--model", "haiku"] + LEAN_FLAGS
    return None

_SECRET = re.compile(r"(?i)(sk-[A-Za-z0-9_\-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|AKIA[0-9A-Z]{12,}|xox[abp]-[A-Za-z0-9\-]{10,}|eyJ[A-Za-z0-9_\-]{20,}|(?:key|token|secret|passw(?:or)?d|auth|bearer)[=: ]+\S+|[A-Za-z0-9+/=_\-]{40,})")
def redact(t):
    """Everything sent to the model passes through here: secrets and long random-looking strings are removed, the home folder is shortened to ~."""
    return _SECRET.sub("[redacted]", t.replace(HOME, "~"))

def llm_json(prompt):
    cmd = ai_cmd()
    if not cmd: return None
    prompt = redact(prompt)
    try:
        fd = os.open(DIR + "/last_prompt.txt", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f: f.write(prompt)  # exactly what the last model call received, for `ctl.py privacy`
    except OSError: pass
    try:
        out = subprocess.run(cmd + [prompt], capture_output=True, text=True, errors="replace", timeout=CFG["ai_timeout"], stdin=subprocess.DEVNULL).stdout
        m = re.search(r"\{.*\}", out, re.S)
        return json.loads(m.group(0)) if m else None
    except Exception:
        return None

def inspect(pid):
    """Read-only look at one process: what started it, how long it has run, where it runs. Cannot change anything."""
    ps = run(["ps", "-o", "etime=,ppid=,command=", "-p", str(pid)]).strip()[:260]
    cwd = [l[1:] for l in run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"], 4).splitlines() if l.startswith("n")]
    return dict(pid=pid, etime_ppid_command=ps, cwd=(cwd[:1] or ["?"])[0])

def agent(ctx, cands, mode):
    """Up to 3 rounds: the model may ask to inspect up to 4 processes per round, then must return actions + advice.
    Actions are validated by code (pids from the list only, 3 reversible verbs only). Advice is text for the human and is never executed."""
    ids, seen = {c["pid"] for c in cands}, []
    lines = "\n".join("%d,%s,%.0f,%d" % (c["pid"], pname(c["cmd"]), c["cpu"], c["rss"] // 2**20) for c in cands)
    for rnd in range(3):
        if mode == "stub":
            raw = {"inspect": [c["pid"] for c in cands[:2]]} if rnd == 0 else {"actions": [dict(do="demote", pid=c["pid"], why="stub") for c in cands[:5]], "advice": ["stub: consider quitting the biggest idle app"]}
        else:
            prompt = ("You are a careful agent managing CPU and memory on one user's Mac. You may only take REVERSIBLE actions. Reply with JSON only, no prose.\n"
                      'To learn more first, reply {"inspect":[pid,...]} (max 4 pids, read-only). ' if rnd < 2 else "You must now give the final answer. ") + (
                      'Final reply: {"actions":[{"do":"demote|pause|protect","pid":N,"why":"short"}],"advice":["irreversible things the human could do, e.g. quit X; these are never executed"]}. '
                      "demote = lower to background priority (default). pause = freeze up to 20 s (only non-interactive batch work over 80% CPU). protect = leave alone (anything that looks like the user's own work). At most 5 actions; pids only from the list.\n"
                      + json.dumps(ctx) + "\nCANDIDATES (pid,name,cpu%,rssMB):\n" + lines + ("\nINSPECTED:\n" + json.dumps(seen) if seen else ""))
            raw = llm_json(prompt)
        if raw is None: return None
        if "inspect" in raw and rnd < 2:
            seen += [inspect(x) for x in raw["inspect"][:4] if isinstance(x, int) and x in ids]
            continue
        return raw
    return None

def valid_plan(raw, cands):
    ok, pids = [], {c["pid"] for c in cands}
    for a in (raw or {}).get("actions", [])[:5]:
        if a.get("do") in ("demote", "pause", "protect") and isinstance(a.get("pid"), int) and a["pid"] in pids:
            ok.append(dict(do=a["do"], pid=a["pid"], why=str(a.get("why", ""))[:80]))
    return ok

def sig(fg, cands, cpu_hot, mem_hot):
    return "|".join([fg or "-", "c" if cpu_hot else "-", "m" if mem_hot else "-"] + sorted({pname(c["cmd"]) for c in cands[:3]}))

# ---------- monitor loop ----------
class Mon:
    def __init__(self, mode):
        self.mode, self.streak, self.calm = mode, defaultdict(int), 0
        self.cache, self.ai_protect, self.cool = jload(F_CACHE, {}), set(), {}
        self.byname = {a["name"]: a["do"] for lst in self.cache.values() for a in lst}
        self.th, self.res, self.last_ai, self.n, self.ai_calls, self.hits, self.advice = None, None, 0.0, 0, 0, 0, []
        self.last_fg, self.t_prev, self.c_prev = None, now(), sum(os.times()[:4])
        self.sleep, self.batt, self.tk0, self.fg_t = CFG["tick"], (0.0, False), None, 0.0

    def on_battery(self):  # checked at most once a minute
        if now() - self.batt[0] > 60: self.batt = (now(), "Battery Power" in run(["pmset", "-g", "batt"]))
        return self.batt[1]

    def cheap(self):  # whole-machine CPU use since the last tick, from kernel counters
        tk, use = cpu_ticks(), None
        if tk and self.tk0:
            d = [a - b for a, b in zip(tk, self.tk0)]; tot = sum(d)
            use = 1.0 - d[2] / tot if tot > 0 else None
        self.tk0 = tk
        return use

    def light(self, use, lvl, swap, po, t):  # nothing is contended: no process scan, no program spawned except one foreground check every 10 s
        if t - self.fg_t >= 10:
            self.fg_t = t; fg = fg_light()
            if fg != self.last_fg: jl(F_FG, dict(t=int(t), app=fg)); self.last_fg = fg
        self.sleep = ((10.0 if self.on_battery() else 6.0) if CFG["adaptive"] else CFG["tick"])
        ct = sum(os.times()[:4]); ovh = 100.0 * (ct - self.c_prev) / max(1e-6, t - self.t_prev); self.c_prev, self.t_prev = ct, t
        st = dict(ts=t, level=lvl, swap=round(swap, 3), pageout_rate=round(po, 1), cpu_frac=round(use or 0.0, 3), cpu_hot=False, mem_hot=False, hold=False, dry=DRY, fg=self.last_fg,
                  demoted=[], paused=[], ai_calls=self.ai_calls, cache_hits=self.hits, advice=self.advice, own_cpu_pct=round(ovh, 2), mode="light")
        jsave(F_STATUS, st)
        if self.n % 5 == 0: jl(F_LOG, dict(t=int(t), lv=lvl, sw=round(swap, 2), po=round(po), cpu=round(use or 0.0, 2), hold=False, nd=0, np=0, own=round(ovh, 1)))
        return st

    def ai_worker(self, ctx, cands, s):
        raw = agent(ctx, cands, self.mode)
        self.res = (s, valid_plan(raw, cands), cands, [str(x)[:160] for x in (raw or {}).get("advice", [])[:3]]) if raw is not None else (s, None, cands, [])

    def tick(self):
        if os.environ.get("MONITORED_FAULT"): raise RuntimeError("fault injection (tests only)")
        use = self.cheap(); lvl, swap, po = memory(); t = now(); self.n += 1
        if not (use is None or use >= CFG["scan_at"] or journal() or lvl >= 4 or (lvl >= 2 and po >= CFG["pageout_hot"])): return self.light(use, lvl, swap, po, t)
        procs = sample(); fg_pid, fg = foreground(procs)
        if fg != self.last_fg: jl(F_FG, dict(t=int(t), app=fg)); self.last_fg = fg
        # Demand, not usage: a demoted process is throttled hard, so count what it was using when we demoted it (valid 120 s). Without this the monitor flaps.
        J0 = journal(); dem = {int(k): e for k, e in J0.items() if e["act"] == "demote" and int(k) in procs and t - e["t"] < 120}
        cpu_frac = (sum(p["cpu"] for p in procs.values()) - sum(procs[k]["cpu"] for k in dem) + sum(e.get("cpu", 0.0) for e in dem.values())) / (100.0 * NCPU)
        cpu_hot, mem_hot = cpu_frac >= CFG["cpu_busy"], (lvl >= 4 or (lvl >= 2 and po >= CFG["pageout_hot"]))
        protected, priority = classify(procs, fg_pid, self.ai_protect)
        for k in list(journal()):  # a slowed program that is now protected (you switched to it) is restored at once
            if int(k) in protected: undo(int(k), "protected")
        for x in [x for x in self.streak if x not in procs]: del self.streak[x]
        cands = []
        for p in procs.values():
            if p["pid"] in protected or not eligible(p): self.streak.pop(p["pid"], None); continue
            self.streak[p["pid"]] = self.streak[p["pid"]] + 1 if p["cpu"] >= CFG["hog_cpu"] else 0
            if p["cpu"] >= 5: cands.append(p)
        cands.sort(key=lambda p: -p["cpu"])
        hogs = [p for p in cands if self.streak[p["pid"]] >= CFG["hog_ticks"]]
        J = journal()
        for pid, e in list(J.items()):  # housekeeping: forget dead pids, end pauses
            if int(pid) not in procs: J.pop(pid); jsave(F_JOURNAL, J)
            elif e["act"] == "pause" and t - e["t"] >= e["ttl"]: undo(int(pid), "pause ttl")
        J = journal()
        if self.res:  # consume a finished AI answer
            rs, plan, rc, adv = self.res; self.res = None; self.advice = adv
            if plan is not None:
                self.cache[rs] = [dict(do=a["do"], name=pname(next(c for c in rc if c["pid"] == a["pid"])["cmd"]), why=a["why"]) for a in plan]
                jsave(F_CACHE, self.cache)
                for a in self.cache[rs]: self.byname[a["name"]] = a["do"]
        names_demote, names_pause = set(), set()
        if cpu_hot:
            self.calm = 0
            if self.mode != "off" and hogs:
                s = sig(fg, hogs, cpu_hot, mem_hot)
                names = {pname(h["cmd"]) for h in hogs[:3]}
                known = {pname(h["cmd"]) for h in hogs if pname(h["cmd"]) in self.byname}  # replay by process name: every culprit with a stored decision costs no model call
                if known: self.hits += 1
                for n in known:
                    do = self.byname[n]
                    {"demote": names_demote, "pause": names_pause}.get(do, set()).add(n)
                    if do == "protect": self.ai_protect.add(n)
                if names - known and (self.th is None or not self.th.is_alive()) and t - self.last_ai >= CFG["ai_every"] and not (CFG["saver"] and self.on_battery()):
                    ctx = dict(foreground=fg, priority_running=sorted({pname(procs[x]["cmd"]) for x in priority if x in procs})[:8], cpu_busy_pct=round(cpu_frac * 100), mem_pressure_level=lvl, cores=NCPU)
                    self.last_ai, self.ai_calls = t, self.ai_calls + 1
                    self.th = threading.Thread(target=self.ai_worker, args=(ctx, cands[:12], s), daemon=True); self.th.start()
            todo = [(p, "demote") for p in hogs if pname(p["cmd"]) not in self.ai_protect] + [(p, "demote") for p in cands if pname(p["cmd"]) in names_demote and p["cpu"] >= CFG["hog_cpu"]]
            npaused = sum(1 for e in J.values() if e["act"] == "pause")
            for p in cands:
                if pname(p["cmd"]) in names_pause and p["cpu"] >= 80 and npaused < CFG["max_pause"] and t - self.cool.get(p["pid"], 0) > CFG["pause_cooldown"]:
                    if apply("pause", p, CFG["pause_ttl"]): self.cool[p["pid"]] = t; npaused += 1
            seen, nd = set(), sum(1 for e in J.values() if e["act"] == "demote")
            for p, act in todo:
                if p["pid"] in seen or p["pid"] in protected: continue
                seen.add(p["pid"]); e = J.get(str(p["pid"]))
                if e and e["act"] == "pause": continue
                if e and t - e["t"] < 60: continue
                if not e and nd >= CFG["max_demote"]: continue
                if apply("demote", p, CFG["demote_ttl"]) and not e: nd += 1
            for k, e in list(J.items()):  # keep demotions alive while demand stays high (the OS-level timer would otherwise end them at 90 s)
                if e["act"] == "demote" and int(k) in procs and t - e["t"] >= 60: apply("demote", procs[int(k)], CFG["demote_ttl"])
        else:
            self.calm += 1
            if self.calm >= CFG["calm_ticks"] and J:
                for pid, e in list(J.items()):
                    if t - e["t"] >= 30: undo(int(pid), "calm")  # minimum hold 30 s
        J = journal()
        calm = cpu_frac < CFG["scan_at"] and not mem_hot and not J  # calm: check less often (6 s on power, 10 s on battery); busy: every 3 s
        self.sleep = ((10.0 if self.on_battery() else 6.0) if calm else (5.0 if CFG["saver"] and self.on_battery() else CFG["tick"])) if CFG["adaptive"] else CFG["tick"]
        ct = sum(os.times()[:4]); ovh = 100.0 * (ct - self.c_prev) / max(1e-6, t - self.t_prev); self.c_prev, self.t_prev = ct, t
        st = dict(ts=t, level=lvl, swap=round(swap, 3), pageout_rate=round(po, 1), cpu_frac=round(cpu_frac, 3), cpu_hot=cpu_hot, mem_hot=mem_hot, hold=mem_hot, dry=DRY,
                  fg=fg, demoted=sorted({e["name"] for e in J.values() if e["act"] == "demote"}), paused=sorted({e["name"] for e in J.values() if e["act"] == "pause"}),
                  ai_calls=self.ai_calls, cache_hits=self.hits, advice=self.advice, own_cpu_pct=round(ovh, 2))
        jsave(F_STATUS, st)
        if cpu_hot or mem_hot or J or self.n % 5 == 0:
            jl(F_LOG, dict(t=int(t), lv=lvl, sw=round(swap, 2), po=round(po), cpu=round(cpu_frac, 2), hold=mem_hot, nd=len(st["demoted"]), np=len(st["paused"]), own=round(ovh, 1)))
        return st

def cmd_run(args):
    global DRY
    mode, ticks = "auto", None
    DRY = "--dry" in args
    if "--saver" in args: CFG["saver"] = True
    if "--ai" in args: mode = args[args.index("--ai") + 1]
    if "--ticks" in args: ticks = int(args[args.index("--ticks") + 1])
    if "--tick" in args: CFG["tick"] = float(args[args.index("--tick") + 1]); CFG["adaptive"] = False
    for k in ("hog_ticks", "ai_every", "hog_cpu"):  # hog_cpu: tests lower it, because macOS ps shows a new process's CPU use only after about 10 s
        if "--" + k in args: CFG[k] = float(args[args.index("--" + k) + 1])
    import fcntl
    lk = open(DIR + "/lock", "w")
    try: fcntl.flock(lk, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: return print("monitored is already running for", DIR)  # one monitor per state folder
    revert_all("startup")
    def bye(*a): revert_all("exit"); sys.exit(0)
    signal.signal(signal.SIGINT, bye); signal.signal(signal.SIGTERM, bye)
    m, n = Mon(mode), 0
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
    if "--max-wait" in args: mw = float(args[args.index("--max-wait") + 1])
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
    os.execvp(cmd[0], cmd)

def cmd_report(args):
    rows = [json.loads(x) for x in open(F_LOG)] if os.path.exists(F_LOG) else []
    ev = [json.loads(x) for x in open(F_EVENTS)] if os.path.exists(F_EVENTS) else []
    if not rows: return print("no log yet")
    span = (rows[-1]["t"] - rows[0]["t"]) / 60.0
    hot = sum(1 for r in rows if r["cpu"] >= CFG["cpu_busy"]); hold = sum(1 for r in rows if r["hold"])
    print("log lines %d over %.1f min (lines are written every tick while busy, every 5th tick otherwise)" % (len(rows), span))
    print("lines with CPU busy: %d, with memory thrash hold: %d" % (hot, hold))
    c = defaultdict(int)
    for e in ev: c[e["ev"]] += 1
    print("events:", dict(c))
    top = defaultdict(int)
    for e in ev:
        if e["ev"] in ("demote", "pause"): top[e["name"]] += 1
    print("most acted on:", sorted(top.items(), key=lambda x: -x[1])[:8])
    print("own CPU use, average %% of one core while logging: %.2f" % (sum(r["own"] for r in rows) / len(rows)))

# ---------- install, settings, friendly commands ----------
__version__ = "0.1.0"
LABEL = os.environ.get("MONITORED_LABEL", "com.activitymonitored.agent")
PLIST = os.path.join(HOME, "Library", "LaunchAgents", LABEL + ".plist")
F_SET = DIR + "/settings.json"
SELF = os.path.abspath(__file__)
DEFAULTS = {"mode": "on", "ai": False, "battery_saver": True}

def settings():
    s = dict(DEFAULTS); s.update(jload(F_SET, {})); return s

def lc(*a): return subprocess.run(["launchctl"] + list(a), capture_output=True, text=True)
def running(): return lc("list", LABEL).returncode == 0

def stable_python():
    for p in ("/usr/bin/python3", "/opt/homebrew/bin/python3", "/usr/local/bin/python3"):
        if os.path.exists(p) and subprocess.run([p, "-c", "import sys; sys.exit(sys.version_info < (3, 8))"], capture_output=True).returncode == 0: return p
    return sys.executable

def stop_agent():
    if running(): lc("bootout", "gui/%d/%s" % (ME, LABEL)); time.sleep(1)
    revert_all("stop")  # undo every slowdown and pause

def write_plist(s):
    import plistlib
    args = [stable_python(), DIR + "/monitored.py", "run", "--ai", "auto" if s["ai"] else "off"]
    if s["mode"] == "watch": args.append("--dry")
    if s["battery_saver"]: args.append("--saver")
    d = {"Label": LABEL, "ProgramArguments": args, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30, "ProcessType": "Adaptive",
         "LowPriorityIO": True, "StandardOutPath": DIR + "/launchd.log", "StandardErrorPath": DIR + "/launchd.log",
         "EnvironmentVariables": {"PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin:" + os.path.join(HOME, ".local", "bin")}}
    os.makedirs(os.path.dirname(PLIST), exist_ok=True)
    with open(PLIST, "wb") as f: plistlib.dump(d, f)

def apply_settings():
    s = settings(); stop_agent()
    if s["mode"] == "off": print("Activity Monitored is off. Nothing is running and nothing is slowed."); return 0
    if not os.path.exists(DIR + "/monitored.py"): print("Activity Monitored is not installed. Run: python3 monitored.py install"); return 1
    write_plist(s); r = lc("bootstrap", "gui/%d" % ME, PLIST)
    if r.returncode != 0: print("macOS refused to start Activity Monitored:", r.stderr.strip()); return 1
    time.sleep(3)
    if s["mode"] == "watch": print("Activity Monitored is watching only. It logs what it would do and changes nothing.")
    else: print("Activity Monitored is on. It slows busy background programs only while your Mac is overloaded, then restores them.")
    print("AI judgment: %s. Battery saver: %s. Running: %s." % ("on" if s["ai"] else "off", "on" if s["battery_saver"] else "off", "yes" if running() else "no"))
    return 0

def cmd_install(args):
    s = settings()
    if "--mode" in args: s["mode"] = args[args.index("--mode") + 1]
    if "--ai" in args: s["ai"] = args[args.index("--ai") + 1] == "on"
    if s["mode"] not in ("on", "watch", "off"): print("mode must be on, watch or off"); return 1
    os.makedirs(DIR, exist_ok=True); os.chmod(DIR, 0o700)
    if os.path.abspath(DIR + "/monitored.py") != SELF: shutil.copy(SELF, DIR + "/monitored.py")
    shim = DIR + "/monitored"
    with open(shim, "w") as f: f.write('#!/bin/bash\nexec "%s" "%s/monitored.py" "$@"\n' % (stable_python(), DIR))
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
    if args[:1] not in (["on"], ["off"]): print("usage: monitored %s on|off" % ("ai" if key == "ai" else "battery")); return 1
    return set_key(key, args[0] == "on")

def cmd_protect(args):
    if not args: print("usage: monitored protect NAME"); return 1
    with open(DIR + "/protect.txt", "a") as f: f.write(" ".join(args) + "\n")
    print("Activity Monitored will never touch programs whose command contains '%s'." % " ".join(args)); return 0

def cmd_status(args):
    s, st = settings(), jload(F_STATUS, {})
    fresh = bool(st) and now() - st.get("ts", 0) < 30
    word = {"on": "on", "watch": "watching only", "off": "off"}.get(s["mode"], s["mode"])
    print("Activity Monitored is %s (AI %s, battery saver %s). %s" % (word, "on" if s["ai"] else "off", "on" if s["battery_saver"] else "off", "Running." if (fresh or running()) else "Not running."))
    if fresh:
        lvl = {1: "normal", 2: "warning", 4: "critical"}.get(st.get("level"), str(st.get("level")))
        print("CPU busy: %d%%. Memory pressure: %s. Front app: %s." % (round(100 * st.get("cpu_frac", 0)), lvl, st.get("fg") or "unknown"))
        print("Slowed right now: %s." % (", ".join(st.get("demoted", [])) or "nothing"))
    ev = []
    if os.path.exists(F_EVENTS):
        for x in open(F_EVENTS):
            try: e = json.loads(x)
            except ValueError: continue
            if now() - e.get("t", 0) < 86400: ev.append(e)
    names = defaultdict(int)
    for e in ev:
        if e.get("ev") in ("demote", "would_demote"): names[e.get("name", "?")] += 1
    print("Last 24 hours: %d slowdowns%s." % (sum(names.values()), (", most often " + ", ".join("%s (%d)" % kv for kv in sorted(names.items(), key=lambda kv: -kv[1])[:3])) if names else ""))
    for a in st.get("advice", [])[:3]: print("Suggestion (never done automatically): " + a)

def cmd_privacy(args):
    ai = settings()["ai"]
    print("AI judgment is %s." % ("ON: for an unfamiliar situation, Activity Monitored sends names, CPU and memory of the busiest programs you own, the front app's name, and (if asked) a program's command line and folder, with secrets and your home path removed, to the `claude` program on this Mac, which uses your own Claude login" if ai else "OFF (default): nothing leaves your Mac and no account is used"))
    p = os.path.join(DIR, "last_prompt.txt")
    print("Exactly what the last model call received:\n" + (open(p).read() if os.path.exists(p) else "(no model call yet)"))

if __name__ == "__main__":
    a = sys.argv[1:]; c = a[0] if a else ""
    table = {"run": cmd_run, "gate": cmd_gate, "report": cmd_report, "revert-all": lambda x: revert_all(), "status": cmd_status, "install": cmd_install,
             "uninstall": cmd_uninstall, "privacy": cmd_privacy, "protect": cmd_protect, "on": lambda x: set_key("mode", "on"), "watch": lambda x: set_key("mode", "watch"),
             "off": lambda x: set_key("mode", "off"), "ai": lambda x: cmd_toggle("ai", x), "battery": lambda x: cmd_toggle("battery_saver", x),
             "version": lambda x: print(__version__), "status-json": status_json}
    sys.exit(table.get(c, lambda x: print(__doc__))(a[1:]) or 0)
