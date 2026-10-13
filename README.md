# Prophesee GenX320 on Kria KV260 — Ubuntu Port

Linux driver and deployment port for running the Prophesee GenX320
event-based camera on the AMD/Xilinx Kria KV260 with Ubuntu 22.04.

## Hardware and software

- Board: Kria KV260
- Sensor: Prophesee GenX320
- OS: Ubuntu 22.04.4 LTS (aarch64)
- Tested kernel: 5.15.0-1027-xilinx-zynqmp
- Output resolution: 320 × 320
- Event format: EVT21
- V4L2 pixel format: PSE2
- FPGA application: prophesee-kv260-genx320

## Project structure

- `linux-sensor-drivers/` — GenX320 sensor driver sources
- `zynq-video-drivers/` — V4L2, DMA, CSI-2 and FPGA pipeline drivers
- `scripts/genx320-setup.sh` — manual initialization
- `tools/` — experimental Python visualization and conversion utilities

## Status

The Ubuntu port has successfully captured a continuous EVT21 event stream.

A long recording showed CD events at:
- approximately 0 seconds
- approximately 64 seconds
- approximately 141 seconds

These measurements demonstrate that CD-event generation continued
well beyond the approximately 20 ms cutoff encountered during initial
porting experiments.

The working long-recording configuration had the packetizer
transfer timeout disabled.

This is experimental software. Extended reliability and clean DMA
shutdown remain under investigation.

## FPGA configuration

The Prophesee KV260 FPGA application must already be installed and
active before loading the drivers.

The tested setup uses the DFX manager to load
`prophesee-kv260-genx320` at boot.

Do not unload or reload the FPGA application from the setup script
when it is already active.

FPGA bitstreams and device-tree overlays are not included here.
Their distribution rights and original sources must be checked
separately.

## Build drivers

Install kernel headers matching the running Ubuntu kernel.

Build the sensor drivers:

```bash
cd linux-sensor-drivers
make KERNEL_SRC=/lib/modules/$(uname -r)/build -j$(nproc)
```

Build the video drivers using the Makefile and kernel-header configuration provided in `zynq-video-drivers`.

## Setup
The included script is based on the tested deployment layout `/home/ubuntu/genx320-port`.

Review its module paths before using it on a different system.

With the correct FPGA application active, run:
```bash
sudo bash scripts/genx320-setup.sh
```

The script loads the required modules if necessary and configures
the seven media pads to EVT21 at 320 × 320.

It does not start capture.

## Capture
Set the transfer timeout to the configuration used during
successful long recording:

```bash
v4l2-ctl -d /dev/video0 \
  --set-ctrl=transfer_timeout_enable=0
```

Capture a bounded number of V4L2 buffers:

```bash
v4l2-ctl -d /dev/video0 \
  --stream-mmap=4 \
  --stream-count=100 \
  --stream-to=/tmp/genx320-test.raw
```

V4L2 buffer count is not a camera frame count.

Raw event recordings can become very large. Monitor available disk
space during longer captures.

**Known issues**

- DMA stop warnings have occurred in some tests.
- The role of packetizer transfer timeout requires further validation.
- Long-term capture reliability has not been fully established.
- The custom PSE2 pixel format may cause a warning during format enumeration.

**Acknowledgments and licenses**

This work builds on the original GenX320 sensor and KV260 video
driver sources, as well as the PetaLinux reference implementation.
Original upstream projects, copyright notices and applicable
licenses must be retained and documented before public distribution.
