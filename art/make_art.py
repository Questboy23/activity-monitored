"""Activity Monitored banner. Style: a 1970s strip-chart recorder: green phosphor trace on graph paper."""
import math, random, sys, os
out = sys.argv[1]; os.makedirs(out, exist_ok=True)
MONO = "'SF Mono',Menlo,'IBM Plex Mono',Consolas,monospace"
T = {"light": ("#f3f0e6", "#1b2a22", "#b9c9b0", "#d6453b"), "dark": ("#0b120e", "#7cf29a", "#1d3326", "#ff6b5e")}
for th, (bg, ink, grid, red) in T.items():
    W, H = 1280, 420; random.seed(3)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="Activity Monitored">',
         f'<rect width="{W}" height="{H}" fill="{bg}"/>']
    for x in range(40, W - 39, 20):
        o.append(f'<line x1="{x}" y1="150" x2="{x}" y2="380" stroke="{grid}" stroke-width="{1.2 if (x-40)%100==0 else 0.5}"/>')
    for y in range(150, 381, 23):
        o.append(f'<line x1="40" y1="{y}" x2="{W-40}" y2="{y}" stroke="{grid}" stroke-width="{1.2 if (y-150)%115==0 else 0.5}"/>')
    # load trace: rises into overload, then gets pulled back under the red ceiling
    ceil = 205
    o.append(f'<line x1="40" y1="{ceil}" x2="{W-40}" y2="{ceil}" stroke="{red}" stroke-width="1.4" stroke-dasharray="6 5"/>')
    o.append(f'<text x="{W-44}" y="{ceil-8}" text-anchor="end" font-family="{MONO}" font-size="13" fill="{red}">OVERLOAD</text>')
    pts = []
    for x in range(40, W - 39, 4):
        t = (x - 40) / (W - 80)
        base = 330 - 170 * math.exp(-((t - 0.42) / 0.12) ** 2)
        if t > 0.42:
            base = 330 - 170 * math.exp(-((t - 0.42) / 0.05) ** 2) - 40 * math.exp(-((t - 0.5) / 0.2) ** 2)
        y = max(ceil + 6 if t > 0.43 else 150, base + random.gauss(0, 5))
        pts.append(f"{x},{y:.1f}")
    o.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{ink}" stroke-width="2.2" stroke-linejoin="round"/>')
    o.append(f'<text x="40" y="78" font-family="{MONO}" font-size="56" font-weight="600" fill="{ink}">activity monitored</text>')
    o.append(f'<text x="42" y="116" font-family="{MONO}" font-size="19" fill="{ink}">keeps a Mac responsive when it is overloaded_</text>')
    o.append(f'<text x="40" y="404" font-family="{MONO}" font-size="12" fill="{ink}">CPU LOAD  ·  1 DIV = 10 s</text>')
    o.append('</svg>')
    open(f"{out}/banner-{th}.svg", "w").write("\n".join(o))
print("ok")
