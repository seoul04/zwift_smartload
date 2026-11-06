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
#include "notification_handler.h"
#include "device_manager.h"
#include "gatt_discovery.h"
#include "nvs_storage.h"
#include "led_feedback.h"
#include "ecg_stream.h"
#include "serial_cmd.h"

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
uint32_t last_cp_data_time = 0;
uint64_t total_rx_count = 0;
char device_name_buffer[32] = DEVICE_NAME_PREFIX;
static uint32_t last_button_event_time = 0;  /* For debouncing */
#define BUTTON_DEBOUNCE_MS 100

/* Vendor-specific services.
 * UUID byte arrays are little-endian, i.e. the reverse of the printed form. */
const struct bt_uuid_128 pmd_service_uuid = BT_UUID_INIT_128(
	0xc8, 0xf0, 0x8d, 0x2d, 0xcd, 0x8a, 0xad, 0x1c,
	0x87, 0xf3, 0xe7, 0x02, 0x80, 0x5c, 0x00, 0xfb);

const struct bt_uuid_128 pmd_control_point_uuid = BT_UUID_INIT_128(
	0xc8, 0xf0, 0x8d, 0x2d, 0xcd, 0x8a, 0xad, 0x1c,
	0x87, 0xf3, 0xe7, 0x02, 0x81, 0x5c, 0x00, 0xfb);

const struct bt_uuid_128 pmd_data_uuid = BT_UUID_INIT_128(
	0xc8, 0xf0, 0x8d, 0x2d, 0xcd, 0x8a, 0xad, 0x1c,
	0x87, 0xf3, 0xe7, 0x02, 0x82, 0x5c, 0x00, 0xfb);

const struct bt_uuid_128 gsp_service_uuid = BT_UUID_INIT_128(
	0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4,
	0x5d, 0x4d, 0x85, 0x71, 0x52, 0x22, 0x80, 0x34);

const struct bt_uuid_128 gsp_command_uuid = BT_UUID_INIT_128(
	0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4,
	0x5d, 0x4d, 0x85, 0x71, 0x01, 0x00, 0x80, 0x34);

const struct bt_uuid_128 gsp_data_uuid = BT_UUID_INIT_128(
	0xf0, 0xe8, 0x50, 0x70, 0x0e, 0x63, 0x31, 0xb4,
	0x5d, 0x4d, 0x85, 0x71, 0x02, 0x00, 0x80, 0x34);

/* Services to discover */
const struct bt_uuid *discover_services[] = {
	BT_UUID_HRS,
	BT_UUID_CPS,
	BT_UUID_FMS,
	&gsp_service_uuid.uuid,
	&pmd_service_uuid.uuid,
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

	/* MTU exchange runs right after the connection completes, so this is the
	 * pre-exchange value. The settled MTU is logged on the first notification. */
	slot->mtu_logged = false;
	log("Initial ATT MTU: %u\n", bt_gatt_get_mtu(conn));

	/* Update LED feedback for new connection */
	led_feedback_update();
	
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

	/* Log every service first, then run the targeted per-service discovery. */
	start_enumeration(conn, slot_idx);
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
			connections[i].cur_service = 0;
			/* Drop the ECG sample clock and any half-received GSP frame:
			 * a reconnect starts a new stream at a new time base. */
			ecg_stream_reset(&connections[i]);
			connections[i].hr_rr_misses = 0;
			
			bt_conn_unref(connections[i].conn);
			connections[i].conn = NULL;
			log("Freed connection slot %d\n", i);
			found_slot = true;
			
			/* Update LED feedback for disconnection */
			led_feedback_update();
			break;
		}
	}

	/* Cancel timeout if this was a pending connection */
	cancel_connection_timeout(conn);

	if (found_slot) {
		start_scan();
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
		
		/* Disconnect all connected devices */
		disconnect_all_devices();
		
		/* Clear saved devices to start fresh pairing */
		nvs_clear_all_devices();
		log("Cleared all saved devices\n");
		
		start_scan_window(5 * 60 * 1000);  /* 5 minutes */
	}
}

static void button_pressed(const struct device *dev, struct gpio_callback *cb, uint32_t pins)
{
	uint32_t now = k_uptime_get_32();
	
	/* Debounce - ignore events within 100ms of last event */
	if ((now - last_button_event_time) < BUTTON_DEBOUNCE_MS) {
		return;
	}
	last_button_event_time = now;
	
	int button_state = gpio_pin_get_dt(&button);
	
	if (button_state == 1) {
		/* Button pressed */
		button_press_time = now;
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

void announce_identity(void)
{
	bt_addr_le_t id_addrs[CONFIG_BT_ID_MAX];
	size_t id_count = ARRAY_SIZE(id_addrs);
	char id_addr[BT_ADDR_STR_LEN] = "";

	bt_id_get(id_addrs, &id_count);
	if (id_count > 0) {
		bt_addr_to_str(&id_addrs[0].a, id_addr, sizeof(id_addr));
	}
	json_out("{\"type\":\"info\",\"ts\":%u,\"name\":\"%s\",\"addr\":\"%s\",\"version\":\"%s\"}\n",
		 k_uptime_get_32(), device_name_buffer, id_addr, VERSION);
}

int main(void)
{
	int err;

	/* Delay to allow serial terminal to connect before first logs */
	k_sleep(K_SECONDS(5));

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

	/* Initialize button press tracking work BEFORE configuring interrupt */
	k_work_init_delayable(&long_press_work, long_press_timeout_handler);

	err = gpio_pin_interrupt_configure_dt(&button, GPIO_INT_EDGE_BOTH);
	if (err < 0) {
		log("Failed to configure button interrupt (err %d)\n", err);
		return 0;
	}

	gpio_init_callback(&button_cb_data, button_pressed, BIT(button.pin));
	gpio_add_callback(button.port, &button_cb_data);
	log("Button initialized on pin %d\n", button.pin);

	/* Initialize modules */
	device_manager_init();
	led_feedback_init();

	/* Commands from z_relay_server over the console, so Zwift's FTMS control
	 * point writes (grade, ERG) reach the trainer. Without this the server's
	 * write blocks against a CDC endpoint nobody drains and wedges its DirCon
	 * event loop. */
	int serial_err = serial_cmd_init();

	if (serial_err) {
		log("Serial command input unavailable (err %d); control point "
		    "writes from the server will not reach the trainer\n", serial_err);
	}

	/* Print initial device list */
	print_device_list();

	log("Central HR Sample Version %s\n", VERSION);

	/* Get device suffix and create full device name */
	char device_suffix[6];
	int suffix_result = nvs_get_device_suffix(device_suffix, sizeof(device_suffix));
	
	if (suffix_result == 0) {
		snprintf(device_name_buffer, sizeof(device_name_buffer), DEVICE_NAME_PREFIX "-%s", device_suffix);
	} else {
		strncpy(device_name_buffer, DEVICE_NAME_PREFIX, sizeof(device_name_buffer) - 1);
		device_name_buffer[sizeof(device_name_buffer) - 1] = '\0';
	}
	
	log("Bluetooth initialized as '%s'\n", device_name_buffer);

	/* Announce our identity on the console. z_relay_server uses the name and
	 * address to present the sensors to Zwift over DirCon. (This is the line
	 * the server reads; the console also carries commands the other way, see
	 * serial_cmd_init().) */
	announce_identity();

	log("Device ready - press button for 2+ seconds to enable scanning (5 min window)\n");
	return 0;
}
