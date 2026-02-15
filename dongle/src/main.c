/* main.c - Application main entry point */

/*
 * Copyright (c) 2015-2016 Intel Corporation
 *
 * SPDX-License-Identifier: Apache-2.0
 */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/sys/byteorder.h>
#include <zephyr/drivers/gpio.h>
#include <string.h>

#include "common.h"
#include "gatt_services.h"
#include "ftms_control_point.h"
#include "notification_handler.h"
#include "device_manager.h"
#include "gatt_discovery.h"
#include "nvs_storage.h"

/* Button configuration */
#define SW0_NODE DT_ALIAS(sw0)
static const struct gpio_dt_spec button = GPIO_DT_SPEC_GET(SW0_NODE, gpios);
static struct gpio_callback button_cb_data;

/* Button press tracking */
static uint32_t button_press_time = 0;
static struct k_work_delayable long_press_work;

/* Serial output mutex for thread-safe logging and JSON output */
K_MUTEX_DEFINE(serial_output_mutex);

/* Global variables */
struct conn_slot connections[MAX_CONNECTIONS];
struct bt_conn *peripheral_conn = NULL;
uint32_t last_cp_data_time = 0;
uint64_t total_rx_count = 0;

/* Services to discover */
const struct bt_uuid *discover_services[] = {
	BT_UUID_HRS,
	BT_UUID_CPS,
	BT_UUID_FMS,
};
const int discover_service_count = ARRAY_SIZE(discover_services);

static void connected(struct bt_conn *conn, uint8_t conn_err)
{
	char addr[BT_ADDR_LE_STR_LEN];

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

	/* Find the connection slot */
	struct conn_slot *slot = NULL;
	int slot_idx = -1;
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (connections[i].conn == conn) {
			slot = &connections[i];
			slot_idx = i;
			break;
		}
	}

	if (!slot) {
		log("Connected but no slot found: %s\n", addr);
		return;
	}

	if (conn_err) {
		log("Failed to connect to %s (%u)\n", addr, conn_err);
		
		/* Cancel timeout if this was the pending connection */
		cancel_connection_timeout(conn);
		
		bt_conn_unref(slot->conn);
		slot->conn = NULL;
		start_scan();
		return;
	}

	log("Connected: %s\n", addr);
	
	/* Copy RSSI from device_info (captured during scanning) */
	slot->rssi = 0;  /* Default if not found */
	struct device_info *dev_info;
	SYS_SLIST_FOR_EACH_CONTAINER(&device_list, dev_info, node) {
		if (!bt_addr_le_cmp(&dev_info->addr, bt_conn_get_dst(conn))) {
			slot->rssi = dev_info->rssi;
			log("RSSI at connection: %d dBm\n", slot->rssi);
			break;
		}
	}
	print_device_list();

	/* Cancel timeout if this was a pending connection */
	cancel_connection_timeout(conn);
	
	/* Save device to NVS for future reconnection priority */
	save_connected_device(conn);

	/* Start discovery */
	start_discovery(conn, slot_idx);
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	char addr[BT_ADDR_LE_STR_LEN];

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

	log("Disconnected: %s, reason 0x%02x %s\n", addr, reason, bt_hci_err_to_str(reason));

	/* Remove device from list so it can be cleanly re-discovered and re-connected */
	struct device_info *dev_info, *tmp;
	SYS_SLIST_FOR_EACH_CONTAINER_SAFE(&device_list, dev_info, tmp, node) {
		if (!bt_addr_le_cmp(&dev_info->addr, bt_conn_get_dst(conn))) {
			log("Removed device from list: %s\n", dev_info->name);
			sys_slist_find_and_remove(&device_list, &dev_info->node);
			k_free(dev_info);
			print_device_list();
			break;
		}
	}

	/* Find and clear the connection slot */
	bool found_slot = false;
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (connections[i].conn == conn) {
			log("Freeing connection slot %d (%d subscriptions)\n", 
			       i, connections[i].subscribe_count);
			
			/* Note: Don't call bt_gatt_unsubscribe here - connection is already
			 * disconnected so it fails with -ENOTCONN. The subscription params
			 * are cleared below and will be handled on next subscribe attempt. */
			
			/* Clear subscription state so params can be reused */
			connections[i].subscribe_count = 0;
			
			bt_conn_unref(connections[i].conn);
			connections[i].conn = NULL;
			log("Freed connection slot %d\n", i);
			found_slot = true;
			break;
		}
	}

	/* Cancel timeout if this was a pending connection */
	cancel_connection_timeout(conn);

	if (found_slot) {
		/* Sensor disconnected */
		start_scan();
	} else {
		/* Peripheral disconnected */
		log("Peripheral disconnected, restarting advertising\n");
		
		if (peripheral_conn == conn) {
			bt_conn_unref(peripheral_conn);
			peripheral_conn = NULL;
			log("[FTMS CP] Cleared peripheral connection\n");
		}
		
		start_advertising();
	}
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
};

