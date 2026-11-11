# Configuración global de la build de Centinela OS
# Rama ESTABLE más reciente (no la LTS): medido en noviembre de 2026 con la base de CVE de kernel.org, la LTS
# 6.18.63 tenía 153 CVE corregidas en ramas nuevas y nunca llevadas a la 6.18; la estable 7.3.4, ninguna. Las
# estables se mantienen ~3 meses: «make check-latest» avisa cuando sale la siguiente.
KERNEL_VERSION ?= 7.3.4
DEBIAN_SUITE   ?= trixie
DEBIAN_MIRROR  ?= https://deb.debian.org/debian
ARCH           ?= amd64
BUILD_DIR      ?= $(CURDIR)/build
ISO_NAME       ?= centinela-os-$(shell date +%Y%m%d)-$(ARCH).iso
# Perfil del kernel: minimal | generic | tailored
KERNEL_PROFILE ?= generic
