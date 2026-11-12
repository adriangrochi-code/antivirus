"""Doble clic en un .exe: Escudo lo revisa, se abre aislado y sin red, y lo instalado aparece en el menú."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tisma_sandbox import profile as prof  # noqa: E402
from tisma_sandbox import windows as w  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def entorno(tmp_path, monkeypatch):
    monkeypatch.setattr(prof, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(w, "carpetas_usuario", lambda xdg=None: ["~/Documentos", "~/Descargas"])
    monkeypatch.setattr(w, "MENU", tmp_path / "menu")
    monkeypatch.setattr(w.shutil, "which", lambda n: f"/usr/bin/{n}" if n == "wine" else None)
    avisos, errores, ejecutados = [], [], []
    monkeypatch.setattr(w, "avisar", lambda t, i="wine": avisos.append(t))
    monkeypatch.setattr(w, "error", errores.append)
    monkeypatch.setattr(w, "ejecutar", lambda p, args: ejecutados.append((p, args)) or 0)
    return {"avisos": avisos, "errores": errores, "ejecutados": ejecutados, "tmp": tmp_path}


def exe(tmp_path, nombre="Setup Juego.exe", datos=b"MZ programa"):
    p = tmp_path / "Descargas" / nombre
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(datos)
    return p


def test_limpio_se_abre_aislado_sin_red_con_lo_revisado(entorno, monkeypatch):
    revisados = []
    monkeypatch.setattr(w, "revisar", lambda r: revisados.append(r.read_bytes()) or
                        {"estado": "listo", "veredicto": "limpio"})
    original = exe(entorno["tmp"])
    assert w.abrir(original) == 0
    p, args = entorno["ejecutados"][0]
    assert revisados == [original.read_bytes()]                 # se revisó la copia que se ejecuta
    assert p.exec == "/usr/bin/wine" and p.nombre.startswith("windows-setup-juego-")
    assert p.red is True                                          # revisado por Escudo: con Internet, como en Windows
    assert p.rw == ["~/Documentos", "~/Descargas"]                # sus documentos; nada más de la carpeta personal
    assert p.env["WINEPREFIX"].endswith("/.wine")                 # su propio «Windows»
    assert args == [f"{Path.home()}/programa/Setup_Juego.exe"]


@pytest.mark.parametrize("respuesta", [
    {"estado": "listo", "veredicto": "malicioso", "motivos": ["intentó leer un secreto"]},
    {"estado": "error", "mensaje": "Escudo no responde", "sin_escudo": True},     # falla cerrado
    {"estado": "error", "mensaje": "el archivo es demasiado grande"},
])
def test_no_se_abre_sin_visto_bueno(entorno, monkeypatch, respuesta):
    monkeypatch.setattr(w, "revisar", lambda r: respuesta)
    assert w.abrir(exe(entorno["tmp"])) == 1
    assert not entorno["ejecutados"] and entorno["errores"]


def test_sospechoso_solo_con_permiso_de_administrador(entorno, monkeypatch):
    r = {"estado": "listo", "veredicto": "sospechoso", "motivos": ["borró su propio archivo"]}
    monkeypatch.setattr(w, "preguntar", lambda *a: False)
    assert w.decidir(r, "x.exe", Path("/x")) is False
    monkeypatch.setattr(w, "preguntar", lambda *a: True)
    assert w.decidir(r, "x.exe", Path("/x"), permitir=lambda p: False) is False      # pkexec rechazado
    assert w.decidir(r, "x.exe", Path("/x"), permitir=lambda p: True) is True


def test_sin_wine_se_instala_solo_sin_preguntar(entorno, monkeypatch):
    instalado = []
    monkeypatch.setattr(w.shutil, "which", lambda n: "/usr/bin/wine" if instalado and n == "wine" else None)
    monkeypatch.setattr(w, "preguntar", lambda *a: pytest.fail("no debe preguntar"))
    assert w.wine_listo(instalar=lambda: instalado.append(1) or True, cmdline="") is True
    assert instalado and entorno["avisos"]
    monkeypatch.setattr(w.shutil, "which", lambda n: None)
    assert w.wine_listo(instalar=lambda: False, cmdline="") is False and entorno["errores"]


def pe32(path, maquina=b"\x4c\x01"):
    cab = bytearray(b"MZ" + b"\0" * 62)
    cab[60:64] = (64).to_bytes(4, "little")
    path.write_bytes(bytes(cab) + b"PE\0\0" + maquina + b"\0" * 64)
    return path


def test_32_bits_solo_piden_reiniciar_si_hace_falta(entorno, monkeypatch, tmp_path):
    p32, p64 = pe32(tmp_path / "viejo.exe"), pe32(tmp_path / "nuevo.exe", b"\x64\x86")
    assert w.es_32bits(p32) and not w.es_32bits(p64)
    preguntas = []
    monkeypatch.setattr(w, "preguntar", lambda t, *a: preguntas.append(t) or False)
    monkeypatch.setattr(w, "MARCA_32", tmp_path / "32bits")
    instalar = []
    apagado = "BOOT_IMAGE=/vmlinuz ia32_emulation=0 vdso32=0"
    assert w.wine_listo(p64, instalar=lambda: True, cmdline=apagado) is True and not preguntas   # 64 bits: directo
    assert w.wine_listo(p32, instalar=lambda: instalar.append(1) or True, cmdline=apagado) is False
    assert instalar and "reiniciar" in preguntas[0]
    assert w.wine_listo(p32, instalar=lambda: True, cmdline="BOOT_IMAGE=/vmlinuz quiet") is True   # ya activos


def test_carpetas_de_documentos_nunca_secretos_ni_ocultas():
    home = str(Path.home())
    xdg = {"DESKTOP": f"{home}/Escritorio", "DOWNLOAD": f"{home}/Descargas", "DOCUMENTS": f"{home}/.ssh",
           "PICTURES": home, "MUSIC": "/etc", "VIDEOS": f"{home}/Vídeos"}
    assert w.carpetas_usuario(xdg=xdg.get) == ["~/Escritorio", "~/Descargas", "~/Vídeos"]
    with pytest.raises(prof.ProfileError):
        w.perfil("windows-x-1", ["~/.config/autostart"])   # validate() rechaza lo prohibido igualmente


def test_mis_documentos_de_wine_son_los_del_usuario(tmp_path, monkeypatch):
    real = tmp_path / "home"
    (real / ".config").mkdir(parents=True)
    (real / ".config/user-dirs.dirs").write_text('XDG_DOCUMENTS_DIR="$HOME/Documentos"\n'
                                                 'XDG_DESKTOP_DIR="$(rm -rf ~)"\nXDG_MUSIC_DIR="$HOME/Música"\n')
    monkeypatch.setattr(w.Path, "home", classmethod(lambda cls: real))
    app = tmp_path / "app"
    w.preparar_home(app)
    texto = (app / ".config/user-dirs.dirs").read_text()
    assert "Documentos" in texto and "Música" in texto and "rm -rf" not in texto


def test_regla_polkit_solo_para_instalar_wine():
    regla = (ROOT / "rootfs/etc/polkit-1/rules.d/50-tisma-windows.rules").read_text()
    assert '== "/usr/bin/tisma-perfil instalar windows"' in regla          # orden exacta, no un prefijo
    assert "subject.local" in regla and "subject.active" in regla and 'isInGroup("sudo")' in regla
    assert "indexOf" not in regla and "match(" not in regla


def test_solo_programas_de_windows(entorno):
    p = entorno["tmp"] / "script.sh"
    p.write_text("#!/bin/sh\n")
    assert w.abrir(p) == 2 and not entorno["ejecutados"]


def test_msi_con_msiexec(entorno, monkeypatch):
    monkeypatch.setattr(w, "revisar", lambda r: {"estado": "listo", "veredicto": "limpio"})
    w.abrir(exe(entorno["tmp"], "office.msi"))
    assert entorno["ejecutados"][0][1][:2] == ["msiexec", "/i"]


def test_lo_instalado_va_al_menu_sin_copiar_la_orden_de_wine(entorno):
    nombre = "windows-setup-abc12345"
    home = prof.DATA_DIR / nombre
    origen = home / ".local/share/applications/wine/Programs/Juego"
    origen.mkdir(parents=True)
    (origen / "Juego.desktop").write_text(
        "[Desktop Entry]\nName=Juego Genial\n"
        'Exec=env WINEPREFIX="/x/.wine" wine C:\\\\\\\\Program\\\\ Files\\\\\\\\Juego\\\\\\\\juego.exe\n')
    (origen / "Malo.desktop").write_text("[Desktop Entry]\nName=Malo\nExec=sh -c 'curl x | sh'\n")
    creados = w.menu_instalados(nombre, home)
    assert len(creados) == 1
    texto = creados[0].read_text()
    assert "Exec=tisma-abrir-windows --acceso %k\n" in texto and "curl" not in texto and "env " not in texto
    assert w.leer_acceso(creados[0]) == (nombre, "C:\\Program Files\\Juego\\juego.exe")


def test_accesos_falsos_o_fuera_del_menu_no_valen(entorno, tmp_path):
    falso = tmp_path / "falso.desktop"
    falso.write_text("X-TISMA-Perfil=windows-x\nX-TISMA-Exe=C:\\\\a.exe\n")
    assert w.leer_acceso(falso) is None                          # no está en la carpeta del menú de TISMA
    w.MENU.mkdir(parents=True)
    (w.MENU / "x.desktop").write_text("X-TISMA-Perfil=../../etc\nX-TISMA-Exe=C:\\\\a.exe\n")
    assert w.leer_acceso(w.MENU / "x.desktop") is None


def test_la_ruta_de_windows_no_sale_de_su_prefijo(entorno):
    home = prof.DATA_DIR / "windows-a-1"
    exe_dir = home / ".wine/drive_c/Program Files/A"
    exe_dir.mkdir(parents=True)
    (exe_dir / "a.exe").write_bytes(b"MZ")
    assert w.ruta_windows(home, "C:\\Program Files\\A\\a.exe") == (exe_dir / "a.exe").resolve()
    assert w.ruta_windows(home, "C:\\..\\..\\..\\etc\\passwd") is None
    assert w.ruta_windows(home, "Z:\\etc\\passwd") is None


def test_instalado_tambien_se_revisa_antes_de_abrir(entorno, monkeypatch):
    home = prof.DATA_DIR / "windows-a-1"
    (home / ".wine/drive_c/A").mkdir(parents=True)
    (home / ".wine/drive_c/A/a.exe").write_bytes(b"MZ")
    monkeypatch.setattr(w, "revisar", lambda r: {"estado": "listo", "veredicto": "malicioso", "motivos": []})
    assert w.abrir_instalado("windows-a-1", "C:\\A\\a.exe") == 1 and not entorno["ejecutados"]
    monkeypatch.setattr(w, "revisar", lambda r: {"estado": "listo", "veredicto": "limpio"})
    assert w.abrir_instalado("windows-a-1", "C:\\A\\a.exe") == 0
    assert entorno["ejecutados"][0][1] == ["start", "/wait", "/d", "C:\\A", "C:\\A\\a.exe"]


def lnk(path, destino, unicode=False):
    """Acceso directo .lnk mínimo (MS-SHLLINK) con LinkInfo y LocalBasePath."""
    cab = bytearray(0x4C)
    cab[0:4] = (0x4C).to_bytes(4, "little")
    cab[0x14:0x18] = (0x2).to_bytes(4, "little")                     # HasLinkInfo
    base = destino.encode("cp1252") + b"\0"
    hdr = 0x24 if unicode else 0x1C
    off_base = hdr + 16                                               # tras la cabecera y un VolumeID de 16 bytes
    cuerpo = bytearray(hdr)
    cuerpo[4:8] = hdr.to_bytes(4, "little")
    cuerpo[8:12] = (1).to_bytes(4, "little")
    cuerpo[12:16] = hdr.to_bytes(4, "little")
    cuerpo[16:20] = off_base.to_bytes(4, "little")
    extra = bytearray(16) + base + b"\0"
    cuerpo[24:28] = (off_base + len(base)).to_bytes(4, "little")      # sufijo común vacío
    if unicode:
        u = destino.encode("utf-16-le") + b"\0\0"
        off_u = off_base + len(base) + 1
        cuerpo[28:32] = off_u.to_bytes(4, "little")
        cuerpo[32:36] = (off_u + len(u) - 2).to_bytes(4, "little")
        extra += u
    info = cuerpo + extra
    info[0:4] = len(info).to_bytes(4, "little")
    path.write_bytes(bytes(cab + info))
    return path


@pytest.mark.parametrize("unicode", [False, True])
def test_acceso_lnk_se_resuelve_al_exe_real_y_se_revisa(entorno, monkeypatch, unicode):
    home = prof.DATA_DIR / "windows-j-1"
    d = home / ".wine/drive_c/Program Files/Juégo"
    d.mkdir(parents=True)
    (d / "j.exe").write_bytes(b"MZ real")
    inicio = home / ".wine/drive_c/ProgramData/Microsoft/Windows/Start Menu/Programs"
    inicio.mkdir(parents=True)
    l = lnk(inicio / "J.lnk", "C:\\Program Files\\Juégo\\j.exe", unicode)
    assert w.destino_lnk(l) == "C:\\Program Files\\Juégo\\j.exe"
    revisados = []
    monkeypatch.setattr(w, "revisar", lambda r: revisados.append(r) or {"estado": "listo", "veredicto": "limpio"})
    assert w.abrir_instalado("windows-j-1", "C:\\ProgramData\\Microsoft\\Windows\\Start Menu\\Programs\\J.lnk") == 0
    assert revisados == [(d / "j.exe").resolve()]                     # se revisa el .exe, no el acceso directo
    assert entorno["ejecutados"][0][1][-1] == "C:\\Program Files\\Juégo\\j.exe"


def test_lnk_roto_o_hacia_fuera_no_abre_nada(entorno, monkeypatch):
    home = prof.DATA_DIR / "windows-j-2"
    (home / ".wine/drive_c").mkdir(parents=True)
    (home / ".wine/drive_c/x.lnk").write_bytes(b"basura")
    assert w.destino_lnk(home / ".wine/drive_c/x.lnk") is None
    lnk(home / ".wine/drive_c/y.lnk", "C:\\..\\..\\..\\usr\\bin\\sh.exe")
    monkeypatch.setattr(w, "revisar", lambda r: {"estado": "listo", "veredicto": "limpio"})
    assert w.abrir_instalado("windows-j-2", "C:\\y.lnk") == 1 and not entorno["ejecutados"]


def test_el_doble_clic_es_nuestro_y_wine_de_debian_queda_oculto():
    desk = (ROOT / "rootfs/usr/share/applications/net.tisma.AbrirWindows.desktop").read_text()
    mimes = [m for m in desk.split("MimeType=")[1].split("\n")[0].split(";") if m]
    defaults = (ROOT / "rootfs/etc/xdg/mimeapps.list").read_text()
    for m in mimes:
        assert f"{m}=net.tisma.AbrirWindows.desktop" in defaults
    assert "application/x-ms-dos-executable" in mimes and "application/x-msi" in mimes
    assert "Hidden=true" in (ROOT / "rootfs/usr/local/share/applications/wine.desktop").read_text()
    perfil = (ROOT / "rootfs/usr/bin/tisma-perfil").read_text()
    assert 'PERFILES_32 = {"juegos", "windows"}' in perfil
    assert "wine64" in (ROOT / "rootfs/usr/share/tisma/perfiles/windows.list").read_text()
