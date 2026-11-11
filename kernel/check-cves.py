#!/usr/bin/env python3
"""Comprueba los CVEs conocidos del kernel contra NUESTRA build.

1. Descarga la base OSV del kernel (los CVEs que publica el CNA de kernel.org, con los
   rangos de versión corregidos por rama estable).
2. Selecciona los CVEs que afectan a la versión compilada (no corregidos en su rama).
3. Para cada uno localiza el subsistema afectado (prefijo del título: «ksmbd:», «net/sched:»…)
   y comprueba si ese código **está compilado** en el árbol de build (existen sus .o).
4. Clasifica:
     - corregible: hay una versión estable posterior que lo corrige → actualizar KERNEL_VERSION
     - mitigable: subsistema prescindible y compilado → se desactiva en 60-cve-mitigacion.config
     - pendiente: compilado y necesario, sin parche estable todavía → se informa
     - no aplica: el código no está en nuestro kernel
Uso: check-cves.py --src build/linux-X --version X [--fragment kernel/fragments/60-cve-mitigacion.config]
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

OSV_URL = "https://osv-vulnerabilities.storage.googleapis.com/Linux/all.zip"

# Subsistemas que un escritorio/estación de ingeniería no necesita: si tienen CVEs abiertos
# se desactivan. (prefijo del CVE en minúsculas → símbolos Kconfig a desactivar)
DISPENSABLE = {
    "ksmbd": ["SMB_SERVER"], "smb: server": ["SMB_SERVER"],
    "can": ["CAN"], "net/smc": ["SMC"], "smc": ["SMC"], "tipc": ["TIPC"], "rds": ["RDS"],
    "sctp": ["IP_SCTP"], "dccp": ["IP_DCCP"], "atm": ["ATM"], "ax25": ["AX25"], "netrom": ["NETROM"],
    "rose": ["ROSE"], "x25": ["X25"], "nfc": ["NFC"], "hsr": ["HSR"], "6lowpan": ["6LOWPAN"],
    "ieee802154": ["IEEE802154"], "mac802154": ["MAC802154"], "llc": ["LLC2"], "phonet": ["PHONET"],
    "caif": ["CAIF"], "batman-adv": ["BATMAN_ADV"], "openvswitch": ["OPENVSWITCH"],
    "net/sched: act_ct": ["NET_ACT_CT"], "mptcp": ["MPTCP"], "nilfs2": ["NILFS2_FS"],
    "hfs": ["HFS_FS"], "hfsplus": ["HFSPLUS_FS"], "jfs": ["JFS_FS"], "ocfs2": ["OCFS2_FS"],
    "reiserfs": ["REISERFS_FS"], "gfs2": ["GFS2_FS"], "udf": ["UDF_FS"], "erofs": ["EROFS_FS"],
    "ntfs3": ["NTFS3_FS"], "orangefs": ["ORANGEFS_FS"], "9p": ["NET_9P"], "net/9p": ["NET_9P"],
    "fbdev": ["FB_DEVICE"], "vmwgfx": ["DRM_VMWGFX"], "drm/vmwgfx": ["DRM_VMWGFX"],
    "firewire": ["FIREWIRE"], "media: vivid": ["VIDEO_VIVID"], "usb: gadget": ["USB_GADGET"],
    "staging": ["STAGING"], "comedi": ["COMEDI"], "isdn": ["ISDN"], "appletalk": ["ATALK"],
    "nfsd": ["NFSD"], "ipvs": ["IP_VS"], "l2tp": ["L2TP"], "bonding": ["BONDING"], "net: bonding": ["BONDING"],
}
# Ya mitigados por la configuración de TISMA OS (sysctl / cmdline / fragmentos)
MITIGATED = {
    "io_uring": "kernel.io_uring_disabled=1 (solo procesos privilegiados)",
    "bpf": "kernel.unprivileged_bpf_disabled=1 + BPF_JIT_ALWAYS_ON",
    "kexec": "kernel.kexec_load_disabled=1 + lockdown=integrity",
    "userfaultfd": "vm.unprivileged_userfaultfd=0",
    "tty": "dev.tty.ldisc_autoload=0 + LEGACY_TIOCSTI desactivado",
    "n_hdlc": "módulo bloqueado en modprobe.d",
    "firewire": "módulos bloqueados en modprobe.d",
    "perf": "kernel.perf_event_paranoid=3",
}
ARCH_SKIP = ("loongarch", "arm64", "arm:", "riscv", "powerpc", "s390", "mips", "sparc", "parisc", "xtensa",
             "m68k", "openrisc", "alpha", "hexagon", "csky", "sh:", "um:", "arc:")


def vtuple(s: str) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", s.split("-")[0])[:3]]   # «6.18-rc3» → (6, 18, 0)
    return tuple(nums + [0] * (3 - len(nums)))  # type: ignore[return-value]


def load_osv(cache: Path) -> zipfile.ZipFile:
    if not cache.exists() or cache.stat().st_size < 1_000_000:
        print(f"Descargando base OSV del kernel → {cache}", file=sys.stderr)
        cache.parent.mkdir(parents=True, exist_ok=True)
        with urllib.request.urlopen(OSV_URL, timeout=300) as r:  # noqa: S310
            cache.write_bytes(r.read())
    return zipfile.ZipFile(io.BytesIO(cache.read_bytes()))


def affecting(z: zipfile.ZipFile, version: str):
    K = vtuple(version)
    for name in z.namelist():
        if not name.startswith("CVE-"):
            continue
        d = json.loads(z.read(name))
        if d.get("withdrawn"):
            continue
        hit, later_fix, fixed_here = False, None, False
        for a in d.get("affected", []):
            for r in a.get("ranges", []):
                if r.get("type") != "ECOSYSTEM":
                    continue
                intro = fixed = None
                for e in r["events"]:
                    if "introduced" in e:
                        intro = vtuple(e["introduced"])
                    if "fixed" in e:
                        fixed = vtuple(e["fixed"])
                # Los rangos de kernel.org se SOLAPAN (uno por rama: «6.7→6.18.11», «6.13→6.19.1»…): si NUESTRA
                # rama tiene la corrección en esta versión o antes, no nos afecta, diga lo que diga otro rango
                if fixed is not None and fixed[:2] == K[:2]:
                    if fixed <= K:
                        fixed_here = True
                    else:
                        later_fix = fixed    # hay estable posterior de NUESTRA rama que lo corrige
                if intro is None or intro > K:
                    continue
                if fixed is not None and fixed <= K:
                    continue
                if fixed is not None and fixed[:2] < K[:2]:
                    continue                 # corrección de una rama anterior que no es la nuestra
                hit = True
        if fixed_here:
            continue
        if hit:
            yield {"id": d["id"], "summary": (d.get("summary") or "").strip(),
                   "fixed_in": ".".join(map(str, later_fix)) if later_fix else None}


class SourceIndex:
    """Índice de directorios y archivos del árbol para ubicar subsistemas y ver si compilaron."""

    def __init__(self, src: Path):
        self.src = src
        self.by_name: dict[str, list[Path]] = defaultdict(list)
        for dirpath, dirnames, files in os.walk(src):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in ("Documentation", "tools", "scripts")]
            p = Path(dirpath)
            self.by_name[p.name.lower()].append(p)
            for f in files:
                if f.endswith(".c"):
                    self.by_name[f[:-2].lower()].append(p / f)

    def locate(self, prefix: str) -> list[Path]:
        cands: list[Path] = []
        for tok in [t.strip().lower() for t in prefix.split(":")]:
            if "/" in tok:
                for base in ("", "drivers/", "net/", "fs/", "sound/", "drivers/gpu/"):
                    p = self.src / (base + tok)
                    if p.exists():
                        cands.append(p)
                last = tok.rsplit("/", 1)[-1]
                cands += self.by_name.get(last, [])
            else:
                cands += self.by_name.get(tok.replace(" ", "_"), []) + self.by_name.get(tok.replace("-", "_"), [])
            if cands:
                break
        return cands

    @staticmethod
    def compiled(paths: list[Path]) -> bool:
        for p in paths:
            if p.is_file() and p.with_suffix(".o").exists():
                return True
            if p.is_dir() and any(p.rglob("*.o")):
                return True
        return False


GENERIC = {"net", "fs", "usb", "scsi", "drm", "pci", "mm", "hid", "media", "sound", "alsa", "asoc",
           "crypto", "block", "kernel", "x86", "arch", "driver", "drivers", "core", "misc", "platform"}


def _tokens(summary: str) -> list[str]:
    """«net: macb: Fix …» → ['net', 'macb'] (todo lo anterior a la descripción)."""
    parts = [p.strip() for p in summary.split(":")]
    return [p for p in parts[:-1] if p and len(p) <= 40 and len(p.split()) <= 3]


def classify(cves, idx: SourceIndex):
    out = []
    for c in cves:
        toks = _tokens(c["summary"])
        low = c["summary"].lower()
        if low.startswith(ARCH_SKIP):
            c["estado"] = "no aplica (otra arquitectura)"
            out.append(c)
            continue
        mitig = None
        for i in range(len(toks), 0, -1):          # «net/sched: act_ct» antes que «net/sched»
            key = ":".join(toks[:i]).lower().replace(": ", ":").replace(":", ": ")
            mitig = mitig or DISPENSABLE.get(key) or DISPENSABLE.get(toks[i - 1].lower())
        paths: list[Path] = []
        for t in reversed(toks):                    # el subsistema más específico primero
            if t.lower() in GENERIC and len(toks) > 1:
                continue
            paths = idx.locate(t)
            if paths:
                break
        built = idx.compiled(paths) if paths else None
        mitigated = next((MITIGATED[t.split("/")[0].lower()] for t in toks if t.split("/")[0].lower() in MITIGATED), None)
        if built is False:
            c["estado"] = "no aplica (código no compilado)"
        elif c["fixed_in"]:
            c["estado"] = f"corregible: actualizar a {c['fixed_in']}"
        elif mitigated:
            c["estado"] = "mitigado: " + mitigated
        elif mitig:
            c["estado"] = "mitigable: se desactiva " + ", ".join(mitig)
            c["symbols"] = mitig
        elif built:
            c["estado"] = "pendiente: compilado, sin parche estable"
        else:
            c["estado"] = "revisar: subsistema no identificado"
        out.append(c)
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True, type=Path)
    ap.add_argument("--version", required=True)
    ap.add_argument("--cache", type=Path, default=Path("build/osv/linux.zip"))
    ap.add_argument("--fragment", type=Path, help="escribir fragmento de mitigación")
    ap.add_argument("--report", type=Path, help="informe JSON")
    a = ap.parse_args()

    z = load_osv(a.cache)
    cves = list(affecting(z, a.version))
    res = classify(cves, SourceIndex(a.src))
    groups: dict[str, list] = defaultdict(list)
    for c in res:
        groups[c["estado"].split(":")[0].split(" (")[0]].append(c)
    print(f"Kernel {a.version}: {len(res)} CVEs publicados sin corregir en esta versión")
    for k in ("corregible", "mitigado", "mitigable", "pendiente", "revisar", "no aplica"):
        print(f"  {k:<11} {len(groups.get(k, []))}")
    newest = max((vtuple(c["fixed_in"]) for c in groups.get("corregible", [])), default=None)
    if newest:
        print(f"→ Actualiza KERNEL_VERSION a {'.'.join(map(str, newest))} o posterior")
    syms = sorted({s for c in groups.get("mitigable", []) for s in c["symbols"]})
    if a.fragment:
        lines = ["# Generado por kernel/check-cves.py: subsistemas prescindibles con CVEs abiertos",
                 f"# Kernel {a.version}. Regenerar en cada actualización."]
        lines += [f"# CONFIG_{s} is not set" for s in syms]
        a.fragment.write_text("\n".join(lines) + "\n")
        print(f"Fragmento escrito: {a.fragment} ({len(syms)} subsistemas desactivados)")
    if a.report:
        a.report.write_text(json.dumps(res, indent=1, ensure_ascii=False))
    print("\nPendientes (compilados, sin parche estable):")
    for c in groups.get("pendiente", [])[:60]:
        print(f"  {c['id']}  {c['summary'][:100]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
