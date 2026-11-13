/* device_manager.c - Device list and scanning */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gap.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/sys/byteorder.h>
#include <string.h>
#include <stdio.h>
#include "common.h"
#include "device_manager.h"
#include "nvs_storage.h"
#include "led_feedback.h"
#include "ecg_stream.h"

sys_slist_t device_list;

/* Connection timeout work */
static struct k_work_delayable conn_timeout_work;
static struct bt_conn *pending_conn = NULL;

/* Scan window control - only scan during active window */
static bool scan_window_active = false;
static struct k_work_delayable scan_window_timeout;

void print_device_list(void)
{
	struct device_info *dev_info;
	char addr[BT_ADDR_LE_STR_LEN];
	uint32_t now = k_uptime_get_32();
	int count = 0;

	/* Count devices first */
	SYS_SLIST_FOR_EACH_CONTAINER(&device_list, dev_info, node) {
		count++;
	}

	json_begin();
	json_out("{\"type\":\"devices\",\"ts\":%u,\"count\":%d,\"list\":[", now, count);
	
	int idx = 0;
	SYS_SLIST_FOR_EACH_CONTAINER(&device_list, dev_info, node) {
		bool is_connected = false;
		for (int i = 0; i < MAX_CONNECTIONS; i++) {
			if (connections[i].conn) {
				const bt_addr_le_t *conn_addr = bt_conn_get_dst(connections[i].conn);
				if (!bt_addr_le_cmp(conn_addr, &dev_info->addr)) {
					is_connected = true;
					break;
				}
			}
		}
		bt_addr_le_to_str(&dev_info->addr, addr, sizeof(addr));
		json_out("{\"name\":\"%s\",\"addr\":\"%s\",\"connected\":%s,\"saved\":%s,\"battery_level\":",
		       dev_info->name, addr, is_connected ? "true" : "false", 
		       dev_info->is_saved ? "true" : "false");
		if (dev_info->battery_level >= 0) {
			json_out("%d", dev_info->battery_level);
		} else {
			json_out("null");
		}
		json_out(",\"last_seen\":%u}", dev_info->last_seen);
		if (idx < count - 1) {
			json_out(",");
		}
		idx++;
	}
	json_out("]}\n");
	json_end();
}

static void conn_timeout_handler(struct k_work *work)
{
	if (pending_conn) {
		char addr[BT_ADDR_LE_STR_LEN];
		bt_addr_le_to_str(bt_conn_get_dst(pending_conn), addr, sizeof(addr));
		log("Connection timeout to %s, cancelling...\n", addr);

		/* Drop the reference before asking for a disconnect, and clear
		 * pending_conn either way.
		 *
		 * This used to clear both only when bt_conn_disconnect() failed. On
		 * success pending_conn kept pointing at a connection that was being
		 * torn down, so it was never unref'd and stayed stale; and because the
		 * work was scheduled with k_work_schedule() it could fire again on a
		 * still-live connection, disconnecting devices that had connected
		 * perfectly well. On hardware that showed up as a disconnect/reconnect
		 * cycle every few seconds with reason 0x00 (local termination). */
		int err = bt_conn_disconnect(pending_conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);

		bt_conn_unref(pending_conn);
		pending_conn = NULL;

		if (err) {
			log("Failed to cancel connection (err %d)\n", err);
			start_scan();
		}
	}
}

void cancel_connection_timeout(struct bt_conn *conn)
{
	if (pending_conn && pending_conn == conn) {
		k_work_cancel_delayable(&conn_timeout_work);
		bt_conn_unref(pending_conn);
		pending_conn = NULL;
	}
}

/* One role per device (trainer > power meter > HR): trainers often also advertise CP */
static uint8_t primary_role(uint8_t svc_mask)
{
	if (svc_mask & 0x04) {
		return 0x04;
	}
	if (svc_mask & 0x02) {
		return 0x02;
	}
	return svc_mask & 0x01;
}

static struct device_info *find_device(const bt_addr_le_t *addr)
{
	struct device_info *dev_info;

