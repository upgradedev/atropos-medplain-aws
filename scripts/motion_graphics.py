"""Deterministic procedural motion graphics. Pillow is imported only in CI rendering."""
from __future__ import annotations

from functools import lru_cache
import math
from pathlib import Path

WIDTH, HEIGHT = 1920, 1080
MINT = (130, 225, 197)
WHITE = (239, 248, 249)
MUTED = (143, 169, 180)
AMBER = (255, 194, 108)
KINDS = {"intro", "sources", "identity", "ui", "architecture", "process", "gate", "benchmark", "h12", "closing"}


def ease(value):
    value = min(1.0, max(0.0, value))
    return value * value * (3 - 2 * value)


def project(x, y, z, center=(960, 500), focal=1300):
    scale = focal / (focal + z)
    return center[0] + x * scale, center[1] + y * scale


def card_quad(center, width, height, yaw, tilt, roll, depth=0):
    result = []
    for x, y in ((-width / 2, -height / 2), (width / 2, -height / 2),
                 (width / 2, height / 2), (-width / 2, height / 2)):
        x, z = x * math.cos(yaw), -x * math.sin(yaw)
        y, z = y * math.cos(tilt) - z * math.sin(tilt), y * math.sin(tilt) + z * math.cos(tilt)
        x, y = x * math.cos(roll) - y * math.sin(roll), x * math.sin(roll) + y * math.cos(roll)
        result.append(project(x, y, z + depth, center))
    return result


def motion_state(kind, local_seconds, duration):
    """Object geometry, not caption changes, proves the scene has procedural movement."""
    progress = local_seconds / max(duration, 0.001)
    phase = local_seconds * 0.43
    state = {"kind": kind, "camera": (math.sin(phase) * 12, math.cos(phase * 0.8) * 8),
             "packet_phase": (local_seconds * 0.19) % 1,
             "reveal": ease(local_seconds / 1.2), "progress": progress}
    if kind in {"intro", "sources", "identity"}:
        state["cards"] = [card_quad((1180 + index * 190 + math.sin(phase + index) * 24 + (1 - state["reveal"]) * 150,
                                              485 + math.cos(phase + index) * 46 - (1 - state["reveal"]) * 80),
                                             330, 440, -0.32 + math.sin(phase + index) * 0.14,
                                             0.08 * math.cos(phase + index), -0.12 + index * 0.11,
                                             90 + index * 110)
                          for index in range(3)]
    elif kind == "ui":
        state["capture_scale"] = 1.025 + 0.025 * ease(progress)
        state["capture_pan"] = (math.sin(phase * 0.65) * 12, math.cos(phase * 0.7) * 8)
    elif kind in {"architecture", "process", "gate"}:
        state["active_node"] = int(local_seconds / 1.25)
        state["orbit"] = (math.cos(phase) * 20, math.sin(phase) * 20)
    else:
        state["bar_reveal"] = ease(local_seconds / 1.4)
        state["shield_rotation"] = 0.05 * math.sin(phase)
    return state


def perspective_coefficients(destination, source):
    matrix = []
    for (x, y), (u, v) in zip(destination, source):
        matrix.append([x, y, 1, 0, 0, 0, -u * x, -u * y, u])
        matrix.append([0, 0, 0, x, y, 1, -v * x, -v * y, v])
    for column in range(8):
        pivot = max(range(column, 8), key=lambda row: abs(matrix[row][column]))
        if abs(matrix[pivot][column]) < 1e-10:
            raise ValueError("Degenerate projected card")
        matrix[column], matrix[pivot] = matrix[pivot], matrix[column]
        value = matrix[column][column]
        matrix[column] = [item / value for item in matrix[column]]
        for row in range(8):
            if row != column:
                value = matrix[row][column]
                matrix[row] = [a - value * b for a, b in zip(matrix[row], matrix[column])]
    return tuple(matrix[row][8] for row in range(8))


def graph_labels(kind, identifier):
    if kind == "architecture":
        return (["Anonymous web", "API Lambda", "DynamoDB", "Streams relay", "SQS", "Strands worker", "Human review", "Patient view"],
                ["Synthetic intake", "FastAPI", "Case + outbox", "Relay Lambda", "Job identifiers", "Lambda + Bedrock", "Artifact binding", "Read-only release"])
    if kind == "gate":
        return (["Source facts", "Candidate card", "Safety gate", "Release or refuse"],
                ["Original witnesses", "Agent proposal", "Deterministic checks", "Bounded correction"])
    if identifier == "release":
        return (["Verified artifact", "Human review", "Patient view"],
                ["Source-bound version", "Exact artifact decision", "Read-only after release"])
    return (["Source indexer", "Question planner", "Artifact publisher"],
            ["Cover every statement", "Link unfamiliar context", "Bind the source-cited card"])


