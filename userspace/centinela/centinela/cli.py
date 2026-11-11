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
import re
import subprocess
import sys

from . import apply as ap
from . import bench, hw
from .adaptd import AdaptDaemon
from .tuner import POWER_PROFILES, USE_CASES, Plan, make_plan

KERNEL_SCRIPT = os.environ.get("CENTINELA_KERNEL_SCRIPT", "/usr/lib/centinela/kernel/build-kernel.sh")


def _factory_power_limits(probe: dict) -> None:
    """Guarda los límites de potencia la primera vez que se ven y los anota en el probe como «fabrica_w»."""
    saved = ap.load_state("potencia-fabrica.json") or {}
    changed = False
    for z in (probe.get("power") or {}).get("rapl", []):
        for cname, cons in (z.get("constraints") or {}).items():
            key = f"{z.get('zone')}:{cname}"
            if key not in saved and cons.get("limit_w"):
                saved[key] = cons["limit_w"]
                changed = True
            cons["fabrica_w"] = saved.get(key)
    if changed:
        try:
            ap.save_state("potencia-fabrica.json", saved)
        except OSError:
            pass


def _plan_from_state(args) -> Plan:
    probe = ap.load_state("hardware.json") or hw.probe()
    _factory_power_limits(probe)
    measures = ap.load_state("bench.json") or {}
    prefs = ap.load_state("prefs.json") or {}
    # Con batería, el uso por defecto es «portátil» (antes todo era «escritorio», también la A275)
    default_use = "portatil" if (probe.get("power") or {}).get("battery") else "escritorio"
    use = getattr(args, "uso", None) or prefs.get("uso", default_use)
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
    # Se conservan las demás preferencias (overclock elegido, perfil del autoajuste): antes se sobrescribía
    # el archivo en cada arranque y el perfil probado del autoajuste nunca se volvía a aplicar.
    prefs = ap.load_state("prefs.json") or {}
    prefs.update({"uso": plan.use_case, "perfil": plan.power_profile})
    ap.save_state("prefs.json", prefs)
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


def _kernel_version() -> str:
    """Última versión de la rama LTS (con todos los parches) o la fijada si no hay red."""
    if os.environ.get("KERNEL_VERSION"):
        return os.environ["KERNEL_VERSION"]
    latest = os.path.join(os.path.dirname(KERNEL_SCRIPT), "latest-lts.sh")
    try:
        v = subprocess.run([latest], capture_output=True, text=True, timeout=60).stdout.strip()
        if re.fullmatch(r"\d+\.\d+\.\d+", v):
            return v
    except (OSError, subprocess.TimeoutExpired):
        pass
    return "6.18.63"


