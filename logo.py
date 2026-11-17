from PIL import Image, ImageDraw

BLUE = (29, 78, 216, 255)
WHITE = (255, 255, 255, 255)
GHOST = (255, 255, 255, 110)
SS = 4

BACK = (39, 49, 159, 133)
FRONT = (71, 97, 207, 197)
K_STEM = (58, 64, 94, 192)
K_CHEVRON = [(110, 128), (162, 64), (198, 64), (146, 128), (198, 192), (162, 192)]
K_BOX = (58, 64, 198, 192)


def _k(d, k, cx, cy, height):
    f = height / (K_BOX[3] - K_BOX[1])
    ox = cx - (K_BOX[0] + K_BOX[2]) / 2 * f
    oy = cy - (K_BOX[1] + K_BOX[3]) / 2 * f
    x0, y0, x1, y1 = K_STEM
    d.rectangle(((ox + x0 * f) * k, (oy + y0 * f) * k, (ox + x1 * f) * k, (oy + y1 * f) * k), fill=BLUE)
    d.polygon([((ox + x * f) * k, (oy + y * f) * k) for x, y in K_CHEVRON], fill=BLUE)


def image(size=256):
    s = size * SS
    k = s / 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 8 * k
    d.rounded_rectangle((pad, pad, s - pad, s - pad), radius=52 * k, fill=BLUE)
    back = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(back).rounded_rectangle(tuple(v * k for v in BACK), radius=14 * k, fill=GHOST)
    img.alpha_composite(back)
    gap = 10 * k
    x0, y0, x1, y1 = FRONT
    d.rounded_rectangle((x0 * k - gap, y0 * k - gap, x1 * k + gap, y1 * k + gap), radius=24 * k, fill=BLUE)
    d.rounded_rectangle((x0 * k, y0 * k, x1 * k, y1 * k), radius=14 * k, fill=WHITE)
    _k(d, k, (x0 + x1) / 2, (y0 + y1) / 2, (y1 - y0) * 0.52)
    return img.resize((size, size), Image.LANCZOS)


SERVER_NAVY = (23, 37, 125, 255)


def server_image(size=256):
    s = size * SS
    k = s / 256
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = 8 * k
    d.rounded_rectangle((pad, pad, s - pad, s - pad), radius=52 * k, fill=SERVER_NAVY)
    bars = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    g = ImageDraw.Draw(bars)
    x0, y0, x1, y1 = BACK
    h = (y1 - y0) / 3.6
    for i in range(3):
        top = y0 + i * h * 1.3
        g.rounded_rectangle((x0 * k, top * k, x1 * k, (top + h) * k), radius=12 * k, fill=GHOST)
    img.alpha_composite(bars)
    gap = 10 * k
    x0, y0, x1, y1 = FRONT
    d.rounded_rectangle((x0 * k - gap, y0 * k - gap, x1 * k + gap, y1 * k + gap), radius=24 * k, fill=SERVER_NAVY)
    d.rounded_rectangle((x0 * k, y0 * k, x1 * k, y1 * k), radius=14 * k, fill=WHITE)
    _k(d, k, (x0 + x1) / 2, (y0 + y1) / 2, (y1 - y0) * 0.52)
    return img.resize((size, size), Image.LANCZOS)


def _draw(server):
    return server_image if server else image


def save_ico(path, server=False):
    _draw(server)(256).save(path, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])


def tk_icon(root, server=False):
    from PIL import ImageTk
    photos = [ImageTk.PhotoImage(_draw(server)(n)) for n in (16, 32, 64)]
    root.iconphoto(True, *photos)
    root._anykir_icons = photos
