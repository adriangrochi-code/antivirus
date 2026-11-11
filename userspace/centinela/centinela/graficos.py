"""Compatibilidad gráfica: el mejor driver y la mejor capa de traducción para cada GPU, también las viejas.

Una GPU antigua no gana funciones que su hardware no tiene, pero sí puede rendir mucho más con el driver y la
capa de traducción adecuados:

* AMD GCN 1 y 2 (Radeon HD 7000, R7/R9 200; ~2012-2014): el kernel les pone el driver viejo «radeon», que NO
  tiene Vulkan. Con «amdgpu» tienen Vulkan (RADV): Steam/Proton (DXVK), juegos y apps modernas. Es el cambio
  que más rinde; se aplica con PRUEBA: si tras reiniciar no hay imagen, el siguiente arranque vuelve al driver
  anterior solo.
* GPU sin Vulkan por hardware (AMD TeraScale, Intel antiguas, NVIDIA anteriores a Kepler): los juegos de
  Windows van por WineD3D (DirectX → OpenGL) en vez de DXVK, que exige Vulkan; las apps que solo saben Vulkan
  arrancan con Lavapipe (Vulkan en la CPU: lento pero funciona).
* GPU con Vulkan por hardware: se oculta Lavapipe para que ningún programa acabe por error en la CPU.
* NVIDIA anteriores a Turing con el driver libre: funcionan (NVK desde Kepler) pero sin subir la frecuencia
  (el hardware no lo permite sin firmware firmado): se avisa en vez de prometer rendimiento.

Lo que se aplica: /etc/default/grub.d/61-centinela-graficos.cfg (driver del kernel, con prueba) y
/etc/environment.d/60-tisma-graficos.conf (capas de traducción para todas las sesiones).
"""

from __future__ import annotations

import json
import os
import re
import subprocess

from . import apply as ap
from . import sysfs

ENVD = "environment.d/60-tisma-graficos.conf"
STATE = "graficos.json"
SWITCH = ["radeon.si_support=0", "amdgpu.si_support=1", "radeon.cik_support=0", "amdgpu.cik_support=1"]
NV_CODE = re.compile(r"\bNVIDIA (NV\w+|G\d{2,3}\w*|GT\d{3}\w*|MCP\w+|GF\d{3}\w*|GK\d{3}\w*|GM\d{3}\w*|GP\d{3}\w*|"
                     r"GV\d{3}\w*|TU\d{3}\w*|GA\d{3}\w*|AD\d{3}\w*|GB\d{3}\w*)\b")


def sh(argv: list[str], timeout: int = 30) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


# ---------------------------------------------------------------- detección
def amdgpu_ids(release: str | None = None, path: str | None = None) -> set[str]:
    """IDs de PCI que amdgpu sabe manejar en ESTE kernel (incluye GCN 1/2 solo si se compiló con ese soporte)."""
    release = release or os.uname().release
    p = path or sysfs.path(f"/lib/modules/{release}/modules.alias")
    ids = set()
    try:
        for line in open(p, encoding="utf-8", errors="replace"):
            m = re.match(r"alias pci:v00001002d0000([0-9A-F]{4})\S* amdgpu$", line.strip())
            if m:
                ids.add(m.group(1).lower())
    except OSError:
        pass
    return ids


def vulkan_devices(run=sh) -> list[dict]:
    """Dispositivos Vulkan (vulkaninfo --summary): nombre, tipo y driver."""
    out = run(["vulkaninfo", "--summary"])
    devs, cur = [], None
    for line in out.splitlines():
        if re.match(r"^GPU\d+:\s*$", line.strip()):         # cada dispositivo empieza con «GPUn:»
            cur = {}
            devs.append(cur)
            continue
        m = re.match(r"\s*(deviceName|deviceType|driverName|driverID)\s*=\s*(.+)$", line)
        if m and cur is not None:
            cur[m.group(1)] = m.group(2).strip()
    return [d for d in devs if d]


def hardware_vulkan(devs: list[dict]) -> bool:
    return any(d.get("deviceType", "").endswith(("INTEGRATED_GPU", "DISCRETE_GPU", "VIRTUAL_GPU")) for d in devs)


def nvidia_chip(run=sh) -> str:
    m = NV_CODE.search(run(["journalctl", "-k", "-b", "--no-pager", "-g", "nouveau"]))
    return m.group(1) if m else ""


def nvidia_gen(chip: str) -> str:
    if chip.startswith(("TU", "GA", "AD", "GB")):
        return "turing+"
    if chip.startswith(("GK", "GM", "GP", "GV")):
        return "kepler-volta"
    return "antigua" if chip else ""