def cmd_build_kernel(args) -> int:
    plan = _plan_from_state(args)
    ap.persist(plan)
    version = _kernel_version()
    print(f"Versión del kernel: {version}")
    env = dict(os.environ, KERNEL_PROFILE="tailored", KERNEL_VERSION=version,
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
        print("AVISO DE SEGURIDAD: con la clave de firma en disco, cualquiera que obtenga root podrá firmar\n"
              "módulos del kernel y anular la protección lockdown. Úsalo solo si necesitas drivers externos\n"
              "(NVIDIA/CUDA). La clave queda en /var/lib/centinela/keys (solo root).")
        setup_dkms_signing(os.path.join(args.build_dir, f"linux-{version}"))
    else:
        # Sin --firmar-dkms la clave privada de firma de módulos no debe quedar en disco: con ella, root podría
        # firmar módulos y saltarse el lockdown.
        for name in ("signing_key.pem", "signing_key.x509.priv"):
            try:
                os.remove(os.path.join(args.build_dir, f"linux-{version}", "certs", name))
            except FileNotFoundError:
                pass
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
    rc = cmd_apply(args)
    try:                                    # gráficos: el mejor driver y capa de traducción para esta GPU
        from . import graficos
        for m in graficos.apply(graficos.detect()):
            print(m)
    except Exception as e:  # noqa: BLE001 — la adaptación gráfica nunca debe romper la configuración base
        print(f"Gráficos: no se pudo adaptar ({e})")
    return rc


def cmd_graficos(args) -> int:
    from . import graficos
    if args.accion in ("aplicar", "confirmar", "probar", "revertir") and os.geteuid() != 0:
        print("Necesita permisos de administrador: sudo centinela graficos " + args.accion, file=sys.stderr)
        return 2
    if args.accion == "detectar":
        info = graficos.detect()
        print(graficos.to_json(info) if args.json else graficos.summarize(info))
    elif args.accion == "aplicar":
        print("\n".join(graficos.apply(graficos.detect())))
    elif args.accion == "confirmar":
        print(f"Gráficos: {graficos.confirm()}")
    elif args.accion == "probar":
        print(graficos.retry())
    else:
        print(graficos.revert())
    return 0


def cmd_energia(args) -> int:
    from . import energia
    act = ap.Actuator(dry_run=args.dry_run)
    if args.modo == "umbral":
        print(energia.set_threshold(args.valor, act))
        return 0
    mode = args.modo if args.modo != "auto" else ("corriente" if energia.on_ac() else "bateria")
    r = energia.apply(mode, act)
    print(f"Energía: modo {'batería' if mode == 'bateria' else 'cargador'} ({r['cambios']} ajustes)")
    return 0


def cmd_status(args) -> int:
    from . import status
    print(json.dumps(status.snapshot(), ensure_ascii=False))
    return 0


def cmd_oc(args) -> int:
    """Overclock de la GPU integrada y Turbo de CPU (root; la app lo invoca con pkexec)."""
    from . import sysfs as _s
    act = ap.Actuator(dry_run=args.dry_run)
    if args.gpu is not None:
        pct = int(args.gpu)
        if not 0 <= pct <= ap.OC_MAX:
            raise ValueError(f"El overclock de GPU debe estar entre 0 y {ap.OC_MAX} %")
        cards = [c for c in _s.ls("/sys/class/drm/card[0-9]") if _s.read(f"{c}/device/pp_sclk_od") is not None]
        if not cards:
            raise ValueError("La GPU no expone overdrive: arranca con amdgpu.ppfeaturemask=0xffffffff "
                             "(perfil rendimiento de centinela) y reinicia")
        for c in cards:
            act.write(f"{c}/device/pp_sclk_od", str(pct))
        prefs = ap.load_state("prefs.json") or {}
        prefs["gpu_oc"] = pct
        ap.save_state("prefs.json", prefs)
    if args.turbo is not None:
        act.write("/sys/devices/system/cpu/cpufreq/boost", "1" if args.turbo == "si" else "0")
    for p_, v in act.changes:
        print(f"{p_} = {v}")
    return 0


def cmd_autotune(args) -> int:
    from . import autotune
    if args.al_arrancar:
        print(autotune.boot_apply(args.dry_run))
        return 0
    r = autotune.run(seconds=args.segundos, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(r, ensure_ascii=False))
        return 0
    for p_ in r["pasos"]:
        print(f"  {'✓' if p_['ok'] else '✗'} {p_['ajuste']} = {p_['valor']}  {p_['motivo'] or 'estable'}  "
              f"({p_['temp_max']:.0f} °C)")
    print("Resultado:", json.dumps(r["resultado"], ensure_ascii=False))
    print("Nota:", r["aviso_cpu_uv"])
    return 0


def cmd_boot(args) -> int:
    from . import boot
    r = boot.analyze()
    if args.json:
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0
    et = r["etapas"]
    print("Arranque: " + " + ".join(f"{k} {v:.1f} s" for k, v in et.items() if k != "escritorio"))
    if "escritorio" in et:
        print(f"Escritorio visible a los {et['escritorio']:.1f} s de userspace")
    print("\nMás lentos:")
    for t, u in r["lentos"][:10]:
        print(f"  {t:6.2f} s  {u}")
    if r["recomendaciones"]:
        print("\nRecomendaciones:")
        for x in r["recomendaciones"]:
            print(f"  • {x}")
    return 0


def cmd_adaptd(args) -> int:
    plan = _plan_from_state(args)
    # Si el usuario cambia de perfil (centinela-perfil@), adaptd recalcula su plan base en vez de volver al viejo
    AdaptDaemon(plan, interval=args.interval, dry_run=args.dry_run, allow_game_boost=not args.no_game_boost,
                reload_plan=lambda: _plan_from_state(argparse.Namespace(uso=None, perfil=None, quick=True))).run()
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

    s = sub.add_parser("graficos", help="mejor driver y capa de traducción (Vulkan/DXVK/WineD3D) para la GPU")
    s.add_argument("accion", nargs="?", default="detectar",
                   choices=["detectar", "aplicar", "confirmar", "probar", "revertir"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_graficos)

    s = sub.add_parser("energia", help="ahorro de energía según cargador/batería, y umbral de carga")
    s.add_argument("modo", choices=["auto", "bateria", "corriente", "umbral"])
    s.add_argument("valor", nargs="?", type=int, help="umbral: % máximo de carga (100 = sin límite)")
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_energia)

    s = sub.add_parser("estado", help="métricas en vivo en JSON (sin privilegios)")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("oc", help="overclock de la GPU integrada (0-15 %) y Turbo de CPU")
    s.add_argument("--gpu", type=int, help="porcentaje sobre la frecuencia de fábrica")
    s.add_argument("--turbo", choices=["si", "no"])
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_oc)

    s = sub.add_parser("autotune", help="overclock y undervolt automáticos con pruebas de estabilidad")
    s.add_argument("--segundos", type=int, default=45, choices=range(10, 601), metavar="10-600")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--json", action="store_true")
    s.add_argument("--al-arrancar", action="store_true", help="(servicio) comprobar cuelgues y reaplicar el perfil")
    s.set_defaults(func=cmd_autotune)

    s = sub.add_parser("arranque", help="analiza qué retrasa el arranque y cómo mejorarlo")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_boot)

    s = sub.add_parser("power")
    s.add_argument("perfil", choices=POWER_PROFILES)
    s.add_argument("--dry-run", action="store_true")
    s.set_defaults(func=cmd_power)
    return p


# Lo único que se puede pedir con pkexec (autorización de «overclock» desde TISMA Rendimiento). El resto
# (compilar e instalar kernels, aplicar planes…) exige sudo: si no, la misma autorización (que se recuerda unos
# minutos) permitiría a cualquier programa de la sesión instalar un kernel compilado desde una carpeta suya.
PKEXEC_ALLOWED = {"oc", "autotune", "estado", "arranque", "energia"}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if os.environ.get("PKEXEC_UID") and args.cmd not in PKEXEC_ALLOWED:
        print(f"error: «centinela {args.cmd}» no se puede ejecutar con pkexec; usa sudo", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(name)s: %(message)s")
    try:
        return args.func(args)
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