	SYS_SLIST_FOR_EACH_CONTAINER(&device_list, dev_info, node) {
		if (!bt_addr_le_cmp(&dev_info->addr, addr)) {
			return dev_info;
		}
	}
	return NULL;
}

/* Roles already held by connected (or connecting) devices */
static uint8_t taken_roles(void)
{
	uint8_t roles = 0;

	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (connections[i].conn) {
			struct device_info *dev = find_device(bt_conn_get_dst(connections[i].conn));
			if (dev) {
				roles |= primary_role(dev->svc_mask);
			}
		}
	}
	return roles;
}

static bool eir_found(struct bt_data *data, void *user_data)
{
	struct {
		const bt_addr_le_t *addr;
		char name[32];
		uint8_t svc_mask;
		bool has_battery_service;
	} *ctx = user_data;

	int i;

	switch (data->type) {
	case BT_DATA_UUID16_SOME:
	case BT_DATA_UUID16_ALL:
		if (data->data_len % sizeof(uint16_t) != 0U) {
			return true;
		}

		for (i = 0; i < data->data_len; i += sizeof(uint16_t)) {
			uint16_t u16;
			memcpy(&u16, &data->data[i], sizeof(u16));
			uint16_t uuid16 = sys_le16_to_cpu(u16);
			const struct bt_uuid *uuid = BT_UUID_DECLARE_16(uuid16);

			if (!bt_uuid_cmp(uuid, BT_UUID_HRS)) {
				ctx->svc_mask |= 0x01;
			} else if (!bt_uuid_cmp(uuid, BT_UUID_DECLARE_16(0x1818))) {
				ctx->svc_mask |= 0x02;
			} else if (!bt_uuid_cmp(uuid, BT_UUID_DECLARE_16(0x1826))) {
				ctx->svc_mask |= 0x04;
			} else if (uuid16 == 0x180F) {
				ctx->has_battery_service = true;
			}
		}
		break;
	case BT_DATA_NAME_COMPLETE:
	case BT_DATA_NAME_SHORTENED:
		{
			size_t len = data->data_len < sizeof(ctx->name) - 1 ? data->data_len : sizeof(ctx->name) - 1;
			memcpy(ctx->name, data->data, len);
			ctx->name[len] = '\0';
		}
		break;
	default:
		break;
	}

	return true;
}

static void device_found(const bt_addr_le_t *addr, int8_t rssi, uint8_t type,
			 struct net_buf_simple *ad)
{
	char dev[BT_ADDR_LE_STR_LEN];

	bt_addr_le_to_str(addr, dev, sizeof(dev));

	struct {
		const bt_addr_le_t *addr;
		char name[32];
		uint8_t svc_mask;
		bool has_battery_service;
	} parse_ctx;
	parse_ctx.addr = addr;
	parse_ctx.name[0] = '\0';
	parse_ctx.svc_mask = 0;
	parse_ctx.has_battery_service = false;

	bt_data_parse(ad, eir_found, &parse_ctx);

	/* Filter out other Z-Relay devices to avoid cross-connection */
	if (strncmp(parse_ctx.name, DEVICE_NAME_PREFIX, strlen(DEVICE_NAME_PREFIX)) == 0) {
		return;
	}

	uint32_t now = k_uptime_get_32();

	struct device_info *dev_info = NULL;
	struct device_info *iter;
	bool found = false;
	SYS_SLIST_FOR_EACH_CONTAINER(&device_list, iter, node) {
		if (!bt_addr_le_cmp(&iter->addr, addr)) {
			dev_info = iter;
			found = true;
			break;
		}
	}

