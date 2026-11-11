#!/usr/bin/env bash
# Avisa si hay una rama estable del kernel más nueva que la de la versión indicada: las correcciones de seguridad
# nuevas solo llegan a las ramas mantenidas, y una estable deja de estarlo ~3 meses después de salir.
# Uso: rama-mas-nueva.sh <versión actual, p. ej. 7.3.4>
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
actual="${1:?versión actual}"
nueva="$("$HERE/latest-lts.sh" stable 2>/dev/null)" || exit 0
if [[ "${nueva%.*}" != "${actual%.*}" ]]; then
    echo "AVISO DE SEGURIDAD: hay una rama estable más nueva ($nueva). Actualiza KERNEL_VERSION en config.mk"
    echo "  y regenera la mitigación de CVE (make cve-check)."
fi
