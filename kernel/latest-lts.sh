#!/usr/bin/env bash
# Imprime la última versión publicada de la rama LTS indicada (por defecto 6.18),
# para compilar siempre con todos los parches de seguridad disponibles.
set -euo pipefail
BRANCH="${1:-6.18}"
curl -fsSL https://www.kernel.org/releases.json | python3 -c "
import json, sys
b = sys.argv[1]
vs = [r['version'] for r in json.load(sys.stdin)['releases'] if r['version'].startswith(b + '.')]
print(vs[0] if vs else sys.exit('rama ' + b + ' no encontrada'))" "$BRANCH"
