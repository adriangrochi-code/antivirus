"""centinela: adapta el sistema operativo al hardware.

  centinela probe                 detecta el hardware
  centinela bench [--quick]       mide cada componente
  centinela plan  [--uso X]       muestra el plan de ajuste (no cambia nada)
  centinela apply [--dry-run]     aplica el plan (archivos + en caliente)
  centinela power <perfil>        silencioso | equilibrado | rendimiento
  centinela build-kernel          compila el kernel a medida para este equipo
  centinela setup                 todo lo anterior, para el primer arranque
  centinela adaptd                demonio de adaptación en tiempo real
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys

from . import apply as ap
from . import bench, hw
from .adaptd import AdaptDaemon
from .tuner import POWER_PROFILES, USE_CASES, Plan, make_plan

KERNEL_SCRIPT = os.environ.get("CENTINELA_KERNEL_SCRIPT", "/usr/lib/centinela/kernel/build-kernel.sh")


def _plan_from_state(args) -> Plan:
    probe = ap.load_state("hardware.json") or hw.probe()
    measures = ap.load_state("bench.json") or {}
    prefs = ap.load_state("prefs.json") or {}
    use = getattr(args, "uso", None) or prefs.get("uso", "escritorio")
    prof = getattr(args, "perfil", None) or prefs.get("perfil", "equilibrado")
    return make_plan(probe, measures, use, prof)


def cmd_probe(args) -> int:
    p = hw.probe()
    if not args.no_save:
        ap.save_state("hardware.json", p)
    print(json.dumps(p, indent=2, ensure_ascii=False) if args.json else "\n".join(hw.summarize(p)))
    return 0


def cmd_bench(args) -> int:
    p = ap.load_state("hardware.json") or hw.probe()
    b = bench.run_all(p, quick=args.quick, progress=lambda m: print(f"· {m}", flush=True))
    ap.save_state("bench.json", b)
    print(json.dumps(b, indent=2) if args.json else "\n".join(bench.summarize(b)))
    return 0


def cmd_plan(args) -> int:
    plan = _plan_from_state(args)
    if args.json:
        print(json.dumps(plan.to_dict(), indent=2, ensure_ascii=False))
        return 0
    print(f"Plan para uso '{plan.use_case}', perfil de potencia '{plan.power_profile}':\n")
    for r in plan.reasons:
        print(f"  • {r}")
    if plan.recommendations:
        print("\nRecomendaciones de hardware:")
        for r in plan.recommendations:
            print(f"  ⚠ {r}")
    print("\nFragmento de kernel:\n  " + "\n  ".join(plan.kernel_fragment))
    print("\nLínea de arranque:\n  " + " ".join(plan.cmdline))
    return 0


def cmd_apply(args) -> int:
    plan = _plan_from_state(args)
    ap.save_state("prefs.json", {"uso": plan.use_case, "perfil": plan.power_profile})
    for f in ap.persist(plan, dry_run=args.dry_run):
        print(f"escrito {f}")
    act = ap.Actuator(dry_run=args.dry_run)
    ap.apply_runtime(plan, act)
    print(f"{len(act.changes)} ajustes en caliente")
    if not args.dry_run and os.path.exists("/usr/sbin/update-grub"):
        subprocess.run(["/usr/sbin/update-grub"], check=False)
    return 0


def cmd_power(args) -> int:
    prefs = ap.load_state("prefs.json") or {}
    prefs["perfil"] = args.perfil
    ap.save_state("prefs.json", prefs)
    plan = _plan_from_state(args)
    act = ap.Actuator(dry_run=args.dry_run)
    ap.apply_power(plan, act)
    for p, v in act.changes:
        print(f"{p} = {v}")
    return 0


def cmd_build_kernel(args) -> int:
    plan = _plan_from_state(args)
    ap.persist(plan)
    env = dict(os.environ, KERNEL_PROFILE="tailored",
               HW_FRAGMENT=os.path.join(ap.ETC, "centinela/hardware.config"),
               BUILTIN_MODULES=os.path.join(ap.ETC, "centinela/builtin-modules.txt"),
               BUILD_DIR=args.build_dir)
    import shutil
    if not all(shutil.which(t) for t in ("gcc", "make", "flex", "bison", "dpkg-buildpackage", "pahole")):
        print("Instalando las herramientas de compilación del kernel…")
        subprocess.run(["apt-get", "install", "-y", "build-essential", "bc", "flex", "bison", "libssl-dev",
                        "libelf-dev", "libdw-dev", "dwarves", "debhelper", "rsync", "cpio", "curl", "zstd"],
                       check=True)
    print("Compilando el kernel a medida (puede tardar entre 15 y 60 minutos)…")
    r = subprocess.run([KERNEL_SCRIPT], env=env)
    if r.returncode != 0:
        return r.returncode
    debs = [os.path.join(args.build_dir, "kernel-debs", f)
            for f in os.listdir(os.path.join(args.build_dir, "kernel-debs"))
            if f.endswith(".deb") and "-dbg" not in f and "tailored" in f]
    if args.firmar_dkms:
        setup_dkms_signing(os.path.join(args.build_dir, f"linux-{os.environ.get('KERNEL_VERSION', '6.18.63')}"))
    if args.install and debs:
        subprocess.run(["dpkg", "-i", *debs], check=True)
        print("Kernel instalado. El kernel genérico sigue disponible en 'Opciones avanzadas' de GRUB.")
    return 0


KEYS_DIR = "/var/lib/centinela/keys"


def setup_dkms_signing(src: str) -> None:
    """Conserva la clave de firma del kernel a medida para que DKMS firme módulos externos
    (p. ej. el driver de NVIDIA, necesario para CUDA/Strata). Compromiso de seguridad: con
    esta clave en disco, root puede firmar módulos; sin ella, ningún módulo externo carga."""
    import shutil
    key, crt = os.path.join(src, "certs/signing_key.pem"), os.path.join(src, "certs/signing_key.x509")
    if not (os.path.exists(key) and os.path.exists(crt)):
        raise ValueError(f"No está la clave de firma de la build en {src}/certs")
    os.makedirs(KEYS_DIR, mode=0o700, exist_ok=True)
    shutil.copy(key, os.path.join(KEYS_DIR, "signing_key.pem"))
    shutil.copy(crt, os.path.join(KEYS_DIR, "signing_key.x509"))
    os.chmod(os.path.join(KEYS_DIR, "signing_key.pem"), 0o600)
    os.makedirs("/etc/dkms/framework.conf.d", exist_ok=True)
    with open("/etc/dkms/framework.conf.d/tisma.conf", "w", encoding="utf-8") as fh:
        fh.write(f"mok_signing_key={KEYS_DIR}/signing_key.pem\nmok_certificate={KEYS_DIR}/signing_key.x509\n")
    print("DKMS firmará los módulos externos con la clave de este kernel.")


def cmd_setup(args) -> int:
    print("== Centinela: adaptando el sistema a este equipo ==")
    args.no_save, args.json = False, False
    cmd_probe(args)
    cmd_bench(args)
    args.dry_run = False
    cmd_plan(args)
    return cmd_apply(args)


def cmd_adaptd(args) -> int:
    plan = _plan_from_state(args)
    AdaptDaemon(plan, interval=args.interval, dry_run=args.dry_run,
                allow_game_boost=not args.no_game_boost).run()
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="centinela", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("probe")
    s.add_argument("--json", action="store_true")
    s.add_argument("--no-save", action="store_true")
    s.set_defaults(func=cmd_probe)

    s = sub.add_parser("bench")
    s.add_argument("--quick", action="store_true")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_bench)

    for name, func in (("plan", cmd_plan), ("apply", cmd_apply), ("setup", cmd_setup),
                       ("build-kernel", cmd_build_kernel), ("adaptd", cmd_adaptd)):
        s = sub.add_parser(name)
        s.add_argument("--uso", choices=USE_CASES)
        s.add_argument("--perfil", choices=POWER_PROFILES)
        s.add_argument("--dry-run", action="store_true")
        s.add_argument("--json", action="store_true")
        s.add_argument("--quick", action="store_true")
        s.set_defaults(func=func)
        if name == "build-kernel":
            s.add_argument("--build-dir", default="/var/cache/centinela/kernel")
            s.add_argument("--install", action="store_true")
            s.add_argument("--firmar-dkms", action="store_true",
                           help="conservar la clave de firma para módulos DKMS (NVIDIA/CUDA)")
        if name == "adaptd":
            s.add_argument("--interval", type=float, default=3.0)
            s.add_argument("--no-game-boost", action="store_true")

    s = sub.add_parser("power")
    s.add_argument("perfil", choices=POWER_PROFILES)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_power)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (ValueError, PermissionError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
