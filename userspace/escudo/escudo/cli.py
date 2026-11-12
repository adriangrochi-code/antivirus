"""Interfaz de línea de comandos de Escudo."""

from __future__ import annotations

import argparse
import json
import os
import re
import logging
import signal
import sys
import threading
from pathlib import Path

from .config import Config
from .firewall import Firewall
from .monitor import RealTimeProtection
from .netguard import NetGuard
from .quarantine import Quarantine
from .scanner import Scanner
from .signatures import SignatureDB


def _setup_logging(cfg: Config, verbose: bool) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(cfg.log_file, encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)


def cmd_scan(cfg: Config, args) -> int:
    scanner = Scanner(cfg)
    q = Quarantine(cfg.quarantine_dir)
    total = 0
    for target in args.paths:
        report = scanner.scan(target, on_detection=print)
        total += len(report.detections)
        print(f"{target}: {report.scanned} archivos, {len(report.detections)} amenazas, "
              f"{report.errors} errores, {report.skipped} omitidos")
        if args.quarantine:
            for d in {d.path: d for d in report.detections if d.severity != "low"}.values():
                try:
                    print(f"  -> cuarentena {q.add(d.path, d.name, expected=d.ident)}: {d.path!r}")
                except OSError as e:
                    print(f"  !! no se aisló {d.path!r}: {e}")
    return 1 if total else 0


