#!/usr/bin/env bash
# Copia de Hope (repositorio privado) todo lo que NO es la red neuronal: herramientas,
# crítico, consejo, memoria, guardián… hacia vendor/. No se versiona aquí porque el
# repositorio de Hope es privado.
#
# Uso: fetch-hope.sh [ruta-local-o-URL-git]   (por defecto: el repo privado en GitHub)
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC="${1:-https://github.com/adriangrochi-code/hope-0.1}"
DEST="$HERE/vendor"
WORK=""
if [[ -d "$SRC/hope" ]]; then
    REPO="$SRC"
else
    WORK="$(mktemp -d)"
    trap 'rm -rf "$WORK"' EXIT
    git clone -q --depth 1 "$SRC" "$WORK/hope"
    REPO="$WORK/hope"
fi
rm -rf "$DEST" && mkdir -p "$DEST/configs"
# Fuera: núcleo neuronal, entrenamiento, plasticidad, topología, benchmarks de la red y su app
rsync -a --exclude __pycache__ \
    --exclude 'core/' --exclude 'train/' --exclude 'plasticity/' --exclude 'registry/' \
    --exclude 'bench/' --exclude 'app/' --exclude 'lineage/' \
    --exclude 'device.py' --exclude 'data/vision.py' --exclude 'eval/vision_bench.py' \
    --exclude 'growth/asg.py' --exclude 'growth/budget_target.py' --exclude 'growth/hn1_ops.py' \
    --exclude 'growth/lth.py' --exclude 'growth/ops.py' --exclude 'growth/usage_report.py' \
    --exclude 'growth/zone_composer.py' --exclude 'teacher/cycle.py' --exclude 'teacher/harvest.py' \
    --exclude 'teacher/tutor.py' --exclude 'teacher/vision.py' --exclude 'teacher/vision_tutores.py' \
    "$REPO/hope" "$DEST/"
rsync -a --exclude __pycache__ --exclude cli.py "$REPO/guardian" "$DEST/"
for f in guardian teacher teacher_local web growth_budget curriculum; do
    [[ -f "$REPO/configs/$f.yaml" ]] && cp "$REPO/configs/$f.yaml" "$DEST/configs/"
done
git -C "$REPO" rev-parse HEAD > "$DEST/HOPE_COMMIT" 2>/dev/null || true
echo "Hope (sin red neuronal) copiado en $DEST"
