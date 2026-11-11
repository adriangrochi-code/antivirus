#!/usr/bin/env bash
# Crea la clave de Secure Boot de TISMA (MOK) con la que la build firma el kernel TISMA.
# Hazlo en TU equipo y guarda la clave privada fuera del repositorio (y con copia: si se pierde, los equipos ya
# instalados no podrán verificar kernels nuevos firmados con otra). Nunca la subas a GitHub ni la pases por chat.
#
# Uso: iso/crear-clave-secureboot.sh <carpeta-destino>
# Luego, para construir:  TISMA_SB_KEY=<carpeta>/tisma-mok.key TISMA_SB_CERT=<carpeta>/tisma-mok.crt make iso
set -euo pipefail
DEST="${1:?carpeta de destino}"
mkdir -p "$DEST" && chmod 700 "$DEST"
umask 077
openssl req -new -x509 -newkey rsa:2048 -sha256 -days 3650 -nodes \
    -subj "/CN=TISMA OS Secure Boot (MOK)/" \
    -addext "extendedKeyUsage=codeSigning,1.3.6.1.4.1.311.10.3.6" \
    -keyout "$DEST/tisma-mok.key" -out "$DEST/tisma-mok.crt"
openssl x509 -in "$DEST/tisma-mok.crt" -outform DER -out "$DEST/tisma-mok.der"
echo "Clave creada en $DEST (tisma-mok.key es PRIVADA: guárdala fuera del repositorio y haz copia)."
