"""Pip, the dashboard's pixel-art buddy. Run this to regenerate his sprite and the icons:

    python -m autoapply.web.pip_sprite

Shapes are drawn on a 32x40 grid and outlined, so every pose matches. Poses: standing and its
idle variations (blink, glance, wave, stretch, dance, cheer, talk), worried, asleep, and four
squat frames for runs (knees stay over his feet, the body drops, sweat at the bottom).
Writes templates/_pip.html (every pose in one SVG; CSS picks which shows) and static/favicon.svg."""
import math

W, H = 32, 40
COL = {"K": "#2b2731", "P": "#ff7ab6", "D": "#d9518f", "L": "#ffb3d6", "W": "#ffffff", "E": "#23202a",
       "Y": "#d9f59a", "G": "#a8c96a", "R": "#ff4f9d", "S": "#ff4f9d", "B": "#6ec3ff", "b": "#c7e8ff"}


def px(v):
    return math.floor(v + .5)


def seg(pxx, pyy, ax, ay, bx, by):
    vx, vy, wx, wy = bx - ax, by - ay, pxx - ax, pyy - ay
    t = max(0.0, min(1.0, (wx * vx + wy * vy) / (vx * vx + vy * vy + 1e-9)))
    cx, cy = ax + t * vx, ay + t * vy
    return math.hypot(pxx - cx, pyy - cy), t, cx


def pose(drop=0.0, eyes="open", arms="down", mouth="smile", sweat=(), shift=0, pop=0):
    """drop: how far he's squatted, 0 (standing) to 1 (bottom)."""
    g = [["." for _ in range(W)] for _ in range(H)]

    def put(x, y, c):
        x, y = px(x), px(y)
        if 0 <= x < W and 0 <= y < H:
            g[y][x] = c

    dy = 7.0 * drop               # a deep squat: hips come down to about knee height
    hip_y = 23 + dy
    for side in (-1, 1):
        hx = 16 + shift + side * 3.5
        ax, ay = 16 + side * 4, 35          # ankles stay put
        kx = ax + side * 1.5 * drop          # knees track over his toes: a hint out, no flare
        ky = 30 + dy * 0.35 - (1.2 if pop == side else 0)
        if pop == side:
            kx += side * -0.8
        thick = 1 + 0.45 * drop              # thighs look wider as they foreshorten
        for y in range(H):
            for x in range(W):
                pp = (x + .5, y + .5)
                d, t, cx = seg(*pp, hx, hip_y, kx, ky)
                r = (2.1 + 1.3 * math.sin(math.pi * min(1, t * 1.05))) * thick
                if d <= r:
                    inner = (pp[0] - cx) * side < 0
                    if t < .3:   # the top of the thigh is inside his shorts, so the shorts fit each leg
                        g[y][x] = "Y"
                    else:
                        g[y][x] = "D" if (inner and d > r * .45) else ("L" if (not inner and d > r * .55 and t < .6) else "P")
                    continue
                d, t, cx = seg(*pp, kx, ky, ax, ay)
                r = 1.1 + 1.0 * math.sin(math.pi * min(1, t * 1.4))
                if d <= r and g[y][x] == ".":
                    inner = (pp[0] - cx) * side < 0
                    g[y][x] = "D" if (inner and d > r * .4) else "P"
        fx = int(ax) + side            # feet point a little outward
        for x in range(fx - 2, fx + 2):
            put(x, 36, "P")
        for x in range(fx - 2, fx + 3) if side > 0 else range(fx - 3, fx + 2):
            put(x, 37, "P")
        for tx in ((fx, fx + 2) if side > 0 else (fx - 2, fx)):
            put(tx, 37, "D")           # the gaps between his toes
    # waist of the shorts: fills between the legs up to his head, with a small split at the bottom
    for y in range(px(hip_y) - 4, px(hip_y) + 1):
        for x in range(12 + shift, 20 + shift):
            if g[y][x] in ".PDL":
                put(x, y, "Y")
    # the crotch notch, then the gap between the legs
    for y in range(px(hip_y) + 1, H):
        for x in (15 + shift, 16 + shift):
            if 0 <= x < W and g[y][x] != ".":
                g[y][x] = "."
    # head (he's all head)
    hcx, hcy, rx, ry = 16 + shift, 12.5 + dy, 8.6, 8.0
    for y in range(H):
        for x in range(W):
            if ((x + .5 - hcx) / rx) ** 2 + ((y + .5 - hcy) / ry) ** 2 <= 1:
                g[y][x] = "P"
    for (x, y) in ((12, 7), (11, 8), (12, 8), (11, 9)):
        put(x + shift, y + dy, "L")
    ey = 12 + dy
    look = {"left": -1, "right": 1}.get(eyes, 0)
    for ex in (12, 19):
        x0 = ex + look + shift
        if eyes in ("closed", "squint"):
            put(x0 - (1 if eyes == "squint" else 0), ey + 1, "K"); put(x0 + 1, ey + 1, "K")
            if eyes == "squint":
                put(x0 + 2, ey + 1, "K")
        else:
            for x, y in ((0, 0), (1, 0), (0, 1), (1, 1)):
                put(x0 + x, ey + y, "E")
            put(x0 + (1 if look > 0 else 0), ey, "W")
    put(10 + shift, ey + 3, "R"); put(22 + shift, ey + 3, "R")
    my = ey + 3
    sh = shift
    if mouth == "smile":
        for x in (15, 16, 17):
            put(x + sh, my + 1, "K")
        put(14 + sh, my, "K"); put(18 + sh, my, "K")
    elif mouth == "open":
        for x in (15, 16, 17):
            put(x + sh, my, "K"); put(x + sh, my + 1, "K")
        put(16 + sh, my + 1, "R")
    elif mouth == "puff":         # effort face at the bottom of a squat
        for x in (14, 15, 16, 17, 18):
            put(x + sh, my + 1, "K")
    elif mouth == "frown":
        for x in (15, 16, 17):
            put(x + sh, my, "K")
        put(14 + sh, my + 1, "K"); put(18 + sh, my + 1, "K")
    elif mouth == "flat":
        for x in (15, 16, 17):
            put(x + sh, my + 1, "K")
    # stick arms: down by his sides, out front for balance in a squat, or waving
    if arms == "down":
        for i in range(4):
            put(6 + sh - i * .6, 17 + dy + i, "K"); put(25 + sh + i * .6, 17 + dy + i, "K")
    elif arms == "up":            # stretch / cheer
        for i in range(5):
            put(7 + sh - i * .7, 13 + dy - i, "K"); put(24 + sh + i * .7, 13 + dy - i, "K")
    elif arms == "dance":         # one up, one down
        for i in range(5):
            put(7 + sh - i * .7, 13 + dy - i, "K")
        for i in range(4):
            put(25 + sh + i * .6, 17 + dy + i, "K")
    elif arms == "dance2":
        for i in range(4):
            put(6 + sh - i * .6, 17 + dy + i, "K")
        for i in range(5):
            put(24 + sh + i * .7, 13 + dy - i, "K")
    elif arms == "out":
        for i in range(5):
            put(6 - i, 16 + dy - i * .2, "K"); put(25 + i, 16 + dy - i * .2, "K")
    elif arms == "wave":
        for i in range(4):
            put(6 - i * .6, 17 + dy + i, "K")
        for i in range(5):
            put(25 + i * .5, 15 + dy - i, "K")
        put(28, 9 + dy, "K"); put(27, 9 + dy, "K")
    # sweat drops flying off his head
    for (sx, sy) in sweat:
        put(sx, sy, "B"); put(sx, sy + 1, "B"); put(sx - .6, sy + 1, "b")
    out = [row[:] for row in g]
    for y in range(H):
        for x in range(W):
            if g[y][x] != ".":
                continue
            for dx, dyy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nx, ny = x + dx, y + dyy
                if 0 <= nx < W and 0 <= ny < H and g[ny][nx] not in ".KEBb":
                    out[y][x] = "K"
                    break
    return out


