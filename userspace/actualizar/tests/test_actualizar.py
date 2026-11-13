"""Actualizar desde el USB: cuentas unidas sin romper a nadie, qué se conserva y qué se reinstala."""
import io
import json
import os
import stat
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "userspace/actualizar"))
from tisma_actualizar import motor, usuarios  # noqa: E402

VIEJO_PASSWD = """root:x:0:0:root:/root:/bin/bash
sddm:x:105:110::/var/lib/sddm:/bin/false
helios:x:1000:1000:Helios:/home/helios:/bin/bash
"""
NUEVO_PASSWD = """root:x:0:0:root:/root:/bin/bash
sddm:x:107:112::/var/lib/sddm:/bin/false
polkitd:x:105:111::/nonexistent:/usr/sbin/nologin
"""
VIEJO_GROUP = "root:x:0:\nsudo:x:27:helios\nsddm:x:110:\nhelios:x:1000:\n"
NUEVO_GROUP = "root:x:0:\nsudo:x:27:\nsddm:x:112:\npolkitd:x:111:\nnuevo:x:110:\n"


def test_unir_cuentas_conserva_personas_y_anade_sistema():
    p, mu = usuarios.unir(usuarios.leer(VIEJO_PASSWD), usuarios.leer(NUEVO_PASSWD))
    g, mg = usuarios.unir(usuarios.leer(VIEJO_GROUP), usuarios.leer(NUEVO_GROUP), grupos=True)
    nombres = {f[0]: f for f in p}
    assert nombres["helios"][2] == "1000" and nombres["sddm"][2] == "105"      # lo del instalado no cambia
    assert nombres["polkitd"][2] not in ("0", "105", "1000")                   # 105 estaba ocupado
    assert mu[107] == 105 and mu[105] == int(nombres["polkitd"][2])
    grupos = {f[0]: f for f in g}
    assert grupos["sudo"][3] == "helios"                                       # miembros conservados
    assert grupos["nuevo"][2] != "110" and mg[110] == int(grupos["nuevo"][2])  # 110 era sddm en el instalado
    p = usuarios.gid_principal(p, mg, {"polkitd"})
    nombres = {f[0]: f for f in p}
    assert nombres["polkitd"][3] == "111"
    assert nombres["sddm"][3] == "110"                                         # cuenta vieja: no se traduce


def test_sombra_conserva_contrasenas():
    viejo = usuarios.leer("helios:$y$hash:1::::::\n")
    nuevo = usuarios.leer("helios:*:1::::::\npolkitd:!:1::::::\n")
    s = usuarios.unir_sombra(viejo, nuevo, {"helios", "polkitd"})
    assert s[0][1] == "$y$hash" and [f[0] for f in s] == ["helios", "polkitd"]


def test_renumerar_conserva_setuid(tmp_path):
    f = tmp_path / "ping"
    f.write_text("x")
    os.chmod(f, 0o4755)
    st = os.stat(f)
    if os.geteuid() != 0:
        return
    n = usuarios.renumerar(str(tmp_path), {st.st_uid: 4242}, {})
    st2 = os.stat(f)
    assert n == 1 and st2.st_uid == 4242 and st2.st_mode & stat.S_ISUID


def test_candidatos_ignora_el_usb():
    lsblk = {"blockdevices": [
        {"name": "sda", "model": "SSD", "size": "238G", "children": [
            {"name": "sda1", "partlabel": None, "parttype": "C12A7328-F81F-11D2-BA4B-00A0C93EC93B", "fstype": "vfat"},
            {"name": "sda2", "partlabel": "TISMA-arranque", "fstype": "ext4"},
            {"name": "sda3", "partlabel": "TISMA", "fstype": "crypto_LUKS"}]},
        {"name": "sdb", "children": [{"name": "sdb1", "partlabel": "TISMA"}, {"name": "sdb2", "partlabel": "TISMA-arranque"}]},
        {"name": "nvme0n1", "children": [{"name": "nvme0n1p1", "partlabel": "Windows"}]}]}
    c = motor.candidatos(lsblk, "sdb")
    assert len(c) == 1 and c[0] == {"disco": "/dev/sda", "raiz": "/dev/sda3", "arranque": "/dev/sda2",
                                    "esp": "/dev/sda1", "cifrado": True, "modelo": "SSD", "tamano": "238G"}


