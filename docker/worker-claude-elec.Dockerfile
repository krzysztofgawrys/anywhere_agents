# Elec worker overlay: base worker-claude image + electronics tooling.
# Bases on the primary worker image (always built as a core service) so the
# generic worker-claude image stays lean. Adds:
#   - OpenOCD runtime shared libs needed by the vendored WCH/OpenOCD binaries
#     in ~/elec (found via `ldd` on riscv-openocd-wch and the WCH 1.92 build)
#   - dfu-util + usbutils so DFU flashing / USB listing work from PATH
#   - gh (GitHub CLI)
# Raw USB passthrough itself is handled in compose.override.yml
# (/dev/bus/usb bind + `c 189:* rmw` cgroup rule); host udev rules give the
# nodes plugdev/0666 so the uid-1000 worker can open them.
FROM claude_cloud-worker-claude:latest

USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        libftdi1-2 \
        libhidapi-hidraw0 \
        libhidapi-libusb0 \
        libjaylink0 \
        libusb-1.0-0 \
        dfu-util \
        usbutils \
        gh \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*
USER 1000:1000
