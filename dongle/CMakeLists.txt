# SPDX-License-Identifier: Apache-2.0

cmake_minimum_required(VERSION 3.20.0)
find_package(Zephyr REQUIRED HINTS $ENV{ZEPHYR_BASE})
project(central_hr)

# Modular version
target_sources(app PRIVATE
    src/main.c
    src/gatt_services.c
    src/ftms_control_point.c
    src/notification_handler.c
    src/device_manager.c
    src/gatt_discovery.c
    src/nvs_storage.c
)

zephyr_library_include_directories(${ZEPHYR_BASE}/samples/bluetooth)
