# TISMA OS — construcción
#   make kernel          kernel genérico (base Debian + fragmentos)        → build/kernel-debs
#   make kernel-minimal  kernel para máquinas virtuales (compila rápido)
#   make cve-check       CVEs abiertos que afectan a nuestro kernel + fragmento de mitigación
#   make iso             ISO live/instalable (requiere root y un kernel compilado)
#   make test            pruebas de centinela, escudo y TISMA Code
#   make qemu-test       arranca el kernel en QEMU y comprueba LSMs, lockdown y firmas
include config.mk
export KERNEL_VERSION BUILD_DIR

LATEST := $(shell ./kernel/latest-lts.sh $(basename $(KERNEL_VERSION)) 2>/dev/null)
DEBCFG := $(BUILD_DIR)/debian-config

.PHONY: kernel kernel-minimal cve-check iso test qemu-test check-latest base-config

check-latest:
	@if [ -n "$(LATEST)" ] && [ "$(LATEST)" != "$(KERNEL_VERSION)" ]; then \
	  echo "Aviso: hay un parche más nuevo de la rama LTS: $(LATEST) (make KERNEL_VERSION=$(LATEST) ...)"; \
	else echo "Kernel $(KERNEL_VERSION): última versión de su rama LTS"; fi

# Config base de Debian (amplio soporte de hardware, todo como módulos)
base-config: $(DEBCFG)/config
$(DEBCFG)/config:
	mkdir -p $(DEBCFG)
	python3 kernel/fetch-debian-config.py $(DEBIAN_MIRROR) $(DEBIAN_SUITE) $@

kernel: check-latest base-config
	KERNEL_PROFILE=generic BASE_CONFIG=$(DEBCFG)/config ./kernel/build-kernel.sh

kernel-minimal: check-latest
	KERNEL_PROFILE=minimal ./kernel/build-kernel.sh

cve-check:
	python3 kernel/check-cves.py --src $(BUILD_DIR)/linux-$(KERNEL_VERSION) --version $(KERNEL_VERSION) \
	  --cache $(BUILD_DIR)/osv/linux.zip --fragment kernel/fragments/60-cve-mitigacion.config \
	  --report $(BUILD_DIR)/cves-$(KERNEL_VERSION).json

iso:
	sudo -E ./iso/build-iso.sh

test:
	cd userspace/tisma-code && python3 -m pytest -q tests
	cd userspace/centinela && python3 -m pytest -q tests
	cd userspace/escudo && python3 -m pytest -q tests
	cd userspace/asistente && python3 -m pytest -q tests
	cd userspace/sandbox && python3 -m pytest -q tests

qemu-test:
	./tests/qemu-boot-test.sh $(BUILD_DIR)/linux-$(KERNEL_VERSION)/arch/x86/boot/bzImage
