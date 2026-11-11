"""Interfaz de línea de comandos de Escudo."""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading

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
                print(f"  -> cuarentena {q.add(d.path, d.name)}: {d.path}")
    return 1 if total else 0


def cmd_protect(cfg: Config, args) -> int:
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    rtp = RealTimeProtection(Scanner(cfg), Quarantine(cfg.quarantine_dir), cfg.auto_quarantine)
    rtp.start(args.paths or cfg.watch_paths)
    guard = None
    if not args.no_network:
        guard = NetGuard(cfg)
        guard.start()
    stop.wait()
    rtp.stop()
    if guard:
        guard.stop()
    return 0


def cmd_quarantine(cfg: Config, args) -> int:
    q = Quarantine(cfg.quarantine_dir)
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
    s.set_defaults(func=cmd_protect)

    s = sub.add_parser("quarantine", help="Gestionar la cuarentena")
    s.add_argument("action", choices=["list", "restore", "delete", "purge"])
    s.add_argument("id", nargs="?")
    s.add_argument("--dest")
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
