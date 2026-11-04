"""Layered character/environment animation, with the retained motion design helpers."""
from __future__ import annotations

import math

import motion_graphics as old

KINDS = {"home", "intro", "ui", "architecture", "process", "gate", "benchmark", "h12", "closing"}


def camera_state(local, duration, depth=0.0):
    progress = old.ease(local / max(duration, 0.001))
    return {"scale": 1.025 + progress * (0.035 + depth * 0.025),
            "x": (progress - 0.5) * (24 + depth * 42),
            "y": math.sin(local * 0.36) * (3 + depth * 6)}


class FinalCanvas(old.CinematicCanvas):
    def layer(self, image, layer, local, duration):
        # Keep alpha in foreground layers; the retained screenshot loader is RGB-only.
        key = (layer["image"], "rgba")
        if key not in self.assets:
            with self.Image.open(self.root / layer["image"]) as opened:
                self.assets[key] = opened.convert("RGBA")
        source = self.assets[key]
        if layer["role"] != "background" and source.getchannel("A").getextrema()[0] == 255:
            raise ValueError("Character foreground must contain real transparency for layered parallax")
        state = camera_state(local, duration, layer["depth"])
        role = layer["role"]
        base = max(1920 / source.width, 1080 / source.height) if role == "background" else min(1920 / source.width, 1080 / source.height)
        scale = base * state["scale"]
        width, height = round(source.width * scale), round(source.height * scale)
        rendered = source.resize((width, height), self.Image.Resampling.BICUBIC)
        anchor = layer.get("anchor", [0.5, 0.5])
        x = round((1920 - width) * anchor[0] + state["x"])
        breath = math.sin(local * 1.1) * 2 if role == "subject" else 0
        y = round((1080 - height) * anchor[1] + state["y"] + breath)
        image.alpha_composite(rendered, (x, y))

    def character_scene(self, image, shot, local, duration):
        for layer in sorted(shot["layers"], key=lambda item: item["depth"]):
            self.layer(image, layer, local, duration)
        key = "final-left-shade"
        if key not in self.assets:
            shade = self.Image.new("RGBA", (1920, 1))
            pixels = shade.load()
            for x in range(1920):
                pixels[x, 0] = (3, 12, 19, round(230 * max(0, 1 - x / 1550)))
            self.assets[key] = shade.resize((1920, 1080))
        image.alpha_composite(self.assets[key])
        if shot["kind"] in {"intro", "closing"}:
            self.text(image, "ATROPOS", (94, 220), 29, old.MINT, bold=True)
            self.text(image, "MedPlain", (88, 270), 108, old.WHITE, bold=True)
            top, size = 440, 57
        else:
            top, size = 255, 72
        height = self.text(image, shot["title"], (92, top), size, bold=True, width=900)
        self.text(image, shot.get("subtitle", ""), (96, top + height + 35), 32, old.WHITE, width=830)
        for index, item in enumerate(shot.get("bullets", [])):
            self.text(image, item, (98, 690 + index * 56), 27, old.MINT, width=830,
                      alpha=round(255 * old.ease((local - index * 0.5) / 1.2)))
        if shot["kind"] == "closing":
            self.panel(image, (94, 760, 865, 840), 18, fill=(*old.MINT, 255))
            self.text(image, "TRY THE ANONYMOUS DEMO", (121, 783), 30, (4, 24, 31), bold=True)
            self.text(image, "github.com/upgradedev/atropos-medplain-aws", (96, 870), 27, old.MINT)

    def ui(self, image, shot, state, local):
        self.text(image, shot["title"], (94, 190), 48, bold=True, width=1720)
        self.text(image, shot.get("subtitle", ""), (96, 260), 25, old.MUTED, width=1720)
        capture = shot["capture"]
        source = self.asset(capture)
        bounds = (1680, 600)
        scale = min(bounds[0] / source.width, bounds[1] / source.height)
        rendered = source.resize((round(source.width * scale), round(source.height * scale)), self.Image.Resampling.LANCZOS)
        drift = camera_state(local, shot["end_seconds"] - shot["start_seconds"], 0.15)
        x = round((1920 - rendered.width) / 2 + drift["x"] * 0.15)
        y = round(325 + (bounds[1] - rendered.height) / 2)
        self.panel(image, (x - 12, y - 12, x + rendered.width + 12, y + rendered.height + 12), 12)
        image.paste(rendered, (x, y))

    def graph(self, image, shot, state, local):
        if shot["kind"] != "process" or not shot.get("nodes"):
            return super().graph(image, shot, state, local)
        self.text(image, shot["title"], (94, 205), 58, bold=True, width=1700)
        self.text(image, shot.get("subtitle", ""), (96, 295), 29, old.MUTED, width=1700)
        nodes = shot["nodes"]
        xs = [320 + index * 1280 / max(1, len(nodes) - 1) for index in range(len(nodes))]
        for index, x in enumerate(xs):
            if index + 1 < len(xs):
                self.thread(image, [(x + 180, 550), (xs[index + 1] - 180, 550)], (local * 0.22 + index * 0.18) % 1)
            active = int(local / 1.4) % len(nodes) == index
            self.panel(image, (x - 185, 450, x + 185, 650), accent=old.MINT if active else old.MUTED)
            self.text(image, nodes[index], (x - 160, 495), 29, old.MINT if active else old.WHITE, bold=True, width=325)
        self.text(image, shot.get("process_note", "Explicit human decisions remain separate from model proposals."),
                  (96, 790), 29, old.MUTED, width=1700)

    def scene(self, shot, seconds):
        if shot["kind"] in {"home", "intro", "closing"}:
            image = self.background.copy()
            self.character_scene(image, shot, max(0, seconds - shot["start_seconds"]), shot["end_seconds"] - shot["start_seconds"])
            return image
        return super().scene(shot, seconds)
