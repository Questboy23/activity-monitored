<picture>
  <source media="(prefers-color-scheme: dark)" srcset="art/banner-dark.svg">
  <img alt="Activity Monitored" src="art/banner-light.svg" width="100%">
</picture>

# Activity Monitored

**Activity Monitored stops the macOS "Your system has run out of application memory" window**, the one that pauses your apps and lists them in red.

That window appears when memory runs out. macOS moves memory it can't fit in RAM into swap files on the disk, and when the disk is nearly full it can't add swap, so it pauses apps. Activity Monitored does 4 things about it, and every one of them is temporary:

| What | When | Exactly what it does | Undo |
|---|---|---|---|
| Keeps disk room for swap | Free disk below the target (30 GB by default), at most once every 6 hours | Runs `uv cache prune`, `pip3 cache purge` and `npm cache clean --force`. Each deletes only that tool's download cache, which the tool downloads again when needed. Nothing else is ever deleted. | The caches refill on the next install. Turn it off: `monitored clean off` |
| Stays quiet | Almost never | It shows nothing in normal use. Pauses, priority changes and cache cleanups go to `monitored status` only. It speaks only when it cannot fix the problem itself: under 10 GB of free disk, or memory still critical for 30 seconds after pausing. Then it shows one notification banner, never a window with an OK button, at most once every 6 hours per kind. |
| Pauses before macOS does | Memory pressure critical, or warning with under 10 GB of disk, for 10 seconds | Pauses (SIGSTOP) the biggest program you are not using, 500 MB or more counting its swapped memory, scripts before apps, at most 3 at once. A paused program keeps all its work and stops taking memory, and macOS compresses or swaps its memory to make room. Never paused: the app in front and everything it started, Claude and its tools, Logic, music, calls, Terminal, system programs, and anything you listed with `monitored protect`. | Resumes one program every 30 seconds once memory is normal, after 30 minutes at most, and by an OS timer even if Activity Monitored crashes. `monitored resume` resumes everything now and holds off new pauses for 10 minutes. |
| Unloads idle AI models first | Same trigger, when the biggest program is a local ollama model | Runs `ollama stop`, which frees the model's RAM at once. The model stays on disk and loads again on the next request. |
| Keeps the front app smooth | CPU over 75% busy | Lowers the priority of busy background command-line programs with `taskpolicy -b`. Apps with windows, Logic, music, calls and Claude are never touched. | Restored when the CPU drops under 50%, after 10 minutes at most, and by an OS timer even if Activity Monitored crashes |

It never quits, kills, deletes or rewrites any program or file. The only deletion is the download-cache cleanup above.

**What it can't do:** it can't free space that your own files take. If your files fill the disk, a banner tells you, and you choose what to move. It can't add RAM: with many big programs open, the Mac still slows down, and the memory banner tells you which one to quit.

## Install

```
git clone https://github.com/Questboy23/activity-monitored
cd activity-monitored
bash install.sh
```

Or download [Activity-Monitored.zip](https://github.com/Questboy23/activity-monitored/releases/latest/download/Activity-Monitored.zip), unzip it, and double-click `Install Activity Monitored.command`. It needs Python 3, which comes with Apple's Command Line Tools.

## Use

```
monitored status            what it is doing now and in the last 24 hours
monitored off | on          off stops it, resumes every paused program, restores every priority
monitored resume            resume every paused program now
monitored clean off | on    automatic cache cleanup
monitored disk-target 50    keep 50 GB free
monitored protect NAME      never touch programs whose command contains NAME
monitored uncompress        undo the file compression that versions 0.x did
monitored uninstall
```

## Tests

`python3 test_monitored.py` runs 25 checks in a temp folder, including a real pause and resume of a 600 MB test program under fake critical pressure, and the OS timer resuming it with no monitor running. It never pauses a real program.

## Versions

1.2 is silent in normal use and measures memory the way Activity Monitor does (physical footprint), so swapped-out programs still count. 1.1 brought back pausing, with an OS resume timer on every pause. 1.0 removed ending programs and compressing files: ending lost work, and file rewriting was slow and risky. The old version is at git tag `v2-before-simplify`.

## AI disclosure

Reed Ostrov supplied the ideas and decisions. Claude Code (Anthropic) wrote code and tests at his direction.

## License

MIT. See `LICENSE`.
