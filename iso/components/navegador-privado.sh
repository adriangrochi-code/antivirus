#!/usr/bin/env bash
# «Navegador privado» de TISMA (app opcional, perfil «privacidad»): Mullvad Browser (el navegador Tor sin la red
# Tor: misma protección contra rastreo y huella, casi tan rápido como uno normal) + modo Tor (torbrowser-launcher
# de Debian, que descarga el navegador Tor y comprueba la firma de Tor Project).
# Mullvad Browser viene del repositorio oficial de Mullvad, con su clave comprobada por huella FIJADA aquí y
# limitado por apt a ESE paquete: el repositorio no puede sustituir ningún paquete de Debian.
# Uso: navegador-privado.sh <raíz>   (tisma-perfil lo llama con «/»)
set -euo pipefail
R="${1:-/}"
HUELLA="A1198702FC3E0A09A9AE5B75D5A1D4F266DE8DDF"          # Mullvad (code signing), documentada por Mullvad
URL_CLAVE="https://repository.mullvad.net/deb/mullvad-keyring.asc"
log() { printf '\033[1;35m[navegador-privado]\033[0m %s\n' "$*"; }
W="$(mktemp -d)"; trap 'rm -rf "$W"' EXIT

curl -fsSL --proto =https --tlsv1.2 --retry 3 -o "$W/clave.asc" "$URL_CLAVE"
huellas=$(gpg --homedir "$W" --batch --show-keys --with-colons "$W/clave.asc" 2>/dev/null | awk -F: '/^fpr/{print $10}')
primaria=$(gpg --homedir "$W" --batch --show-keys --with-colons "$W/clave.asc" 2>/dev/null \
           | awk -F: '/^pub/{p=1;next} p&&/^fpr/{print $10;exit}')
[[ "$primaria" == "$HUELLA" ]] || { log "La clave de Mullvad no es la esperada ($primaria): no se instala"; exit 1; }
[[ $(gpg --homedir "$W" --batch --show-keys --with-colons "$W/clave.asc" 2>/dev/null | grep -c '^pub') == 1 ]] \
    || { log "El archivo de clave trae más de una clave: no se instala"; exit 1; }
install -d -m 755 "$R/usr/share/keyrings" "$R/etc/apt/sources.list.d" "$R/etc/apt/preferences.d" "$R/etc/apt/apt.conf.d"
gpg --homedir "$W" --batch --dearmor -o "$W/mullvad.gpg" "$W/clave.asc"
install -m 644 "$W/mullvad.gpg" "$R/usr/share/keyrings/tisma-mullvad.gpg"
echo "deb [arch=amd64 signed-by=/usr/share/keyrings/tisma-mullvad.gpg] https://repository.mullvad.net/deb/stable stable main" \
    > "$R/etc/apt/sources.list.d/tisma-mullvad.list"
cat > "$R/etc/apt/preferences.d/tisma-mullvad" <<'PIN'
# TISMA: del repositorio de Mullvad SOLO se acepta mullvad-browser (nunca la VPN ni nada que pise a Debian)
Package: mullvad-browser
Pin: origin repository.mullvad.net
Pin-Priority: 500

Package: *
Pin: origin repository.mullvad.net
Pin-Priority: -1
PIN
# Que se actualice solo, como lo de Debian (un navegador sin parches es la puerta de entrada más común)
cat > "$R/etc/apt/apt.conf.d/53-tisma-mullvad" <<'UU'
// TISMA: Mullvad Browser (perfil «privacidad») se actualiza solo, como Debian
Unattended-Upgrade::Origins-Pattern { "origin=repository.mullvad.net,codename=stable"; };
UU
if [[ "$R" == "/" ]]; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get -q -o Acquire::Retries=3 -o DPkg::Lock::Timeout=900 update
    apt-get -y -q -o DPkg::Lock::Timeout=900 install --no-install-recommends mullvad-browser
fi
# Ajustes de TISMA: SOLO los que ningún sitio puede ver (el anti-rastreo de Mullvad/Tor funciona porque todos los
# usuarios se ven iguales: tocar idioma, tamaño, fuentes o JavaScript haría tu navegador único y rastreable)
if [[ -d "$R/usr/lib/mullvad-browser/defaults/pref" ]]; then
    cat > "$R/usr/lib/mullvad-browser/defaults/pref/tisma.js" <<'PREF'
// TISMA (perfil «privacidad»): rendimiento y seguridad sin cambiar la huella del navegador
pref("browser.tabs.unloadOnLowMemory", true);          // con poca memoria duerme pestañas en vez de colgarse
pref("browser.low_commit_space_threshold_percent", 15);
pref("dom.ipc.processPrelaunch.enabled", false);        // no deja un proceso extra esperando (≈60 MB menos)
pref("browser.download.start_downloads_in_tmp_dir", false); // las descargas van a Descargas: Escudo las revisa
pref("browser.download.always_ask_before_handling_new_types", true); // nada se abre solo al descargarse
PREF
fi
log "Listo"