def selected_capture(shot, progress):
    return shot["capture_alt"] if shot.get("capture_alt") and progress >= 0.5 else shot.get("capture")


class CinematicCanvas:
    def __init__(self, root):
        import PIL
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
        if PIL.__version__ != "13.0.0":
            raise ValueError("Motion renderer requires reviewed Pillow 13.0.0")
        self.Image, self.Draw, self.Filter, self.Font = Image, ImageDraw, ImageFilter, ImageFont
        self.root = Path(root)
        self.background = self._background()
        self.assets = {}

    @lru_cache(maxsize=40)
    def font(self, size, bold=False):
        suffix = "-Bold" if bold else ""
        return self.Font.truetype(f"/usr/share/fonts/truetype/dejavu/DejaVuSans{suffix}.ttf", size)

    def _background(self):
        image = self.Image.new("RGB", (240, 135))
        pixels = image.load()
        for y in range(135):
            for x in range(240):
                glow = math.exp(-(((x - 162) / 96) ** 2 + ((y - 55) / 66) ** 2))
                vignette = max(0.1, 1 - ((x - 120) / 210) ** 2 - ((y - 67) / 145) ** 2)
                pixels[x, y] = (int(5 + 6 * glow * vignette), int(13 + 20 * glow * vignette),
                                int(20 + 26 * glow * vignette))
        return image.resize((WIDTH, HEIGHT), self.Image.Resampling.BICUBIC).convert("RGBA")

    def text(self, image, value, xy, size=36, color=WHITE, bold=False, width=None, alpha=255):
        draw = self.Draw.Draw(image)
        if width:
            # Pixel-aware wrapping keeps rendered copy within its intended design column.
            lines, line = [], ""
            for word in value.split():
                proposed = (line + " " + word).strip()
                if line and draw.textlength(proposed, font=self.font(size, bold)) > width:
                    lines.append(line)
                    line = word
                else:
                    line = proposed
            value = "\n".join(lines + [line])
        if alpha < 255:
            box = draw.multiline_textbbox((0, 0), value, font=self.font(size, bold), spacing=10)
            layer = self.Image.new("RGBA", (max(1, box[2] + 4), max(1, box[3] + 4)))
            self.Draw.Draw(layer).multiline_text((0, 0), value, font=self.font(size, bold), fill=(*color, alpha), spacing=10)
            image.alpha_composite(layer, (round(xy[0]), round(xy[1])))
        else:
            draw.multiline_text(xy, value, font=self.font(size, bold), fill=(*color, alpha), spacing=10)
        box = draw.multiline_textbbox((0, 0), value, font=self.font(size, bold), spacing=10)
        return box[3]

    def panel(self, image, box, radius=28, accent=MINT, fill=(17, 37, 46, 230)):
        layer = self.Image.new("RGBA", image.size)
        draw = self.Draw.Draw(layer)
        draw.rounded_rectangle(box, radius, fill=fill, outline=(*accent, 90), width=2)
        image.alpha_composite(layer)

    def thread(self, image, points, phase, color=MINT, width=3):
        draw = self.Draw.Draw(image)
        draw.line(points, fill=(*color, 70), width=width, joint="curve")
        lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])]
        travel = phase * sum(lengths)
        for index, length in enumerate(lengths):
            if travel <= length:
                a, b = points[index:index + 2]
                amount = travel / max(length, 0.001)
                x, y = a[0] + (b[0] - a[0]) * amount, a[1] + (b[1] - a[1]) * amount
                draw.ellipse((x - 8, y - 8, x + 8, y + 8), fill=(*color, 255))
                draw.ellipse((x - 14, y - 14, x + 14, y + 14), outline=(*color, 90), width=2)
                break
            travel -= length

    def shield(self, image, center, scale, local_seconds, color=MINT):
        x, y = center
        points = [(x - 110 * scale, y - 115 * scale), (x, y - 155 * scale),
                  (x + 110 * scale, y - 115 * scale), (x + 99 * scale, y + 26 * scale),
                  (x + 54 * scale, y + 104 * scale), (x, y + 145 * scale),
                  (x - 54 * scale, y + 104 * scale), (x - 99 * scale, y + 26 * scale)]
        layer = self.Image.new("RGBA", image.size)
        draw = self.Draw.Draw(layer)
        draw.polygon(points, fill=(*color, 25))
        draw.line(points + [points[0]], fill=(*color, 230), width=max(2, round(5 * scale)), joint="curve")
        draw.line([(x - 48 * scale, y - 5 * scale), (x - 8 * scale, y + 35 * scale),
                   (x + 58 * scale, y - 48 * scale)], fill=(*color, 255), width=max(3, round(9 * scale)), joint="curve")
        image.alpha_composite(layer)
        draw = self.Draw.Draw(image)
        radius = 176 * scale
        for index in range(3):
            angle = local_seconds * 0.4 + index * math.tau / 3
            px, py = x + math.cos(angle) * radius, y + math.sin(angle) * radius * 0.4
            draw.ellipse((px - 5, py - 5, px + 5, py + 5), fill=(*color, 190))

    @lru_cache(maxsize=10)
    def paper(self, name):
        texture = self.Image.new("RGBA", (380, 520))
        draw = self.Draw.Draw(texture)
        draw.rounded_rectangle((2, 2, 378, 518), 24, fill=(21, 44, 55, 250), outline=(*MINT, 170), width=2)
        draw.rounded_rectangle((28, 32, 84, 88), 14, fill=(*MINT, 50))
        draw.line((44, 60, 56, 72, 72, 48), fill=(*MINT, 255), width=4)
        self.text(texture, name, (30, 124), size=30, bold=True, width=320)
        for index, length in enumerate((285, 250, 292, 210, 270, 190)):
            draw.rounded_rectangle((30, 224 + index * 32, 30 + length, 234 + index * 32), 5,
                                   fill=(115, 155, 166, 60 if index % 3 else 110))
        self.text(texture, "ILLUSTRATION", (30, 468), 18, MUTED)
        return texture

    def projected_card(self, image, texture, quad):
        left, top = math.floor(min(x for x, _ in quad)), math.floor(min(y for _, y in quad))
        right, bottom = math.ceil(max(x for x, _ in quad)), math.ceil(max(y for _, y in quad))
        local = [(x - left, y - top) for x, y in quad]
        source = [(0, 0), (texture.width, 0), (texture.width, texture.height), (0, texture.height)]
        coefficients = perspective_coefficients(local, source)
        warped = texture.transform((right - left, bottom - top), self.Image.Transform.PERSPECTIVE,
                                   coefficients, self.Image.Resampling.BICUBIC)
        shadow = self.Image.new("RGBA", warped.size, (0, 0, 0, 0))
        shadow.putalpha(warped.getchannel("A").filter(self.Filter.GaussianBlur(12)))
        image.alpha_composite(shadow, (left + 10, top + 20))
        image.alpha_composite(warped, (left, top))

    def atmosphere(self, image, seconds):
        draw = self.Draw.Draw(image)
        for index in range(32):
            x = (index * 173 + seconds * (5 + index % 4)) % 2020 - 50
            y = (index * 97 + math.sin(seconds * 0.17 + index) * 34) % 1080
            draw.ellipse((x, y, x + 2, y + 2), fill=(*MINT, 30 + index % 30))
        for index in range(6):
            y = 220 + index * 118 + math.sin(seconds * 0.17 + index) * 8
            draw.line([(850, y + 260), (1300, y), (1920, y - 100)], fill=(*MINT, 10), width=1)

    def heading(self, image, shot, size=72):
        size = min(size, 68) if len(shot["title"]) > 35 else size
        height = self.text(image, shot["title"], (90, 225), size, bold=True, width=720)
        self.text(image, shot.get("subtitle", ""), (94, 250 + height), 31, MUTED, width=650)

    def source_cards(self, image, shot, state, local_seconds):
        self.heading(image, shot, 78 if shot["kind"] == "intro" else 65)
        names = ("Home list", "Discharge sheet", "Caregiver concern")
        if shot["kind"] == "identity":
            names = tuple((shot.get("bullets") or ["Original phrase", "Original passage", "A question"])[index % len(shot.get("bullets") or [1, 2, 3])]
                          for index in range(3))
        for index in (2, 1, 0):
            self.projected_card(image, self.paper(names[index]), state["cards"][index])
        for index, points in enumerate(state["cards"]):
            start = points[3]
            path = [start, (start[0] - 90, start[1] + 65), (970, 760), (770, 760)]
            self.thread(image, path, (state["packet_phase"] + index * 0.25) % 1)
        if local_seconds > 3:
            reveal = ease((local_seconds - 3) / 1.5)
            self.shield(image, (772, 760), 0.34 * reveal, local_seconds)
        self.text(image, "ATROPOS", (94, 660), 23, MINT, bold=True)
        self.text(image, "MedPlain", (90, 695), 66, WHITE, bold=True)

    def asset(self, capture):
        name = capture["image"]
        key = (name, tuple(capture.get("crop") or ()))
        if key not in self.assets:
            with self.Image.open(self.root / name) as source:
                image = source.convert("RGB")
            if capture.get("crop"):
                x, y, width, height = capture["crop"]
                if x + width > image.width or y + height > image.height:
                    raise ValueError("Capture crop exceeds original pixels")
                image = image.crop((x, y, x + width, y + height))
            self.assets[key] = image
        return self.assets[key]

    def ui(self, image, shot, state, local_seconds):
        self.text(image, shot["title"], (90, 215), 55, bold=True, width=460)
        self.text(image, shot.get("subtitle", ""), (94, 405), 29, MUTED, width=440)
        for index, value in enumerate(shot.get("bullets", [])[:3]):
            amount = ease((local_seconds - 0.6 - index * 0.4) / 0.8)
            self.Draw.Draw(image).line((95, 595 + index * 72, 126 + amount * 36, 595 + index * 72),
                                       fill=(*MINT, 255), width=3)
            self.text(image, value, (94, 610 + index * 72), 24, WHITE, width=440, alpha=round(255 * amount))
        self.panel(image, (590, 186, 1842, 885), fill=(8, 20, 28, 255))
        self.text(image, "CAPTURED APPLICATION", (622, 205), 16, MINT, bold=True)
        source = self.asset(selected_capture(shot, state["progress"]))
        box = (1212, 620)
        scale = min(box[0] / source.width, box[1] / source.height) * state.get("capture_scale", 1.035)
        rendered = source.resize((round(source.width * scale), round(source.height * scale)), self.Image.Resampling.LANCZOS)
        viewport = self.Image.new("RGB", box, (8, 20, 28))
        pan = state.get("capture_pan", (math.sin(local_seconds * 0.3) * 10, 0))
        viewport.paste(rendered, (round((box[0] - rendered.width) / 2 + pan[0]),
                                  round((box[1] - rendered.height) / 2 + pan[1])))
        image.paste(viewport, (610, 248))
        self.Draw.Draw(image).rounded_rectangle((609, 247, 1823, 870), 8, outline=(*MINT, 80), width=2)

    def graph(self, image, shot, state, local_seconds):
        self.text(image, shot["title"], (90, 190), 62, bold=True, width=1700)
        self.text(image, shot.get("subtitle", ""), (94, 285), 29, MUTED, width=1700)
        labels, sub = graph_labels(shot["kind"], shot["id"])
        if shot["kind"] == "architecture":
            positions = [(275 + index * 445, 445) for index in range(4)] + [(275 + (3 - index) * 445, 710) for index in range(4)]
        elif shot["kind"] == "gate":
            positions = [(280 + index * 455, 545) for index in range(4)]
        else:
            positions = [(375 + index * 585, 530) for index in range(3)]
        for index in range(len(positions) - 1):
            x, y = positions[index]
            nx, ny = positions[index + 1]
            if y == ny:
                offset = 165 if nx > x else -165
                path = [(x + offset, y), (nx - offset, ny)]
            else:
                path = [(x, y + 58), (x + 190, y + 120), (nx + 190, ny - 120), (nx, ny - 58)]
            self.thread(image, path, (state["packet_phase"] + index * 0.13) % 1)
        for index, ((x, y), label, detail) in enumerate(zip(positions, labels, sub)):
            active = state["active_node"] % len(labels) == index
            drift = math.sin(local_seconds * 0.5 + index) * 3
            self.panel(image, (x - 176, y - 74 + drift, x + 176, y + 74 + drift), accent=MINT if active else (83, 120, 137))
            self.text(image, label, (x - 151, y - 35 + drift), 26, MINT if active else WHITE, bold=True)
            self.text(image, detail, (x - 151, y + 13 + drift), 19, MUTED)
        if shot["kind"] == "architecture":
            self.thread(image, [(positions[4][0], 780), (positions[4][0], 835), (1550, 835)], state["packet_phase"], AMBER, 2)
            self.text(image, "SQS dead-letter queue", (1575, 817), 21, AMBER)
            self.text(image, "Explicit services, scoped roles, conditional publication", (92, 860), 24, MUTED)
        elif shot["id"] == "release":
            self.text(image, "Public view follows review of that exact artifact.", (500, 790), 29, MINT)
        else:
            left, right = positions[1], positions[-1]
            path = [(right[0], right[1] + 80), (right[0], 790), (left[0], 790), (left[0], left[1] + 80)]
            self.thread(image, path, (1 - state["packet_phase"]) % 1, AMBER)
            self.text(image, "On discrepancy: request correction or withhold", (570, 810), 26, AMBER)

    def benchmark(self, image, shot, state, local_seconds):
        self.text(image, shot["title"], (90, 190), 64, bold=True, width=1700)
        self.text(image, "HISTORICAL 43d | 30 engineering-annotated synthetic dossiers", (94, 292), 26, MINT)
        if shot["id"] == "availability":
            for column, (name, accepted) in enumerate((("Strands", 25), ("Full deterministic baseline", 30))):
                x = 95 + column * 920
                self.text(image, name, (x, 368), 31, MINT if column == 0 else WHITE, bold=True)
                self.text(image, f"{accepted} / 30 verified cards", (x, 420), 29, WHITE)
                draw = self.Draw.Draw(image)
                for index in range(30):
                    px, py = x + (index % 6) * 128, 495 + (index // 6) * 57
                    visible = ease((local_seconds - index * 0.035) / 0.55)
                    color = MINT if index < accepted else AMBER
                    fill = tuple(round(value * (0.16 + 0.32 * visible)) for value in color)
                    draw.rounded_rectangle((px, py, px + 104, py + 38), 8, fill=(*fill, 255), outline=(*color, 170), width=2)
                    if index >= accepted:
                        draw.line((px + 44, py + 11, px + 60, py + 27), fill=(*AMBER, 255), width=2)
                        draw.line((px + 60, py + 11, px + 44, py + 27), fill=(*AMBER, 255), width=2)
            self.panel(image, (90, 835, 1830, 900), accent=AMBER, fill=(45, 34, 24, 230))
            self.text(image, "Five refusals retained. Completion target remains open.", (120, 850), 28, AMBER)
            return
        bars = [("Strands", 9, "25 / 30 cards", "TP 9  |  FP 2  |  FN 6  |  Precision 81.8%", MINT),
                ("Full deterministic baseline", 4, "30 / 30 cards", "TP 4  |  FP 2  |  FN 11  |  Precision 66.7%", (121, 151, 172))]
        for index, (name, value, cards, metric, color) in enumerate(bars):
            y = 375 + index * 180
            self.text(image, name, (95, y), 31, color, bold=True)
            self.text(image, cards, (1390, y), 30, WHITE, bold=True)
            self.Draw.Draw(image).rounded_rectangle((95, y + 58, 1280, y + 94), 18, fill=(25, 48, 61))
            width = 1185 * value / 15 * state["bar_reveal"]
            self.Draw.Draw(image).rounded_rectangle((95, y + 58, 95 + max(36, width), y + 94), 18, fill=(*color, 255))
            self.text(image, str(value) + " relevant matches", (int(115 + width), y + 54), 26, color)
            self.text(image, metric, (95, y + 111), 25, MUTED)
        self.panel(image, (90, 790, 1830, 880), accent=AMBER, fill=(45, 34, 24, 230))
        self.text(image, "Known corpus, not an unseen holdout. Clinical benefit is unmeasured.", (120, 816), 28, AMBER)

    def h12(self, image, shot, state, local_seconds):
        self.heading(image, shot, 70)
        for index, (label, color) in enumerate((("1 verified card", MINT), ("1 refusal", AMBER))):
            y = 385 + index * 210
            x = 1170 + math.sin(local_seconds * 0.4 + index) * 12
            self.panel(image, (x - 150, y - 70, x + 500, y + 70), accent=color)
            self.text(image, label, (x - 100, y - 25), 42, color, bold=True)
        self.text(image, "11 diagnostics + 30 comparison cases unexecuted", (95, 770), 31, AMBER)
        self.text(image, "Provider refusal retained. Current-source full comparison is not measured.", (95, 827), 26, MUTED)

    def closing(self, image, shot, state, local_seconds):
        self.shield(image, (1330, 480), 1.15 + math.sin(local_seconds * 0.35) * 0.025, local_seconds)
        self.text(image, "ATROPOS", (94, 225), 27, MINT, bold=True)
        self.text(image, "MedPlain", (88, 280), 112, WHITE, bold=True)
        self.text(image, shot["title"], (94, 455), 51, WHITE, bold=True, width=860)
        self.text(image, shot.get("subtitle", ""), (96, 605), 30, MUTED, width=850)
        self.panel(image, (92, 740, 763, 816), radius=20, fill=(*MINT, 255))
        self.text(image, "TRY THE ANONYMOUS DEMO", (119, 761), 27, (6, 27, 32), bold=True)
        self.text(image, "github.com/upgradedev/atropos-medplain-aws", (95, 843), 25, MINT)
        for index in range(4):
            self.thread(image, [(945, 355 + index * 90), (1090, 420 + index * 40), (1260, 470)],
                        (state["packet_phase"] + index * 0.2) % 1)

    def scene(self, shot, seconds):
        image = self.background.copy()
        local = seconds - shot["start_seconds"]
        state = motion_state(shot["kind"], local, shot["end_seconds"] - shot["start_seconds"])
        self.atmosphere(image, seconds)
        kind = shot["kind"]
        if shot.get("capture"):
            self.ui(image, shot, state, local)
        elif kind in {"intro", "sources", "identity"}:
            self.source_cards(image, shot, state, local)
        elif kind in {"architecture", "process", "gate"}:
            self.graph(image, shot, state, local)
        elif kind == "benchmark":
            self.benchmark(image, shot, state, local)
        elif kind == "h12":
            self.h12(image, shot, state, local)
        else:
            self.closing(image, shot, state, local)
        return image

    def frame(self, shots, seconds, cues=()):
        shot_index = next((index for index, shot in enumerate(shots) if seconds < shot["end_seconds"]), len(shots) - 1)
        shot = shots[shot_index]
        image = self.scene(shot, seconds)
        remaining = shot["end_seconds"] - seconds
        if shot_index + 1 < len(shots) and remaining < 0.35:
            next_image = self.scene(shots[shot_index + 1], max(seconds, shots[shot_index + 1]["start_seconds"]))
            image = self.Image.blend(image, next_image, ease(1 - remaining / 0.35))
        self.text(image, "ATROPOS MEDPLAIN", (90, 60), 23, MINT, bold=True)
        label = shot.get("honesty_label", "CGI illustration")
        if shot.get("capture"):
            progress = (seconds - shot["start_seconds"]) / (shot["end_seconds"] - shot["start_seconds"])
            label = selected_capture(shot, progress)["provenance_label"]
        elif "cgi" not in label.lower():
            label = "CGI illustration | " + label
        self.text(image, label, (94, 112), 20, MUTED, width=1700)
        self.Draw.Draw(image).line((90, 160, 1830, 160), fill=(80, 129, 143, 45), width=1)
        for start, end, text in cues:
            if start <= seconds < end:
                lines = text.splitlines()
                draw = self.Draw.Draw(image)
                width = max(draw.textlength(line, font=self.font(36)) for line in lines)
                height = len(lines) * 48 + 24
                self.panel(image, ((1920 - width) / 2 - 32, 1045 - height, (1920 + width) / 2 + 32, 1045),
                           18, accent=(44, 77, 90), fill=(3, 10, 16, 245))
                for index, line in enumerate(lines):
                    line_width = draw.textlength(line, font=self.font(36))
                    self.text(image, line, ((1920 - line_width) / 2, 1050 - height + index * 48), 36)
                break
        return image.convert("RGB")


def prototype_shot():
    return {"id": "opening", "kind": "intro", "start_seconds": 0.0, "end_seconds": 8.0,
            "title": "Every discharge\nleaves questions.",
            "subtitle": "A home list. A discharge sheet. A caregiver trying to connect them.",
            "bullets": [], "honesty_label": "CGI illustration | Design prototype, not application footage"}