	if (found && dev_info) {
		if (parse_ctx.name[0] != '\0' && strcmp(dev_info->name, parse_ctx.name) != 0) {
			bool was_address = (strcmp(dev_info->name, dev) == 0);
			strncpy(dev_info->name, parse_ctx.name, sizeof(dev_info->name) - 1);
			dev_info->name[sizeof(dev_info->name) - 1] = '\0';
			if (was_address) {
				log("Captured name for device: %s (%s)\n", dev_info->name, dev);
			}
		}
		dev_info->last_seen = now;
		dev_info->svc_mask |= parse_ctx.svc_mask;
		dev_info->has_battery_service = dev_info->has_battery_service || parse_ctx.has_battery_service;
		dev_info->rssi = rssi;  /* Update RSSI from latest advertisement */
		/* Check if device is saved */
		dev_info->is_saved = nvs_is_device_saved(&dev_info->addr);
	} else if (!found) {
		/* Only track saved devices, or any device during scan window */
		bool is_saved = nvs_is_device_saved(addr);
		
		/* Outside scan window: only track saved devices */
		if (!scan_window_active && !is_saved) {
			return;
		}
		
		/* During scan window: track devices with relevant services */
		if (scan_window_active && parse_ctx.svc_mask == 0 && !is_saved) {
			/* Skip devices without HR/CP/FTMS services */
			return;
		}
		
		dev_info = k_malloc(sizeof(struct device_info));
		if (dev_info) {
			memcpy(&dev_info->addr, addr, sizeof(bt_addr_le_t));
			if (parse_ctx.name[0] != '\0') {
				strncpy(dev_info->name, parse_ctx.name, sizeof(dev_info->name) - 1);
				dev_info->name[sizeof(dev_info->name) - 1] = '\0';
			} else {
				strncpy(dev_info->name, dev, sizeof(dev_info->name) - 1);
				dev_info->name[sizeof(dev_info->name) - 1] = '\0';
			}
			dev_info->svc_mask = parse_ctx.svc_mask;
			dev_info->has_battery_service = parse_ctx.has_battery_service;
			dev_info->battery_level = -1;
			dev_info->last_seen = now;
			dev_info->is_saved = is_saved;
			dev_info->role_skip_logged = false;
			dev_info->rssi = rssi;  /* Store RSSI from advertisement */
			sys_slist_append(&device_list, &dev_info->node);
			log("Added device: %s (svc_mask=%d, saved=%d)\n", dev_info->name, dev_info->svc_mask, is_saved);
			print_device_list();
		} else {
			log("ERROR: Failed to allocate memory for device %s\n", dev);
			return;
		}
	} else {
		log("ERROR: found=true but dev_info=NULL for %s\n", dev);
		return;
	}

