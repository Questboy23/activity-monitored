<picture>
  <source media="(prefers-color-scheme: dark)" srcset="art/banner-dark.svg">
  <img alt="Activity Monitored" src="art/banner-light.svg" width="100%">
</picture>

# Activity Monitored

**Activity Monitored stops the macOS "Your system has run out of application memory" window**, the one that pauses your apps and lists them in red.

That window appears when the disk is nearly full. macOS moves memory it can't fit in RAM into swap files on the disk. With no free disk space it can't add swap, so it pauses apps. Activity Monitored does 3 things about it:

| What | When | Exactly what it does | Undo |
|---|---|---|---|
| Keeps disk room for swap | Free disk below the target (30 GB by default), at most once every 6 hours | Runs `uv cache prune`, `pip3 cache purge` and `npm cache clean --force`. Each deletes only that tool's download cache, which the tool downloads again when needed. Nothing else is ever deleted. | The caches refill on the next install. Turn it off: `monitored clean off` |
| Warns you early | Free disk under 15 GB, or memory pressure critical for 30 seconds | Shows a macOS notification banner, at most once per 30 minutes per kind. The memory banner names the 3 biggest programs so you can quit one. Under 10 GB free, or with memory exhausted, it also shows an alert that closes itself after 60 seconds, because Focus mode hides banners. | Nothing to undo |
| Keeps the front app smooth | CPU over 75% busy | Lowers the priority of busy background command-line programs with `taskpolicy -b`. Apps with windows, Logic, music, calls and Claude are never touched. | Restored when the CPU drops under 50%, after 10 minutes at most, and by an OS timer even if Activity Monitored crashes |

It never pauses, quits, kills or rewrites any program or file.

**What it can't do:** it can't free space that your own files take. If your files fill the disk, the banner tells you, and you choose what to move. It can't add RAM: with many big programs open, the Mac still slows down, and the memory banner tells you which one to quit.

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
monitored off | on          off stops it and restores every lowered priority
monitored clean off | on    automatic cache cleanup
monitored disk-target 50    keep 50 GB free
monitored protect NAME      never touch programs whose command contains NAME
monitored uncompress        undo the file compression that versions 0.x did
monitored uninstall
```

## Tests

`python3 test_monitored.py` runs 13 checks in a temp folder. It touches no real program.

## Version 1.0

Versions 0.x also paused and ended memory-hungry programs and compressed big idle files. Pausing kept the memory in use, ending lost work, and file rewriting was slow and risky, so 1.0 removed all three. The old version is at git tag `v2-before-simplify`.

## AI disclosure

Reed Ostrov supplied the ideas and decisions. Claude Code (Anthropic) wrote code and tests at his direction.

## License

MIT. See `LICENSE`.
