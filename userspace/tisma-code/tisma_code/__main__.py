import sys

if sys.platform == "win32" and not sys.flags.utf8_mode:
    # Los archivos de la app y de los proyectos son UTF-8: sin el modo UTF-8, Windows los leería con la página de
    # códigos local. Se vuelve a lanzar con -X utf8 (los lanzadores del ZIP ya lo pasan y no llegan aquí).
    import signal
    import subprocess
    signal.signal(signal.SIGINT, signal.SIG_IGN)      # Ctrl+C lo atiende el hijo (apaga el motor en orden)
    sys.exit(subprocess.call([sys.executable, "-X", "utf8", "-m", "tisma_code", *sys.argv[1:]]))

if sys.argv[1:2] == ["motor"]:
    from .motor import cli
    sys.exit(cli(sys.argv[2:]))

from .server import main

sys.exit(main())