	/* Attempt connection if device has known services */
	if (dev_info && dev_info->svc_mask != 0 && 
	    dev_info->name[0] != '\0' && strcmp(dev_info->name, dev) != 0) {
		
		/* Check if already connected first to avoid spam */
		bool already_connected = false;
		for (int i = 0; i < MAX_CONNECTIONS; i++) {
			if (connections[i].conn) {
				const bt_addr_le_t *conn_addr = bt_conn_get_dst(connections[i].conn);
				if (!bt_addr_le_cmp(conn_addr, addr)) {
					already_connected = true;
					break;
				}
			}
		}

		/* Saved devices can connect anytime; non-saved only during scan window */
		if (!already_connected && !dev_info->is_saved) {
			if (!scan_window_active) {
				/* Skip non-saved devices outside scan window */
				return;
			}
		}

		if (!already_connected && (taken_roles() & primary_role(dev_info->svc_mask))) {
			if (!dev_info->role_skip_logged) {
				log("Skipping %s: another device already provides its service\n", dev_info->name);
				dev_info->role_skip_logged = true;
			}
			return;
		}

		if (!already_connected) {
			int free_slot = -1;
			for (int i = 0; i < MAX_CONNECTIONS; i++) {
				if (!connections[i].conn) {
					free_slot = i;
					break;
				}
			}

			if (free_slot >= 0) {
				/* BT_LE_CONN_PARAM_DEFAULT uses a 30 ms interval and a
				 * short supervision timeout. On a marginal link (-80 dBm
				 * measured) that expires during momentary fades and tears
				 * the connection down with HCI reason 0x08, which also
				 * aborts the ECG stream mid-batch. Ask for the longest
				 * supervision timeout the spec allows (32 s) so short
				 * fades are ridden out instead of ending the session; the
				 * sensor may counter-propose, which the stack handles. */
				static struct bt_le_conn_param param = {
					.interval_min = 24,   /* 30 ms */
					.interval_max = 40,   /* 50 ms */
					.latency = 0,
					.timeout = 400,       /* 4 s supervision */
				};
				int err;

				/* Clear the slot to ensure clean state for new connection */
				memset(&connections[free_slot].subscribe_params, 0, 
				       sizeof(connections[free_slot].subscribe_params));
				memset(&connections[free_slot].ccc_disc_params, 0,
				       sizeof(connections[free_slot].ccc_disc_params));
				memset(&connections[free_slot].sub_kind, 0,
				       sizeof(connections[free_slot].sub_kind));
				connections[free_slot].subscribe_count = 0;
				connections[free_slot].discover_service_index = 0;
				connections[free_slot].ftms_control_point_handle = 0;
				connections[free_slot].polar_pmd_cp_handle = 0;
				connections[free_slot].movesense_cmd_handle = 0;
				connections[free_slot].temp_value_handle = 0;

				/* Reset the GSP subscribe state for the new connection.
				 * ecg_stream_reset() clears the current references and the
				 * step counter while preserving the previous ones, so the
				 * subscribe sequence can unsubscribe them first. It is safe
				 * to keep them even if a different device takes this slot:
				 * gsp_send_next() only runs for peers that expose the GSP
				 * command characteristic. */
				ecg_stream_reset(&connections[free_slot]);

				err = bt_le_scan_stop();
				if (err) {
					log("Stop LE scan failed (err %d)\n", err);
				} else {
					log("Creating connection to %s (slot %d)\n", dev_info->name, free_slot);
					struct bt_conn_le_create_param create_param = *BT_CONN_LE_CREATE_CONN;
					create_param.options = 0;
					
					err = bt_conn_le_create(&dev_info->addr, &create_param, &param, &connections[free_slot].conn);
					if (err) {
						log("Create connection failed (err %d)\n", err);
						connections[free_slot].conn = NULL;
						start_scan();
					} else {
						pending_conn = bt_conn_ref(connections[free_slot].conn);
						k_work_schedule(&conn_timeout_work, K_SECONDS(10));
					}
				}
			}
		}
	}

	/* Clean up old devices */
	struct device_info *next;
	SYS_SLIST_FOR_EACH_CONTAINER_SAFE(&device_list, dev_info, next, node) {
		bool is_connected = false;
		for (int i = 0; i < MAX_CONNECTIONS; i++) {
			if (connections[i].conn) {
				const bt_addr_le_t *conn_addr = bt_conn_get_dst(connections[i].conn);
				if (!bt_addr_le_cmp(conn_addr, &dev_info->addr)) {
					is_connected = true;
					dev_info->last_seen = now;
					break;
				}
			}
		}
		
		/* Remove devices not seen in 10 seconds, unless connected */
		if (now - dev_info->last_seen > 10000) {
			if (!is_connected) {
				log("Removed device: %s\n", dev_info->name);
				print_device_list();
				sys_slist_find_and_remove(&device_list, &dev_info->node);
				k_free(dev_info);
			}
		}
	}
}

static void _start_scan_internal(void)
{
	int err;

	struct bt_le_scan_param scan_param = {
		.type       = BT_LE_SCAN_TYPE_ACTIVE,
		.options    = BT_LE_SCAN_OPT_CODED,
		.interval   = BT_GAP_SCAN_FAST_INTERVAL,
		.window     = BT_GAP_SCAN_FAST_WINDOW,
	};

	err = bt_le_scan_start(&scan_param, device_found);
	if (err) {
		if (err == -EALREADY) {
			return;
		}
		log("Scanning with Coded PHY support failed (err %d)\n", err);

		log("Scanning without Coded PHY\n");
		scan_param.options &= ~BT_LE_SCAN_OPT_CODED;
		err = bt_le_scan_start(&scan_param, device_found);
		if (err && err != -EALREADY) {
			log("Scanning failed to start (err %d)\n", err);
			return;
		}
	}

	if (scan_window_active) {
		log("Scanning successfully started (window active)\n");
	} else {
		log("Scanning successfully started\n");
	}
}