def sd_notify(estado: str) -> bool:
    """Avisa a systemd (Type=notify). La pantalla de inicio de sesión espera a READY=1: así ninguna sesión, ni sus
    programas de inicio automático, arranca antes de que la protección esté puesta."""
    import socket
    addr = os.environ.get("NOTIFY_SOCKET")
    if not addr:
        return False
    if addr.startswith("@"):
        addr = "\0" + addr[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as s:
            s.connect(addr)
            s.sendall(estado.encode())
        return True
    except OSError:
        return False


def cmd_protect(cfg: Config, args) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    from . import behavior
    guard = behavior.RansomwareGuard()
    rtp = RealTimeProtection(Scanner(cfg), Quarantine(cfg.quarantine_dir), cfg.auto_quarantine, guard=guard)
    rtp.start(args.paths or cfg.watch_paths)
    if os.access(behavior.AUDIT_LOG, os.R_OK):
        threading.Thread(target=behavior.watch_audit, args=(stop,), daemon=True).start()
        logging.getLogger("escudo").info("Vigilante de comportamiento activo (auditd)")
    else:
        logging.getLogger("escudo").warning("Sin acceso a auditd: el vigilante de comportamiento funciona "
                                            "solo con el detector de ransomware")
    # Procesos que corren desde memoria o con su programa borrado (malware «sin archivo»): cada 30 s
    threading.Thread(target=behavior.vigilar_procesos, args=(stop,), daemon=True, name="sin-archivo").start()
    gate = None
    if not args.no_detonar and sys.platform.startswith("linux"):
        from .primera_ejecucion import Gate
        try:
            gate = Gate(quarantine=rtp.quarantine, scanner=rtp.scanner)
            gate.start()
        except OSError as e:
            logging.getLogger("escudo").warning("Puerta de primera ejecución desactivada: %s", e)
            gate = None
    # Consulta «¿puedo abrir este archivo?» para lo que el kernel no ejecuta (programas de Windows con Wine): usa el
    # mismo análisis que la puerta (firmas, YARA, ML y detonación) aunque fanotify no esté disponible
    consulta = None
    if not args.no_detonar and sys.platform.startswith("linux"):
        from .consulta import Consulta
        from .primera_ejecucion import Gate as _Gate
        try:
            consulta = Consulta(gate or _Gate(quarantine=rtp.quarantine, scanner=rtp.scanner))
            consulta.start()
        except OSError as e:
            logging.getLogger("escudo").warning("Consulta de archivos desactivada: %s", e)
            consulta = None
    # Teclados falsos (USB que se hacen pasar por teclado para escribir órdenes): se detectan por cómo escriben
    teclados = None
    try:
        from . import teclados as T

        def avisar_teclado(nombre, ident, motivo):
            behavior.emit(behavior.Alert("teclado_falso", "critical", f"«{nombre}» se presentó como teclado y {motivo}. Lo "
                                "bloqueamos antes de que pudiera hacer algo.", None, ident,
                                "Si era tuyo (por ejemplo, un lector de códigos de barras), pulsa «Era mío».",
                                boton="permitir_teclado"))
        teclados = T.Guardia(avisar_teclado)
        teclados.start()
    except (ImportError, OSError) as e:
        logging.getLogger("escudo").warning("Protección de teclados falsos desactivada: %s", e)
        teclados = None
    guard = None
    if not args.no_network:
        guard = NetGuard(cfg)
        guard.start()
    activas = ["antivirus en tiempo real"] + (["puerta de primera ejecución"] if gate else []) + \
        (["consulta de programas de Windows"] if consulta else []) + \
        (["teclados falsos"] if teclados else []) + \
        (["IPS de red"] if guard else [])
    sd_notify("READY=1\nSTATUS=Protección activa: " + ", ".join(activas))
    stop.wait()
    sd_notify("STOPPING=1")
    rtp.stop()
    if consulta:
        consulta.stop()
    if gate:
        gate.stop()
    if guard:
        guard.stop()
    return 0


def cmd_detonar(cfg: Config, args) -> int:
    from . import detonar
    inf = detonar.detonar(args.path, args.args, segundos=args.segundos,
                          usar_vm=True if args.vm else False if args.sandbox else None)
    if args.json:
        print(json.dumps(inf.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(f"{args.path}: {inf.veredicto.upper()} ({inf.entorno}, {inf.segundos:.0f} s)")
        for m in inf.motivos:
            print(f"  - {m}")
        if inf.conexiones:
            print(f"  Conexiones intentadas: {', '.join(inf.conexiones[:10])}")
        if inf.nota:
            print(f"  Nota: {inf.nota}")
    return {"limpio": 0, "sospechoso": 1, "malicioso": 3}[inf.veredicto]


def cmd_permitir(cfg: Config, args) -> int:
    if os.geteuid() != 0:
        print("Necesita permisos de administrador: sudo escudo permitir <archivo>", file=sys.stderr)
        return 2
    from .primera_ejecucion import permitir
    print(f"Permitido: {args.path} (SHA-256 {permitir(args.path)[:16]}…)")
    return 0


def cmd_teclado(cfg: Config, args) -> int:
    """El usuario dice que un teclado bloqueado era suyo (p. ej. un lector de códigos de barras)."""
    import subprocess
    from . import teclados as T
    if os.geteuid() != 0:
        print("Necesita permisos de administrador", file=sys.stderr)
        return 1
    T.Registro().permitir(args.ident)              # valida el formato
    vid_pid = ":".join(args.ident.split(":")[:2])
    r = subprocess.run(["usbguard", "list-devices", "--blocked"], capture_output=True, text=True)
    for ln in r.stdout.splitlines():
        m = re.match(r"^(\d+): block id " + re.escape(vid_pid) + r"\b", ln)
        if m:
            subprocess.run(["usbguard", "allow-device", m.group(1)], check=False)
    print("Permitido: desconéctalo y vuelve a conectarlo si no responde.")
    return 0


def cmd_quarantine(cfg: Config, args) -> int:
    q = Quarantine(Path(args.dir) if args.dir else cfg.quarantine_dir)
    if args.action == "list":
        for m in q.list():
            print(f"{m['id']}  {m['time']}  {m['threat']:<35} {m['original_path']}")
    elif args.action == "restore":
        print("Restaurado en", q.restore(args.id, args.dest))
    elif args.action == "delete":
        q.delete(args.id)
    elif args.action == "purge":
        print(f"{q.purge()} elementos eliminados")
    return 0


def cmd_firewall(cfg: Config, args) -> int:
    fw = Firewall(cfg)
    if args.action == "block":
        fw.block(args.ip, duration_s=args.duration, reason="manual")
    elif args.action == "unblock":
        fw.unblock(args.ip)
    elif args.action == "list":
        for ip, exp in fw.blocked.items():
            print(ip, "permanente" if not exp else f"expira {exp:.0f}")
    return 0


def cmd_signatures(cfg: Config, args) -> int:
    db = SignatureDB(cfg.signatures_file)
    if args.action in ("import", "add-hash", "add-pattern") and not (args.source if args.action == "import"
                                                                     else args.value):
        print("Falta " + ("--source <archivo o URL>" if args.action == "import" else "el valor (hash o patrón)"),
              file=sys.stderr)
        return 2
    if args.action == "import":
        print(f"{db.import_hash_list(args.source)} hashes nuevos")
    elif args.action == "add-hash":
        db.add_hash(args.value, args.name)
    elif args.action == "add-pattern":
        db.add_pattern(args.name, args.value, args.severity)
    elif args.action == "info":
        sc = Scanner(cfg)
        print(f"{len(db.hashes)} hashes, {len(db.patterns)} patrones, "
              f"YARA: {'sí' if sc.yara.available() else 'no'}, ML EMBER2024: {'sí' if sc.ml.available() else 'no'}")
        return 0
    elif args.action == "yara-actualizar":
        from .yara_engine import YaraEngine
        print(f"{YaraEngine(cfg.data_dir / 'yara').update()} archivos de reglas YARA instalados")
        return 0
    elif args.action == "modelos-descargar":
        from .ml import download_models
        print("Modelos verificados: " + ", ".join(download_models(cfg.data_dir / "modelos")))
        return 0
    db.save()
    return 0


def cmd_analyze(cfg: Config, args) -> int:
    from . import analyze
    import json as _json
    r = analyze.report(args.path, Scanner(cfg))
    print(_json.dumps(r, indent=2, ensure_ascii=False) if args.json else analyze.summary(r))
    if args.explicar:
        print("\nExplicación (modelo local):\n" + analyze.explain(r, args.endpoint))
    return 0


def cmd_windows(cfg: Config, args) -> int:
    """Analiza (y con --limpiar, limpia) un Windows apagado: una carpeta ya montada, una partición o, sin
    argumentos, todas las particiones NTFS del equipo."""
    import stat as _stat
    import time as _time
    from . import windows as W

    def aviso(m):
        if not args.json:
            print(m, flush=True)

    objetivos = []           # (raiz, dispositivo, montado_por_nosotros)
    if args.objetivo and os.path.isdir(args.objetivo):
        objetivos.append((args.objetivo, None, False))
    elif args.objetivo:
        try:
            if not _stat.S_ISBLK(os.stat(args.objetivo).st_mode):
                raise ValueError(f"{args.objetivo}: no es una carpeta ni una partición")
        except FileNotFoundError:
            raise ValueError(f"{args.objetivo}: no existe")
        objetivos.append((None, args.objetivo, True))
    else:
        for p in W.particiones():
            if (p.get("fstype") or "").lower() == "bitlocker":
                aviso(f"{p['name']}: cifrada con BitLocker. Desbloquéala con «sudo cryptsetup bitlkOpen "
                      f"{p['name']} win» (contraseña o clave de recuperación) y analiza /dev/mapper/win")
            elif p.get("mountpoint"):
                objetivos.append((p["mountpoint"], p["name"], False))
            else:
                objetivos.append((None, p["name"], True))
        if not objetivos:
            aviso("No hay particiones NTFS (Windows) en este equipo.")
            return 0
    if any(nuestro for _r, _d, nuestro in objetivos) and os.geteuid() != 0:
        print("Montar el disco de Windows necesita permisos de administrador: sudo escudo windows …",
              file=sys.stderr)
        return 2
    scanner = Scanner(cfg)
    resultados, peor, errores = [], 0, 0
    for raiz, disp, nuestro in objetivos:
        montado = None
        try:
            if nuestro:
                raiz = montado = W.montar(disp)
            try:
                w = W.Windows(raiz)
            except FileNotFoundError:
                if args.objetivo:
                    raise
                continue                     # partición de datos, no de sistema
            aviso(f"\nAnalizando {w.producto} en {disp or raiz}…")
            a = W.Analisis(w, scanner, log=aviso if args.verbose else None)
            hall = a.ejecutar(completo=args.completo, sin_archivos=args.sin_archivos)
            r = {"windows": w.producto, "version": w.version, "raiz": raiz, "dispositivo": disp,
                 "avisos": w.avisos, "hallazgos": [W.asdict(h) for h in hall]}
            if not args.json:
                print(W.informe(w, hall, todo=args.todo))
            niveles = ("high", "medium") if args.tambien_medias else ("high",)
            limpiar = args.limpiar and any(h.gravedad in niveles for h in hall)
            if (limpiar or args.kit) and nuestro:      # volver a montar con escritura solo para escribir
                W.desmontar(raiz)
                montado = None
                raiz = montado = W.montar(disp, escritura=True)
                w = W.Windows(raiz)
                if limpiar:
                    hall = W.Analisis(w, scanner).ejecutar(completo=args.completo, sin_archivos=args.sin_archivos)
            elif (limpiar or args.kit) and not os.access(raiz, os.W_OK):
                raise OSError(f"{raiz} está montado en solo lectura: no se puede escribir en él")
            if limpiar:
                aviso("\nLimpiando…")
                hecho = W.limpiar(w, hall, niveles, log=aviso)
                r["limpieza"] = hecho
                destino = os.path.join(w.raiz, "TISMA-Cuarentena")
                os.makedirs(destino, exist_ok=True)
                with open(os.path.join(destino, f"informe-{_time.strftime('%Y%m%d-%H%M%S')}.txt"), "w",
                          encoding="utf-8-sig", newline="\r\n") as fh:
                    fh.write(W.informe(w, hall, todo=True) + "\n\nLimpieza:\n" +
                             "\n".join([*(f"cuarentena: {x['ruta']}" for x in hecho["archivos"]),
                                         *hecho["registro"], *(f"ERROR: {e}" for e in hecho["errores"])]))
                aviso(f"{len(hecho['archivos'])} archivos en cuarentena, {len(hecho['registro'])} cambios en el "
                      f"registro, {len(hecho['errores'])} errores.\nCopia del registro: {hecho['copias']}\n"
                      f"Informe y cuarentena en el disco de Windows: {w.unidad}\\TISMA-Cuarentena "
                      f"(restaurar: escudo quarantine restore <id> --dir {destino})")
                for e in hecho["errores"]:
                    aviso(f"  ERROR: {e}")
            elif args.limpiar:
                aviso("Nada que limpiar.")
            if args.kit:
                r["kit"] = W.instalar_kit(w)
                aviso(f"\nKit de endurecimiento copiado a {r['kit']}. Al arrancar Windows: clic derecho en "
                      f"{r['kit']}\\TISMA-Endurecer.bat > «Ejecutar como administrador» (lee antes LEEME.txt).")
            resultados.append(r)
            peor = max([peor] + [W.ORDEN[h.gravedad] for h in hall])
        except OSError as e:
            errores += 1
            print(f"{disp or raiz}: {e}", file=sys.stderr)
        finally:
            if montado:
                W.desmontar(montado)
    if args.json:
        print(json.dumps(resultados, ensure_ascii=False, indent=2))
    if not resultados and not errores:
        aviso("No se encontró ninguna instalación de Windows.")
    return 1 if peor >= W.ORDEN["medium"] else (2 if errores else 0)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="escudo", description="Antivirus e IPS de Centinela OS")
    p.add_argument("-c", "--config")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scan", help="Escanear archivos o directorios")
    s.add_argument("paths", nargs="+")
    s.add_argument("-q", "--quarantine", action="store_true", help="Poner amenazas en cuarentena")
    s.set_defaults(func=cmd_scan)

    s = sub.add_parser("protect", help="Protección en tiempo real + IPS de red")
    s.add_argument("paths", nargs="*")
    s.add_argument("--no-network", action="store_true")
    s.add_argument("--no-detonar", action="store_true", help="sin puerta de primera ejecución")
    s.set_defaults(func=cmd_protect)

    s = sub.add_parser("detonar", help="Ejecutar un programa en un entorno controlado y ver qué intenta hacer")
    s.add_argument("path")
    s.add_argument("args", nargs="*")
    s.add_argument("--segundos", type=int, default=45, choices=range(5, 601), metavar="5-600")
    s.add_argument("--vm", action="store_true", help="obligar a máquina virtual")
    s.add_argument("--sandbox", action="store_true", help="obligar a sandbox")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_detonar)

    s = sub.add_parser("permitir", help="Permitir un programa marcado como sospechoso (administrador)")
    s.add_argument("path")
    s.set_defaults(func=cmd_permitir)

    s = sub.add_parser("teclado", help="Teclados USB bloqueados por escribir como un robot")
    s.add_argument("accion", choices=["permitir"])
    s.add_argument("ident", help="vendor:producto[:serie], como lo muestra el aviso")
    s.set_defaults(func=cmd_teclado)

    s = sub.add_parser("quarantine", help="Gestionar la cuarentena")
    s.add_argument("action", choices=["list", "restore", "delete", "purge"])
    s.add_argument("id", nargs="?")
    s.add_argument("--dest")
    s.add_argument("--dir", help="otra cuarentena (p. ej. la de un disco de Windows: …/TISMA-Cuarentena)")
    s.set_defaults(func=cmd_quarantine)

    s = sub.add_parser("firewall", help="Bloquear/desbloquear IPs")
    s.add_argument("action", choices=["block", "unblock", "list"])
    s.add_argument("ip", nargs="?")
    s.add_argument("--duration", type=int, help="segundos (0 = permanente)")
    s.set_defaults(func=cmd_firewall)

    s = sub.add_parser("signatures", help="Gestionar firmas")
    s.add_argument("action", choices=["info", "import", "add-hash", "add-pattern", "yara-actualizar", "modelos-descargar"])
    s.add_argument("value", nargs="?", help="hash o regex")
    s.add_argument("--source", help="archivo o URL con hashes SHA-256")
    s.add_argument("--name", default="Malware.Custom")
    s.add_argument("--severity", default="medium", choices=["low", "medium", "high"])
    s.set_defaults(func=cmd_signatures)
    s = sub.add_parser("windows", help="Analizar y limpiar un Windows apagado (XP, Vista, 7, 8…) desde TISMA")
    s.add_argument("objetivo", nargs="?", help="carpeta montada o partición (/dev/sda2); sin nada: todas")
    s.add_argument("--limpiar", action="store_true", help="desactivar lo grave (cuarentena + registro)")
    s.add_argument("--tambien-medias", action="store_true", help="con --limpiar: también lo sospechoso")
    s.add_argument("--completo", action="store_true", help="escanear el disco entero (más lento)")
    s.add_argument("--sin-archivos", action="store_true", help="solo inicio automático y configuración")
    s.add_argument("--todo", action="store_true", help="listar también las entradas de inicio normales")
    s.add_argument("--kit", action="store_true", help="copiar el kit de endurecimiento a C:\\TISMA-Kit")
    s.add_argument("--json", action="store_true")
    s.set_defaults(func=cmd_windows)

    s = sub.add_parser("analizar", help="Informe de un archivo sospechoso (YARA, ML, capa)")
    s.add_argument("path")
    s.add_argument("--json", action="store_true")
    s.add_argument("--explicar", action="store_true", help="explicación con el modelo local")
    s.add_argument("--endpoint", default="http://127.0.0.1:8080/v1")
    s.set_defaults(func=cmd_analyze)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = Config.load(args.config)
    _setup_logging(cfg, args.verbose)
    try:
        return args.func(cfg, args)
    except (ValueError, FileNotFoundError, FileExistsError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
