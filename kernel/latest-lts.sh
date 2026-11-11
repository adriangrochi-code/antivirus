#!/usr/bin/env bash
# Imprime la última versión publicada de la rama indicada (p. ej. 7.3 o 6.18), o con «stable» la de la rama
# estable más nueva: para compilar siempre con todos los parches de seguridad disponibles.
set -euo pipefail
BRANCH="${1:-stable}"
curl -fsSL https://www.kernel.org/releases.json | python3 -c "
import json, sys
b = sys.argv[1]
rel = json.load(sys.stdin)['releases']
if b == 'stable':
    vs = [r['version'] for r in rel if r['moniker'] == 'stable']
else:
    vs = [r['version'] for r in rel if r['version'].startswith(b + '.')]
print(vs[0] if vs else sys.exit('rama ' + b + ' no encontrada'))" "$BRANCH"
