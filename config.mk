# Configuración global de la build de Centinela OS
KERNEL_VERSION ?= 6.18.63
DEBIAN_SUITE   ?= trixie
DEBIAN_MIRROR  ?= https://deb.debian.org/debian
ARCH           ?= amd64
BUILD_DIR      ?= $(CURDIR)/build
ISO_NAME       ?= centinela-os-$(shell date +%Y%m%d)-$(ARCH).iso
# Perfil del kernel: minimal | generic | tailored
KERNEL_PROFILE ?= generic