def classify(gpus: list[dict], amd_ids: set[str], vk: list[dict], nv_chip: str = "") -> list[dict]:
    """Para cada GPU: familia, si se puede pasar a amdgpu y qué hacer. Lógica pura (probada)."""
    out = []
    for g in gpus:
        dev = (g.get("device_id") or "").lower().removeprefix("0x")
        r = {"card": g.get("card"), "vendor": g.get("vendor"), "device_id": dev, "driver": g.get("driver"),
             "familia": "", "cambiar_a_amdgpu": False, "avisos": []}
        if g.get("vendor") == "amd":
            if g.get("driver") == "amdgpu":
                r["familia"] = "AMD GCN o posterior (amdgpu)"
            elif g.get("driver") == "radeon" and dev in amd_ids:
                r["familia"] = "AMD GCN 1/2 con el driver antiguo «radeon» (sin Vulkan)"
                r["cambiar_a_amdgpu"] = True
            elif g.get("driver") == "radeon":
                r["familia"] = "AMD TeraScale o anterior (solo OpenGL)"
            else:
                r["familia"] = "AMD sin driver"
        elif g.get("vendor") == "nvidia":
            gen = nvidia_gen(nv_chip) if g.get("driver") == "nouveau" else ""
            r["familia"] = f"NVIDIA {nv_chip or ''} ({g.get('driver') or 'sin driver'})".replace("  ", " ")
            if gen == "kepler-volta":
                r["avisos"].append("Driver libre sin subida de frecuencia en esta generación (el hardware exige "
                                   "firmware firmado): funciona, Vulkan incluido (NVK), pero a velocidad baja.")
            elif gen == "antigua":
                r["avisos"].append("Generación anterior a Kepler: solo OpenGL (sin Vulkan).")
        elif g.get("vendor") == "intel":
            r["familia"] = f"Intel ({g.get('driver') or 'sin driver'})"
        out.append(r)
    return out


def decide(cards: list[dict], vk: list[dict]) -> dict:
    """Qué aplicar: cambio de driver (con prueba) y variables de entorno de las capas de traducción."""
    hw_vk = hardware_vulkan(vk)
    switch = any(c["cambiar_a_amdgpu"] for c in cards)
    env = {}
    why = []
    if not vk:
        # vulkaninfo no respondió (ni siquiera Lavapipe, que Mesa trae siempre): sin datos fiables no se toca
        # nada; forzar WineD3D en una GPU moderna costaría mucho rendimiento
        return {"cambiar_a_amdgpu": switch, "entorno": {}, "avisos": [a for c in cards for a in c["avisos"]],
                "razones": ["No se pudo consultar Vulkan (vulkaninfo): capas de traducción por defecto."]}
    if hw_vk:
        # Con Vulkan por hardware, Lavapipe (CPU) solo puede estorbar: un programa podría elegirlo por error
        env["VK_LOADER_DRIVERS_DISABLE"] = "*lvp*"
        why.append("Vulkan por hardware: DXVK/VKD3D para juegos de Windows; Lavapipe (CPU) oculto.")
    elif switch:
        why.append("El cambio a amdgpu dará Vulkan tras reiniciar; mientras, juegos de Windows por WineD3D.")
        env["PROTON_USE_WINED3D"] = "1"
    else:
        env["PROTON_USE_WINED3D"] = "1"
        why.append("Sin Vulkan por hardware: juegos de Windows por WineD3D (DirectX → OpenGL) en vez de DXVK; las "
                   "apps que exigen Vulkan usarán Lavapipe (en la CPU, lento pero funcionan).")
    return {"cambiar_a_amdgpu": switch, "entorno": env, "razones": why,
            "avisos": [a for c in cards for a in c["avisos"]]}


def detect(run=sh) -> dict:
    from . import hw
    gs = hw.gpus()
    vk = vulkan_devices(run)
    cards = classify(gs, amdgpu_ids(), vk, nvidia_chip(run) if any(g.get("vendor") == "nvidia" for g in gs) else "")
    return {"gpus": cards, "vulkan": vk, **decide(cards, vk)}


# ---------------------------------------------------------------- aplicar con prueba
# La prueba la gestiona GRUB (/etc/grub.d/05_tisma_graficos) con un grubenv en la partición EFI: «probar» → al
# arrancar, GRUB lo pasa a «fallido» ANTES de usar los parámetros; «ok» → siempre. Así un cuelgue en cada
# arranque con amdgpu no puede dejar el equipo sin arrancar: el siguiente arranque ya va sin el cambio.
ESP_CANDIDATES = ("/boot/efi", "/efi", "/boot")


def esp_env(root: str = "") -> str | None:
    for d in ESP_CANDIDATES:
        mp = root + d
        if os.path.isdir(mp) and (os.path.ismount(mp) or root):
            if os.path.isdir(os.path.join(mp, "EFI")) or root:
                return os.path.join(mp, "tisma", "grubenv")
    return None


def env_get(path: str, run=sh) -> str:
    for line in run(["grub-editenv", path, "list"]).splitlines():
        if line.startswith("tisma_gpu="):
            return line.split("=", 1)[1].strip()
    return ""


def env_set(path: str, value: str, run=sh) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        run(["grub-editenv", path, "create"])
    run(["grub-editenv", path, "set", f"tisma_gpu={value}"])


