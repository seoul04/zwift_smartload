# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(central_hr)

# Modular version
target_sources(app PRIVATE
    src/main.c
    src/ftms_control_point.c
    src/notification_handler.c
    src/device_manager.c
    src/gatt_discovery.c
    src/nvs_storage.c
    src/led_feedback.c
    src/ecg_stream.c
    src/serial_cmd.c
)

zephyr_library_include_directories(${ZEPHYR_BASE}/samples/bluetooth)

# Compile-time diagnostic: drop the Heart Rate Service (0x180D) subscription.
#
# The Movesense app's HrBridge subscribes /Component/MAX3000x/HeartRateData
# whenever a client enables 0x2A37, and the MAX3000x is the same AFE that
# produces /Meas/ECG. Measured on the host with the same sensor, ECG was clean
# in 5/5 trials with 0x2A37 left off and failed in 2/4 with it on, so this
# removes HR from the link entirely to see whether the dongle's stalls follow.
#
# Off by default: the Polar H10 has no other source of R-R, and the Movesense
# GSP /Meas/HR fallback is covered by the same switch because it is the same
# AFE consumer wearing a different path.
#
# Set it through the environment, not with -D:
#
#   HR_OFF=1 west build -b nrf52840dongle/nrf52840 -d build_hr_off
#
# This project builds with sysbuild, and a -DHR_OFF=ON is only recorded in the
# sysbuild cache, so it never reaches the application image's compilation - the
# build succeeds and silently produces an HR-on binary. An environment variable
# is inherited by the app's CMake process, so it does take effect. The configure
# step prints "HR_OFF: enabled" when it is picked up; if that line is missing,
# the build is an HR-on build whatever the command line said.
#
# Build in a separate directory (-d build_hr_off) so the HR-on baseline used
# for comparison is not overwritten.
if(HR_OFF OR "$ENV{HR_OFF}" STREQUAL "1")
    message(STATUS "HR_OFF: enabled - Heart Rate Service subscription disabled")
    zephyr_compile_definitions(HR_OFF=1)
endif()