def test_reinstalar_solo_lo_manual_que_falta(tmp_path):
    viejo, nuevo = tmp_path / "v", tmp_path / "n"
    for r in (viejo, nuevo):
        (r / "var/lib/dpkg").mkdir(parents=True)
        (r / "var/lib/apt").mkdir(parents=True)
    bloque = lambda p, a="amd64": f"Package: {p}\nStatus: install ok installed\nArchitecture: {a}\n"
    (viejo / "var/lib/dpkg/status").write_text("\n".join([bloque("krita"), bloque("libfoo"), bloque("dolphin"),
                                                          bloque("wine32", "i386"), bloque("live-boot"),
                                                          bloque("linux-image-7.2.12")]))
    (viejo / "var/lib/apt/extended_states").write_text("Package: libfoo\nArchitecture: amd64\nAuto-Installed: 1\n")
    (nuevo / "var/lib/dpkg/status").write_text(bloque("dolphin"))
    assert motor.a_reinstalar(str(viejo), str(nuevo)) == ["krita", "wine32:i386"]


def test_conserva_lo_del_equipo(tmp_path):
    etc = tmp_path / "etc"
    (etc / "NetworkManager/system-connections").mkdir(parents=True)
    (etc / "ssh").mkdir()
    for f in ("fstab", "crypttab", "hostname", "ssh/ssh_host_ed25519_key", "ssh/sshd_config", "passwd"):
        (etc / f).write_text("x")
    r = motor.rutas_etc(str(tmp_path))
    assert {"fstab", "crypttab", "hostname", "NetworkManager/system-connections", "ssh/ssh_host_ed25519_key"} <= set(r)
    assert "ssh/sshd_config" not in r and "passwd" not in r       # las cuentas se UNEN aparte; la config, nueva


def test_puntos_fstab():
    f = ("UUID=1 / btrfs subvol=/@ 0 0\nUUID=1 /home btrfs subvol=/@home 0 0\nUUID=1 /.snapshots btrfs x 0 0\n"
         "/swap/swapfile none swap defaults 0 0\n/dev/mapper/x none swap sw 0 0\n# comentario\n")
    assert motor.puntos_fstab(f) == [("/", "btrfs"), ("/home", "btrfs"), ("/.snapshots", "btrfs"),
                                     ("/swap/swapfile", "swap")]


def test_aviso_es_json():
    s = io.StringIO()
    motor.aviso("copiar", "Copiando…", 30, s)
    assert json.loads(s.getvalue()) == {"paso": "copiar", "texto": "Copiando…", "pct": 30}


def test_ventana_solo_acciones_fijas(monkeypatch):
    from tisma_actualizar import ventana
    monkeypatch.setattr(ventana.motor, "buscar", lambda: [{"raiz": "/dev/sda3"}])
    llamadas = []
    monkeypatch.setattr(ventana, "POPEN", lambda *a, **k: llamadas.append(a) or (_ for _ in ()).throw(AssertionError))
    assert not ventana.lanzar("rm", "/dev/sda3", "x")["ok"]
    assert not ventana.lanzar("actualizar", "/dev/sdb1", "x")["ok"]          # disco que no es un TISMA
    assert not ventana.lanzar("actualizar", "/dev/sda3", "a\nb")["ok"]
    assert llamadas == []