def write_env(env: dict) -> None:
    _write(ENVD, (ap.HEADER + "".join(f"{k}={v}\n" for k, v in sorted(env.items()))) if env else None)


def _write(rel: str, content: str | None) -> None:
    path = os.path.join(ap.ETC, rel)
    if content is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(content)
    os.replace(tmp, path)


def apply(info: dict, run=sh, env_path: str | None = None) -> list[str]:
    """Capas de traducción al momento y, si procede, el cambio a amdgpu EN PRUEBA para el próximo arranque."""
    msgs = []
    write_env(info["entorno"])
    msgs.append("Capas de traducción: " + (", ".join(f"{k}={v}" for k, v in info["entorno"].items()) or "por defecto")
                + " (desde el próximo inicio de sesión)")
    if not info["cambiar_a_amdgpu"]:
        return msgs
    st = ap.load_state(STATE) or {}
    if st.get("estado") == "fallido":
        msgs.append("El cambio a amdgpu ya falló en este equipo: se mantiene «radeon» (centinela graficos probar "
                    "para intentarlo de nuevo).")
        return msgs
    if st.get("estado") in ("confirmado", "en_prueba"):
        return msgs
    path = env_path or esp_env()
    if not path:
        msgs.append("GCN 1/2: el cambio a amdgpu necesita arranque UEFI (la prueba segura se guarda en la partición "
                    "EFI); en este equipo se queda en «radeon».")
        return msgs
    env_set(path, "probar", run)
    ap.save_state(STATE, {"estado": "en_prueba", "env": path})
    msgs.append("GCN 1/2: el próximo arranque probará amdgpu (Vulkan). Si no hubiera imagen, reinicia: volverá solo "
                "al driver anterior.")
    return msgs


def display_ok(card_root: str = "/sys/class/drm") -> bool:
    """¿Alguna salida de vídeo con imagen? (conector conectado y activado por el driver)"""
    for c in sysfs.ls(f"{card_root}/card[0-9]*-*"):
        if sysfs.read(f"{c}/status") == "connected" and sysfs.read(f"{c}/enabled") == "enabled":
            return True
    return False


def confirm(run=sh, check_display=display_ok, cmdline: str | None = None) -> str:
    """Tras cada arranque (temporizador): si este fue el arranque de prueba y hay imagen con amdgpu, se
    confirma para siempre; si no, queda descartado (GRUB ya lo marcó «fallido» antes de arrancar)."""
    st = ap.load_state(STATE) or {}
    if st.get("estado") != "en_prueba":
        return st.get("estado", "sin cambios")
    path = st.get("env") or esp_env()
    cmdline = cmdline if cmdline is not None else (sysfs.read("/proc/cmdline", "") or "")
    if "amdgpu.si_support=1" not in cmdline:
        # Arranque sin el cambio: o la prueba aún no ocurrió («probar»), o ya se descartó tras un cuelgue
        if path and env_get(path, run) == "fallido":
            ap.save_state(STATE, {"estado": "fallido"})
            return "revertido"
        return "en_prueba"
    from . import hw
    on_amdgpu = any(g.get("vendor") == "amd" and g.get("driver") == "amdgpu" for g in hw.gpus())
    if on_amdgpu and check_display():
        if path:
            env_set(path, "ok", run)
        ap.save_state(STATE, {"estado": "confirmado", "env": path})
        write_env(detect(run)["entorno"])          # ahora con Vulkan: DXVK y sin Lavapipe
        return "confirmado"
    ap.save_state(STATE, {"estado": "fallido"})    # GRUB ya dejó «fallido»: el próximo arranque va sin el cambio
    return "revertido"


def retry(run=sh) -> str:
    st = ap.load_state(STATE) or {}
    ap.save_state(STATE, {})
    info = detect(run)
    return "\n".join(apply(info, run)) if info["cambiar_a_amdgpu"] else "No hay GPU que pasar a amdgpu."


def revert(run=sh) -> str:
    st = ap.load_state(STATE) or {}
    path = st.get("env") or esp_env()
    if path and os.path.exists(path):
        env_set(path, "no", run)
    ap.save_state(STATE, {"estado": "fallido"})
    return "Vuelve a «radeon» desde el próximo arranque."


def summarize(info: dict) -> str:
    lines = []
    for c in info["gpus"]:
        lines.append(f"{c['card']}: {c['familia']} [{c['device_id']}]")
    lines.append("Vulkan: " + (", ".join(f"{d.get('deviceName')} ({d.get('driverName', '?')})" for d in info["vulkan"])
                               or "ninguno"))
    lines += [f"• {r}" for r in info["razones"]] + [f"⚠ {a}" for a in info["avisos"]]
    if info["cambiar_a_amdgpu"]:
        lines.append("→ Recomendado: pasar a amdgpu (sudo centinela graficos aplicar)")
    return "\n".join(lines)


def to_json(info: dict) -> str:
    return json.dumps(info, indent=2, ensure_ascii=False)
