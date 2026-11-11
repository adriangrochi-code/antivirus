#!/usr/bin/env python3
"""Genera los PNG de marca (GRUB, fondo de pantalla) a partir de los SVG."""
import struct
import subprocess
import sys
import zlib
from pathlib import Path


def solid_png(path: Path, rgba: tuple, w: int = 8, h: int = 8) -> None:
    raw = b"".join(b"\x00" + bytes(rgba) * w for _ in range(h))
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b"")
    path.write_bytes(png)


def svg(src: Path, dst: Path, width: int) -> None:
    subprocess.run(["rsvg-convert", "-w", str(width), str(src), "-o", str(dst)], check=True)


def main(branding: Path, root: Path) -> None:
    grub = root / "boot/grub/themes/centinela"
    grub.mkdir(parents=True, exist_ok=True)
    svg(branding / "wallpaper.svg", grub / "background.png", 1920)
    svg(branding / "logo.svg", grub / "logo.png", 112)
    svg(branding / "wordmark.svg", grub / "wordmark.png", 320)
    for part in ("c", "n", "s", "e", "w", "ne", "nw", "se", "sw"):
        solid_png(grub / f"select_{part}.png", (90, 43, 143, 255))
        solid_png(grub / f"terminal_{part}.png", (28, 27, 32, 235))

    wp = root / "usr/share/wallpapers/Centinela"
    (wp / "contents/images").mkdir(parents=True, exist_ok=True)
    for w, h in ((1920, 1080), (2560, 1440), (3840, 2160)):
        svg(branding / "wallpaper.svg", wp / f"contents/images/{w}x{h}.png", w)
    (wp / "metadata.json").write_text(
        '{"KPlugin": {"Id": "Centinela", "Name": "Centinela", "License": "CC-BY-SA-4.0",'
        ' "Authors": [{"Name": "TISMA OS"}]}}\n')

    b = root / "usr/share/centinela/branding"
    b.mkdir(parents=True, exist_ok=True)
    for f in ("logo.svg", "logo-light.svg", "wordmark.svg", "wallpaper.svg"):
        (b / f).write_bytes((branding / f).read_bytes())
    icons = root / "usr/share/icons/hicolor"
    for size in (16, 32, 48, 64, 128, 256):
        d = icons / f"{size}x{size}/apps"
        d.mkdir(parents=True, exist_ok=True)
        svg(branding / "logo.svg", d / "tisma.png", size)
    (icons / "scalable/apps").mkdir(parents=True, exist_ok=True)
    (icons / "scalable/apps/tisma.svg").write_bytes((branding / "logo.svg").read_bytes())
    # Plymouth (pantalla de arranque): el tema «spinner» de Debian (dos pasos: también pide la clave de LUKS
    # y muestra los mensajes) con el logo de TISMA y su fondo grafito
    spinner = root / "usr/share/plymouth/themes/spinner"
    if spinner.is_dir():
        ply = root / "usr/share/plymouth/themes/tisma"
        ply.mkdir(parents=True, exist_ok=True)
        for f in spinner.glob("*.png"):
            (ply / f.name).write_bytes(f.read_bytes())
        svg(branding / "logo.svg", ply / "watermark.png", 150)
        (ply / "tisma.plymouth").write_text(
            "[Plymouth Theme]\nName=TISMA\nDescription=Logo de TISMA OS sobre grafito\nModuleName=two-step\n\n"
            "[two-step]\nFont=Noto Sans 12\nTitleFont=Noto Sans Light 26\nImageDir=/usr/share/plymouth/themes/tisma\n"
            "DialogHorizontalAlignment=.5\nDialogVerticalAlignment=.70\nTitleHorizontalAlignment=.5\n"
            "TitleVerticalAlignment=.62\nHorizontalAlignment=.5\nVerticalAlignment=.72\n"
            "WatermarkHorizontalAlignment=.5\nWatermarkVerticalAlignment=.42\nTransition=none\n"
            "TransitionDuration=0.0\nBackgroundStartColor=0x232227\nBackgroundEndColor=0x131216\n\n"
            "[boot-up]\nUseEndAnimation=false\n\n[shutdown]\nUseEndAnimation=false\n\n[reboot]\nUseEndAnimation=false\n")
    # SDDM (pantalla de inicio de sesión)
    sddm = root / "usr/share/sddm/themes/breeze"
    sddm.mkdir(parents=True, exist_ok=True)
    (sddm / "theme.conf.user").write_text(
        "[General]\nbackground=/usr/share/wallpapers/Centinela/contents/images/3840x2160.png\n"
        "type=image\naccentColor=#9145E6\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
