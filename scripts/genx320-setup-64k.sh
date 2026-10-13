#!/usr/bin/env bash
# Restore the previously working KV260 / GenX320 driver and EVT21 media setup.
# Intentionally DOES NOT reconfigure the FPGA or start video streaming.
set -Eeuo pipefail

DRIVER_DIR=/home/ubuntu/genx320-port
APP=prophesee-kv260-genx320
EVT21=0x5303

log() { printf '[genx320-setup] %s\n' "$*"; }
fail() { log "ERROR: $*" >&2; exit 1; }
trap 'fail "command failed at line $LINENO (exit $?)"' ERR

for tool in xmutil insmod media-ctl v4l2-ctl awk sed grep; do
    command -v "$tool" >/dev/null 2>&1 || fail "missing command: $tool"
done

# Wait for dfx-mgr to activate the already configured boot application.
# Never unload/load an app from a boot script: hot FPGA transitions have frozen this board.
log "Checking that $APP is already active; no FPGA changes will be made"
active=0
for _ in $(seq 1 60); do
    apps=$(xmutil listapps 2>/dev/null || true)
    slot=$(awk -v app="$APP" '$1 == app {print $NF; exit}' <<<"$apps")
    if [[ "$slot" == 0 || "$slot" == 0, ]]; then
        active=1
        break
    fi
    sleep 1
done
[[ "$active" == 1 ]] || fail "$APP not active in slot 0. Refusing to load camera drivers."

for dev in a0010000.mipi_csi2_rx_subsystem a0030000.ps_host_if \
           a0040000.axis_tkeep_handler a0050000.event_stream_smart_tracker; do
    [[ -d "/sys/bus/platform/devices/$dev" ]] || fail "missing FPGA device $dev"
done

# Load exactly the modules used in the recovered working setup.
# Already loaded modules are left untouched (no rmmod, no hot replacement).
load_module() {
    local name="$1" path="$2"
    if [[ -d "/sys/module/$name" ]]; then
        log "$name already loaded; leaving it alone"
    else
        [[ -f "$path" ]] || fail "module file missing: $path"
        log "Loading $name"
        insmod "$path"
    fi
}
load_module genx320_driver "$DRIVER_DIR/linux-sensor-drivers/genx320-driver.ko"
load_module psee_video "/home/ubuntu/genx320-preview-64k/psee-video.ko"
load_module psee_csi2rxss "$DRIVER_DIR/zynq-video-drivers/psee-csi2rxss.ko"
load_module psee_tkeep_handler "$DRIVER_DIR/zynq-video-drivers/psee-tkeep-handler.ko"
load_module psee_event_stream_smart_tracker "$DRIVER_DIR/zynq-video-drivers/psee-event-stream-smart-tracker.ko"
load_module psee_streamer "$DRIVER_DIR/zynq-video-drivers/psee-streamer.ko"

# Find the Prophesee media graph without assuming /dev/media0 or subdev numbering.
media=""
for _ in $(seq 1 30); do
    for candidate in /dev/media*; do
        [[ -e "$candidate" ]] || continue
        if media-ctl -d "$candidate" -p 2>/dev/null | grep -Eq '^driver[[:space:]]+psee-video[[:space:]]*$'; then
            media="$candidate"
            break
        fi
    done
    [[ -n "$media" ]] && break
    sleep 1
done
[[ -n "$media" ]] || fail 'Prophesee media graph did not appear'
log "Using media device $media"

# The sensor I2C bus number can vary, so discover its actual entity name.
topology=$(media-ctl -d "$media" -p)
sensor_name=$(sed -n -E 's/^- entity [0-9]+: (genx320 [^ ]+) .*/\1/p' <<<"$topology" | head -n 1)
[[ -n "$sensor_name" ]] || fail 'GenX320 sensor entity missing from media graph'

node_for() {
    local entity="$1" node
    node=$(media-ctl -d "$media" -e "$entity" 2>/dev/null) || fail "media entity unavailable: $entity"
    [[ -c "$node" ]] || fail "no character device for entity $entity: $node"
    printf '%s' "$node"
}
sensor=$(node_for "$sensor_name")
csi=$(node_for 'a0010000.mipi_csi2_rx_subsystem')
tkeep=$(node_for 'a0040000.axis_tkeep_handler')
tracker=$(node_for 'a0050000.event_stream_smart_tra')
video=$(node_for 'ps_host_if output 0')

set_pad() {
    local node="$1" pad="$2" out
    log "Configuring $node pad $pad as EVT21 320x320"
    out=$(v4l2-ctl -d "$node" \
        --set-subdev-fmt="pad=$pad,width=320,height=320,code=$EVT21,field=none" 2>&1) || {
        printf '%s\n' "$out" >&2
        fail "setting $node pad $pad failed"
    }
}

set_pad "$sensor" 0
set_pad "$csi" 0
set_pad "$csi" 1
set_pad "$tkeep" 0
set_pad "$tkeep" 1
set_pad "$tracker" 0
set_pad "$tracker" 1

verify_pad() {
    local node="$1" pad="$2" fmt
    fmt=$(v4l2-ctl -d "$node" --get-subdev-fmt="pad=$pad") || fail "reading $node pad $pad failed"
    grep -Eq 'Width/Height[[:space:]]*:[[:space:]]*320/320' <<<"$fmt" || fail "$node pad $pad is not 320x320"
    grep -Eq 'Mediabus Code[[:space:]]*:[[:space:]]*0x5303([[:space:]]|$)' <<<"$fmt" || fail "$node pad $pad is not EVT21"
}
verify_pad "$sensor" 0
verify_pad "$csi" 0
verify_pad "$csi" 1
verify_pad "$tkeep" 0
verify_pad "$tkeep" 1
verify_pad "$tracker" 0
verify_pad "$tracker" 1

fmt=$(v4l2-ctl -d "$video" --get-fmt-video) || fail "cannot read video format from $video"
grep -q "Pixel Format[[:space:]]*:[[:space:]]*'PSE2'" <<<"$fmt" || fail "$video is not PSE2"
grep -Eq 'Width/Height[[:space:]]*:[[:space:]]*320/320' <<<"$fmt" || fail "$video is not 320x320"

log "READY: $APP, six modules, EVT21 320x320, $video PSE2"
log 'No capture or STREAMON was attempted.'