def svg(grid, cls):
    rects = []
    for y, row in enumerate(grid):
        x = 0
        while x < W:
            c = row[x]
            if c == ".":
                x += 1
                continue
            run = 1
            while x + run < W and row[x + run] == c:
                run += 1
            rects.append(f'<rect x="{x}" y="{y}" width="{run}" height="1" fill="{COL[c]}"/>')
            x += run
    return f'<g class="{cls}">{"".join(rects)}</g>'


poses = {
    "stand": pose(),
    "blink": pose(eyes="closed"),
    "left": pose(eyes="left"),
    "right": pose(eyes="right"),
    "wave": pose(arms="wave", mouth="open"),
    "talk": pose(mouth="open"),
    "asleep": pose(eyes="closed", mouth="flat"),
    "worried": pose(mouth="frown"),
    "stretch": pose(eyes="closed", arms="up", mouth="open"),
    "cheer": pose(arms="up", mouth="open"),
    "danceL": pose(arms="dance", pop=1, mouth="open"),
    "danceR": pose(arms="dance2", pop=-1, mouth="smile"),
    # one squat: down, bottom (effort face, sweat), up. Drops fly out and fall as he rises.
    "sq0": pose(0.0, arms="out"),
    "sq1": pose(0.55, arms="out", mouth="flat"),
    "sq2": pose(1.0, arms="out", mouth="puff", sweat=((7, 13), (25, 12))),
    "sq3": pose(0.55, arms="out", mouth="flat", sweat=((4, 13), (28, 14))),
}


def favicon(grid) -> str:
    """Just his face, cropped from the standing pose."""
    x0, y0, size = 6, 3, 20
    rects = []
    for y in range(y0, y0 + size):
        for x in range(x0, x0 + size):
            c = grid[y][x]
            if c != ".":
                rects.append(f'<rect x="{x - x0}" y="{y - y0}" width="1" height="1" fill="{COL[c]}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {size} {size}" shape-rendering="crispEdges">'
            + "".join(rects) + "</svg>")


if __name__ == "__main__":
    from pathlib import Path

    here = Path(__file__).parent
    groups = "".join(svg(v, f"p-{k}") for k, v in poses.items())
    (here / "templates" / "_pip.html").write_text(
        "{# Generated by autoapply/web/pip_sprite.py: edit that, then rerun it. #}\n"
        '<svg class="pip" data-pose="stand" viewBox="0 0 32 40" aria-hidden="true">' + groups + "</svg>\n")
    icon = favicon(poses["stand"])
    (here / "static" / "favicon.svg").write_text(icon)
    print("poses:", ", ".join(poses))
    # The Chrome extension needs PNG icons: render his face with the browser the app already uses.
    from playwright.sync_api import sync_playwright

    out = here.parent.parent / "extension" / "icons"
    out.mkdir(exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        for size in (16, 32, 48, 128):
            page.set_viewport_size({"width": size, "height": size})
            page.set_content(f'<body style="margin:0;background:transparent">{icon.replace("<svg ", f"<svg width=\'{size}\' height=\'{size}\' ", 1)}</body>')
            page.screenshot(path=str(out / f"icon{size}.png"), omit_background=True)
        browser.close()
    print("icons:", out)
