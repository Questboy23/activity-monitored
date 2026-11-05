# How Activity Monitored works

## The problem, in plain words

A Mac has a fixed amount of processor time. When more programs ask for it than exists, every program gets less than it wants, including the one you are using, and your Mac feels slow. macOS shares the time by its own rules, but it cannot know which busy programs you do not care about. Activity Monitor (Apple's built-in app) shows the busy programs and leaves the decision to you.

## What Activity Monitored does, step by step

1. **Watch.** Every 6 seconds (10 on battery) it asks macOS how busy the processor is and how strained memory is. This costs about 0.2% of one processor core. When the Mac is busy it checks every 3 seconds.
2. **Notice a problem.** If processor use reaches about 72%, or memory is under strain, it looks at the list of your running programs. Otherwise it does nothing.
3. **Pick culprits.** A program becomes a culprit when all of these hold: it is yours, it is not protected, it used more than 20% of one core for three checks in a row (about 9 seconds), and the whole Mac is at least 75% busy.
4. **Slow it down.** It marks the culprit as a background program. macOS then gives it lower priority, runs it on the low-power cores of an Apple silicon Mac, and slows its disk and network use. Nothing is killed, paused, or changed in the program.
5. **Undo it.** After the Mac has been calm for three checks (and the slowdown is at least 30 seconds old), the program is restored. A second timer, set at the moment of the slowdown, restores it after 90 seconds no matter what, even if Activity Monitored crashes or is removed. If you switch to a slowed program, it is restored within one check. While the overload continues, the slowdown is renewed each minute.
6. **Optional AI judgment (off by default).** For a situation it has not seen, it can send the names and usage of the busiest programs to a Claude model through your own `claude` login. The model answers slow, leave alone, or a suggestion for you. Answers are stored by program name and reused with no further calls. The code accepts only program numbers from the list and only the reversible actions.
7. **Hold new work.** `monitored gate -- COMMAND` waits (at most 30 seconds) until memory is not thrashing, then starts the command.

## Protected by default

- The app you are using and everything it started, plus system programs (WindowServer, Finder, Dock), Terminal, and Claude.
- Media and calls: Spotify, Zoom, FaceTime, Discord, OBS, Music, Teams, Logic Pro, GarageBand, Ableton.
- Work you started on purpose: builds and tests (xcodebuild, clang, make, cmake, ninja, gradle, cargo, npm run, vite, webpack, esbuild, tsc, pytest), renders (ffmpeg, HandBrake, Final Cut, Premiere, DaVinci, Blender, Compressor), virtual machines (Docker, Parallels, VMware, UTM, qemu), local servers (postgres, mysqld, mongod, redis-server, nginx), and ollama.
- Anything you add with `monitored protect NAME`.

What remains are background programs you did not ask for: helper processes stuck at high CPU, indexers, sync tools, runaway scripts.

## What it never does

It never kills a program, edits a file, sends anything over the network (with AI off), asks for an admin password, or touches another user's programs.

## Why it is different from current options

- **macOS alone** shares time by internal rules and has no setting for "slow the programs I do not care about while I work".
- **App Tamer** (paid) gives a program background priority whenever it is not the front app, according to its developer. The trigger is where you are looking. Activity Monitored's trigger is real overload, so an idle or lightly used Mac is never touched.
- **Process Lasso** (Windows only) lowers the priority of processes that hog the processor. Activity Monitored applies the same idea on macOS.
- **Activity Monitored adds:** protection for work you started on purpose, an automatic undo that survives a crash, optional AI judgment with stored answers, and a free open script (once public).
- **Not measured:** a head-to-head comparison with App Tamer. The only benchmark so far is synthetic: a job finished in 24.2 s instead of 39.3 s while 24 background processes competed (median of 3 runs each).

## Definitions

- **Processor (CPU) and core:** the part of the computer that runs programs. One core runs one thing at a time. A 10-core Mac can run 10 things at once, and 100% use of one core is 100%.
- **Efficiency cores:** the low-power cores in Apple silicon. Background programs run there.
- **Priority:** a number macOS uses to decide whose turn it is. Lower priority waits longer when cores are busy.
- **Background priority (`taskpolicy -b`):** a macOS setting that lowers CPU priority, moves the program to efficiency cores, and throttles its disk and network use. `taskpolicy -B` removes it.
- **Process and PID:** a running program, and the number macOS uses to identify it.
- **Memory pressure:** macOS's own gauge of memory strain: 1 normal, 2 warning, 4 critical. Page-outs (memory written to disk) per second show how hard it is working.
- **Kernel counters:** the operating system's own tallies of processor and memory use. Reading them takes microseconds, so calm checks are nearly free.
- **LaunchAgent (login item):** a macOS mechanism that starts a program at login and restarts it after a crash. Activity Monitored installs one without admin rights.
- **Dead-man timer:** a separate short script started with every slowdown that undoes it after 90 seconds. It runs even if the monitor is gone.
- **Foreground app:** the app in front, found with macOS's `lsappinfo`.
- **Decision cache:** stored answers keyed by program name, so a program the AI already judged costs no further model calls.
