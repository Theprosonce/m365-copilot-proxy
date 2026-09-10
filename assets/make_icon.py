from PIL import Image, ImageDraw

S = 1024  # supersample, downscale later

# --- diagonal gradient (blue -> violet), Copilot-ish ---
c1 = (18, 120, 222)   # blue
c2 = (138, 92, 246)   # violet
g = Image.new("RGB", (256, 256))
gp = g.load()
for y in range(256):
    for x in range(256):
        t = (x + y) / 510.0
        gp[x, y] = (
            int(c1[0] + (c2[0] - c1[0]) * t),
            int(c1[1] + (c2[1] - c1[1]) * t),
            int(c1[2] + (c2[2] - c1[2]) * t),
        )
grad = g.resize((S, S), Image.BILINEAR)

# --- rounded-square mask ---
m = int(S * 0.06)
r = int(S * 0.24)
mask = Image.new("L", (S, S), 0)
ImageDraw.Draw(mask).rounded_rectangle([m, m, S - m, S - m], radius=r, fill=255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
img.paste(grad, (0, 0), mask)

# subtle top highlight
hl = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(hl).rounded_rectangle([m, m, S - m, int(S * 0.5)], radius=r, fill=(255, 255, 255, 26))
img = Image.alpha_composite(img, Image.composite(hl, Image.new("RGBA",(S,S),(0,0,0,0)), mask))

d = ImageDraw.Draw(img)
W = (255, 255, 255, 240)
t = int(S * 0.058)            # bar thickness
hh = int(S * 0.085)           # arrowhead half-height
hl_ = int(S * 0.10)           # arrowhead length

def arrow(y, x_start, x_tip, direction):
    # rounded bar from x_start to (x_tip - direction*hl_), arrowhead at x_tip
    base = x_tip - direction * hl_
    x0, x1 = sorted([x_start, base])
    d.rounded_rectangle([x0, y - t // 2, x1, y + t // 2], radius=t // 2, fill=W)
    d.polygon([(x_tip, y), (base, y - hh), (base, y + hh)], fill=W)

cy = S // 2
arrow(int(cy - S * 0.10), int(S * 0.30), int(S * 0.72), +1)   # top -> right
arrow(int(cy + S * 0.10), int(S * 0.70), int(S * 0.28), -1)   # bottom -> left

base = img.resize((256, 256), Image.LANCZOS)
base.save("assets/icon.png")
print("icon written:", "assets/icon.png", base.size)
