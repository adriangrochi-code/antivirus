"""En Windows solo se recogen las pruebas que no dependen de Linux. El resto prueba bubblewrap, /proc, fcntl,
pkexec, rutas POSIX o herramientas de TISMA OS (REA, Craft, SolidCraft, Hope…), que en Windows no existen o están
desactivadas a propósito; corren en la CI de Linux. La lista de lo que se deja fuera sale en la cabecera de pytest."""
import sys
from pathlib import Path

EN_WINDOWS = {"test_plataforma.py", "test_windows.py", "test_construir.py", "test_motor.py"}
MOTIVO = "depende de Linux (bubblewrap, /proc, fcntl, pkexec, rutas POSIX o herramientas de TISMA OS)"


def _fuera(path: Path) -> bool:
    return sys.platform == "win32" and path.name.startswith("test_") and path.suffix == ".py" \
        and path.name not in EN_WINDOWS


def pytest_ignore_collect(collection_path, config):
    return True if _fuera(Path(collection_path)) else None


def pytest_report_header(config):
    if sys.platform != "win32":
        return None
    fuera = sorted(p.name for p in Path(__file__).parent.glob("test_*.py") if _fuera(p))
    return [f"SOLO LINUX (no se recogen en este sistema: {MOTIVO}):"] + [f"  - {n}" for n in fuera]