def test_piezas_del_sistema():
    assert "tisma-actualizar-usb" in (ROOT / "rootfs/usr/lib/tisma/instalador-auto").read_text()
    regla = (ROOT / "rootfs/usr/share/tisma/live/49-tisma-live-instalador.rules").read_text()
    assert "org.tisma.actualizar-instalado" in regla
    pol = (ROOT / "rootfs/usr/share/polkit-1/actions/org.tisma.actualizar.policy").read_text()
    assert "/usr/lib/tisma/actualizar-instalado" in pol
    assert "tisma_actualizar" in (ROOT / "iso/build-iso.sh").read_text()
    assert set(motor.PAQUETES_LIVE) >= {"live-boot", "calamares-settings-debian"}


def test_usb_deja_elegir_con_tisma_instalado():
    """Antes, con TISMA instalado, el USB arrancaba el sistema del disco sin avisar: no se podía actualizar."""
    b = (ROOT / "iso/build-iso.sh").read_text()
    # El menú se ve (no una línea chica que pasaba desapercibida) y «Actualizar» va primero
    bloque = b[b.index("--file /tisma-instalado"):]
    assert "set timeout_style=menu" in bloque and "set timeout=10" in bloque and "set default=disco" in bloque
    assert bloque.index("Actualizar o reinstalar TISMA") < bloque.index("Arrancar el TISMA instalado")
    tema = (ROOT / "rootfs/boot/grub/themes/centinela/theme.txt").read_text().splitlines()
    assert not any(l.startswith("terminal-box") for l in tema)        # sin recuadro negro de consola
    assert "vt.global_cursor_default=0" in (ROOT / "rootfs/etc/default/grub.d/50-centinela.cfg").read_text()


def test_nombre_del_disco_como_el_instalado():
    """Con otro nombre, el arranque rehecho no pedía la contraseña del disco y se reiniciaba en bucle."""
    ct = "# x\nluks-1234abcd UUID=1234abcd-0000 none luks,discard\n"
    assert motor.nombre_crypttab(ct, "1234abcd-0000", "/dev/sda3") == "luks-1234abcd"
    assert motor.nombre_crypttab(ct, "otro", "/dev/sda3") is None
    assert motor.nombre_crypttab("raiz /dev/sda3 none luks\n", "", "/dev/sda3") == "raiz"
    assert motor.nombre_crypttab("$(rm) UUID=u none\n", "u", "/dev/x") is None


def test_montados_bajo():
    m = ("/dev/x /tmp/w/top btrfs rw 0 0\n/dev/x /tmp/w/raiz btrfs rw 0 0\nproc /tmp/w/raiz/proc proc rw 0 0\n"
         "/dev/y /tmp/wotro ext4 rw 0 0\n/dev/z /tmp/w/con\\040espacio ext4 rw 0 0\n")
    r = motor.montados_bajo("/tmp/w", m)
    assert r[0] == "/tmp/w/raiz/proc" and set(r) == {"/tmp/w/top", "/tmp/w/raiz", "/tmp/w/raiz/proc",
                                                      "/tmp/w/con espacio"}


def test_nunca_borra_lo_montado(tmp_path, monkeypatch):
    """Si algo sigue montado en la carpeta de trabajo, no se borra NADA (antes se vació el sistema nuevo)."""
    a = motor.Actualizacion({"cifrado": False, "raiz": "/dev/x"}, salida=io.StringIO())
    sistema = os.path.join(a.w, "raiz", "usr")
    os.makedirs(sistema)
    open(os.path.join(sistema, "importante"), "w").write("x")
    monkeypatch.setattr(motor, "montados_bajo", lambda base, m=None: [os.path.join(a.w, "raiz")])
    monkeypatch.setattr(motor.subprocess, "run", lambda *x, **k: None)
    assert a.limpiar_trabajo() is False
    assert os.path.exists(os.path.join(sistema, "importante"))
    # Sin montajes: solo se quitan carpetas vacías, nunca archivos
    monkeypatch.setattr(motor, "montados_bajo", lambda base, m=None: [])
    a.limpiar_trabajo()
    assert os.path.exists(os.path.join(sistema, "importante"))
    os.unlink(os.path.join(sistema, "importante"))
    a.limpiar_trabajo()
    assert not os.path.exists(a.w)