void start_scan(void)
{
	_start_scan_internal();
}

static void scan_window_timeout_handler(struct k_work *work)
{
	log("Scan window expired - resuming normal scanning\n");
	scan_window_active = false;
	
	/* Update LED feedback for scan window end */
	led_feedback_update();
	
	/* Restart scanning to allow saved devices to reconnect normally */
	start_scan();
}

void start_scan_window(uint32_t duration_ms)
{
	scan_window_active = true;
	log("Scan window started for %u ms\n", duration_ms);
	
	/* Update LED feedback for scan window start */
	led_feedback_update();

	/* Schedule timeout to close window */
	k_work_reschedule_for_queue(&k_sys_work_q, &scan_window_timeout,
	                           K_MSEC(duration_ms));

	/* Start scanning immediately */
	_start_scan_internal();
}

void stop_scan_window(void)
{
	if (scan_window_active) {
		int err = bt_le_scan_stop();
		if (err && err != -EALREADY) {
			log("Failed to stop scan (err %d)\n", err);
		}
		k_work_cancel_delayable(&scan_window_timeout);
		scan_window_active = false;
		
		/* Update LED feedback for scan window stop */
		led_feedback_update();
		
		log("Scan window stopped\n");
	}
}

bool is_scan_window_active(void)
{
	return scan_window_active;
}

void disconnect_all_devices(void)
{
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (connections[i].conn) {
			char addr[BT_ADDR_LE_STR_LEN];
			bt_addr_le_to_str(bt_conn_get_dst(connections[i].conn), addr, sizeof(addr));
			log("Disconnecting device: %s\n", addr);
			bt_conn_disconnect(connections[i].conn, BT_HCI_ERR_REMOTE_USER_TERM_CONN);
		}
	}
}

void save_connected_device(struct bt_conn *conn)
{
	/* Find device in list */
	const bt_addr_le_t *addr = bt_conn_get_dst(conn);
	struct device_info *dev_info;
	
	SYS_SLIST_FOR_EACH_CONTAINER(&device_list, dev_info, node) {
		if (!bt_addr_le_cmp(&dev_info->addr, addr)) {
			/* Save device to NVS if not already saved */
			if (!dev_info->is_saved) {
				int err = nvs_save_device(&dev_info->addr, dev_info->name, dev_info->svc_mask);
				if (err == 0) {
					dev_info->is_saved = true;
					log("Auto-saved connected device: %s\n", dev_info->name);
				} else {
					log("Failed to save device %s (err %d)\n", dev_info->name, err);
				}
			}
			break;
		}
	}
}

void device_manager_init(void)
{
	sys_slist_init(&device_list);
	k_work_init_delayable(&conn_timeout_work, conn_timeout_handler);
	k_work_init_delayable(&scan_window_timeout, scan_window_timeout_handler);
	
	/* Initialize NVS storage */
	int err = nvs_storage_init();
	if (err) {
		log("Failed to initialize NVS storage (err %d)\n", err);
	} else {
		/* Load saved devices */
		struct saved_device saved[MAX_SAVED_DEVICES];
		int count = nvs_load_devices(saved, MAX_SAVED_DEVICES);
		log("Loaded %d saved device(s) from NVS\n", count);
		for (int i = 0; i < count; i++) {
			if (saved[i].valid) {
				char addr_str[BT_ADDR_LE_STR_LEN];
				bt_addr_le_to_str(&saved[i].addr, addr_str, sizeof(addr_str));
				log("  [%d] %s (%s) svc_mask=%d\n", i, saved[i].name, addr_str, saved[i].svc_mask);
			}
		}
	}
	
	/* Always start scanning - saved devices can reconnect anytime */
	start_scan();
}