static void print_table_work_handler(struct k_work *work)
{
	log("Printing device list\n");
	print_device_list();
}

K_WORK_DEFINE(print_table_work, print_table_work_handler);

static void long_press_timeout_handler(struct k_work *work)
{
	/* Check if button is still pressed after 2 seconds */
	int button_state = gpio_pin_get_dt(&button);
	if (button_state == 1) {
		/* Button still pressed - it's a long press */
		log("Long button press detected - enabling scan window for 5 minutes\n");
		start_scan_window(5 * 60 * 1000);  /* 5 minutes */
	}
}

static void button_pressed(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
	int button_state = gpio_pin_get_dt(&button);
	
	if (button_state == 1) {
		/* Button pressed */
		button_press_time = k_uptime_get_32();
		log("Button pressed\n");
		
		/* Schedule long press check after 2 seconds */
		k_work_reschedule_for_queue(&k_sys_work_q, &long_press_work, K_MSEC(2000));
	} else {
		/* Button released */
		uint32_t press_duration = k_uptime_get_32() - button_press_time;
		
		if (press_duration < 2000) {
			/* Short press - cancel long press work and print device list */
			k_work_cancel_delayable(&long_press_work);
			log("Short button press (%u ms) - printing device list\n", press_duration);
			k_work_submit(&print_table_work);
		}
		/* If long press, the timeout work already handled it */
	}
}

int main(void)
{
	int err;

	err = bt_enable(NULL);

	if (err) {
		log("Bluetooth init failed (err %d)\n", err);
		return 0;
	}

	/* Initialize button */
	if (!gpio_is_ready_dt(&button)) {
		log("Button device not ready\n");
		return 0;
	}

	err = gpio_pin_configure_dt(&button, GPIO_INPUT);
	if (err < 0) {
		log("Failed to configure button (err %d)\n", err);
		return 0;
	}

	err = gpio_pin_interrupt_configure_dt(&button, GPIO_INT_EDGE_BOTH);
	if (err < 0) {
		log("Failed to configure button interrupt (err %d)\n", err);
		return 0;
	}

	/* Initialize button press tracking work */
	k_work_init_delayable(&long_press_work, long_press_timeout_handler);

	gpio_init_callback(&button_cb_data, button_pressed, BIT(button.pin));
	gpio_add_callback(button.port, &button_cb_data);
	log("Button initialized on pin %d\n", button.pin);

	/* Initialize modules */
	device_manager_init();
	ftms_control_point_init();

	/* Print initial device list */
	print_device_list();

	log("Central HR Sample Version %s\n", VERSION);

	/* Get unique device suffix from hardware ID (computed from FICR) */
	char device_suffix[6];  /* 4 hex chars + null terminator */
	if (nvs_get_device_suffix(device_suffix, sizeof(device_suffix)) == 0) {
		char full_name[32];
		snprintf(full_name, sizeof(full_name), "Z-Relay-%s", device_suffix);
		bt_set_name(full_name);
		log("Bluetooth initialized as '%s'\n", full_name);
	} else {
		bt_set_name("Z-Relay");
		log("Bluetooth initialized as 'Z-Relay' (suffix retrieval failed)\n");
	}

	start_advertising();
	log("Device ready - press button for 2+ seconds to enable scanning (5 min window)\n");
	return 0;
}
