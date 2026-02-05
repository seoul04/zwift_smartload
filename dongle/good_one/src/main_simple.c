/* Simplified Heart Rate Central - Focus on reconnection */

#include <zephyr/types.h>
#include <stddef.h>
#include <errno.h>
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/hci.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/uuid.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/byteorder.h>

/* Simple timestamped printk */
#define _printk_orig printk
#undef printk
#define printk(fmt, ...) \
	do { \
		uint32_t _ms = k_uptime_get_32(); \
		_printk_orig("[%u.%u] " fmt, _ms / 1000, (_ms % 1000) / 100, ##__VA_ARGS__); \
	} while(0)

/* Configuration: 
 * 1 = Broken (reuses same memory)
 * 2 = Works (alternating slots)
 * 10 = Works (ring buffer - proves scalability)
 */
#define NUM_SUBSCRIPTION_SLOTS 10

/* Single connection state */
static struct bt_conn *hr_conn = NULL;
static struct bt_gatt_discover_params discover_params;
/* Ring buffer of subscription parameters - each subscription gets fresh memory */
static struct bt_gatt_subscribe_params subscribe_params[NUM_SUBSCRIPTION_SLOTS];
static int next_subscribe_slot = 0;
static struct bt_uuid_16 discover_uuid;
static uint16_t hr_value_handle = 0;

/* Forward declarations */
static void start_scan(void);
static uint8_t discover_cb(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			   struct bt_gatt_discover_params *params);
static uint8_t notify_cb(struct bt_conn *conn, struct bt_gatt_subscribe_params *params,
			const void *data, uint16_t length);

static uint8_t notify_cb(struct bt_conn *conn, struct bt_gatt_subscribe_params *params,
			const void *data, uint16_t length)
{
	if (!data) {
		printk("[UNSUBSCRIBED]\n");
		return BT_GATT_ITER_STOP;
	}

	const uint8_t *hr_data = data;
	if (length < 2) {
		printk("Invalid HR data length: %u\n", length);
		return BT_GATT_ITER_CONTINUE;
	}

	uint8_t flags = hr_data[0];
	uint8_t hr_format = flags & 0x01;
	uint16_t heart_rate;

	if (hr_format == 0) {
		heart_rate = hr_data[1];
	} else {
		if (length < 3) {
			printk("Invalid HR data length for UINT16: %u\n", length);
			return BT_GATT_ITER_CONTINUE;
		}
		heart_rate = sys_le16_to_cpu(*(uint16_t *)&hr_data[1]);
	}

	printk("Heart Rate: %u bpm\n", heart_rate);
	return BT_GATT_ITER_CONTINUE;
}

static uint8_t discover_cb(struct bt_conn *conn, const struct bt_gatt_attr *attr,
			   struct bt_gatt_discover_params *params)
{
	int err;

	if (!attr) {
		printk("Discovery complete\n");
		memset(params, 0, sizeof(*params));
		start_scan();
		return BT_GATT_ITER_STOP;
	}

	printk("[ATTR] handle %u\n", attr->handle);

	if (params->type == BT_GATT_DISCOVER_PRIMARY) {
		/* Found HR Service, now discover characteristics */
		discover_params.uuid = NULL;
		discover_params.start_handle = attr->handle + 1;
		discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;

		err = bt_gatt_discover(conn, &discover_params);
		if (err) {
			printk("Discover failed (err %d)\n", err);
		}
	} else if (params->type == BT_GATT_DISCOVER_CHARACTERISTIC) {
		/* Found a characteristic, check if it's HR Measurement (0x2A37) */
		const struct bt_gatt_chrc *chrc = attr->user_data;
		uint16_t char_uuid = BT_UUID_16(chrc->uuid)->val;

		if (char_uuid == 0x2A37) {
			/* HR Measurement characteristic found */
			hr_value_handle = bt_gatt_attr_value_handle(attr);
			printk("HR Measurement found at handle %u\n", hr_value_handle);

			/* Discover CCC descriptor */
			memcpy(&discover_uuid, BT_UUID_GATT_CCC, sizeof(discover_uuid));
			discover_params.uuid = &discover_uuid.uuid;
			discover_params.start_handle = attr->handle + 2;
			discover_params.type = BT_GATT_DISCOVER_DESCRIPTOR;

			err = bt_gatt_discover(conn, &discover_params);
			if (err) {
				printk("Discover CCC failed (err %d)\n", err);
			}
		} else {
			/* Not HR Measurement, keep looking */
			discover_params.start_handle = attr->handle + 1;
			err = bt_gatt_discover(conn, &discover_params);
			if (err) {
				printk("Discover failed (err %d)\n", err);
			}
		}
	} else if (params->type == BT_GATT_DISCOVER_DESCRIPTOR) {
		/* Found CCC, subscribe to notifications */
		printk("CCC found at handle %u\n", attr->handle);

		/* Get next slot from ring buffer */
		int slot = next_subscribe_slot;
		next_subscribe_slot = (next_subscribe_slot + 1) % NUM_SUBSCRIPTION_SLOTS;
		printk("Using subscription slot %d\n", slot);
		
		memset(&subscribe_params[slot], 0, sizeof(subscribe_params[slot]));
		subscribe_params[slot].notify = notify_cb;
		subscribe_params[slot].value = BT_GATT_CCC_NOTIFY;
		subscribe_params[slot].value_handle = hr_value_handle;
		subscribe_params[slot].ccc_handle = attr->handle;

		err = bt_gatt_subscribe(conn, &subscribe_params[slot]);
		if (err && err != -EALREADY) {
			printk("Subscribe failed (err %d)\n", err);
		} else {
			printk("[SUBSCRIBED] to HR notifications\n");
		}

		/* Don't resume scanning here - let discovery complete */
		return BT_GATT_ITER_STOP;
	}

	return BT_GATT_ITER_STOP;
}

static void connected(struct bt_conn *conn, uint8_t err)
{
	char addr[BT_ADDR_LE_STR_LEN];
	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

	if (err) {
		printk("Failed to connect to %s (err %u)\n", addr, err);
		bt_conn_unref(conn);
		hr_conn = NULL;
		start_scan();
		return;
	}

	printk("Connected: %s\n", addr);
	hr_conn = conn;

	/* Start service discovery for Heart Rate Service */
	memcpy(&discover_uuid, BT_UUID_HRS, sizeof(discover_uuid));
	discover_params.uuid = &discover_uuid.uuid;
	discover_params.func = discover_cb;
	discover_params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
	discover_params.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE;
	discover_params.type = BT_GATT_DISCOVER_PRIMARY;

	err = bt_gatt_discover(conn, &discover_params);
	if (err) {
		printk("Discover failed (err %d)\n", err);
		start_scan();
	}
}

static void disconnected(struct bt_conn *conn, uint8_t reason)
{
	char addr[BT_ADDR_LE_STR_LEN];
	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

	printk("Disconnected: %s (reason 0x%02x)\n", addr, reason);

	if (hr_conn == conn) {
		bt_conn_unref(hr_conn);
		hr_conn = NULL;
		hr_value_handle = 0;
		
		/* Clear discovery params and subscription params to ensure clean state */
		memset(&discover_params, 0, sizeof(discover_params));
		memset(&subscribe_params[0], 0, sizeof(subscribe_params[0]));
		memset(&subscribe_params[1], 0, sizeof(subscribe_params[1]));
	}

	/* Resume scanning */
	start_scan();
}

BT_CONN_CB_DEFINE(conn_callbacks) = {
	.connected = connected,
	.disconnected = disconnected,
};

static void device_found(const bt_addr_le_t *addr, int8_t rssi, uint8_t type,
			 struct net_buf_simple *ad)
{
	char addr_str[BT_ADDR_LE_STR_LEN];
	struct bt_le_conn_param *param;
	int err;

	/* Already connected */
	if (hr_conn) {
		return;
	}

	/* Check if this is a Heart Rate device by looking for HR Service UUID */
	bool has_hrs = false;
	while (ad->len > 0) {
		uint8_t field_len = net_buf_simple_pull_u8(ad);
		if (field_len == 0) break;

		uint8_t field_type = net_buf_simple_pull_u8(ad);
		field_len--;

		if (field_type == BT_DATA_UUID16_ALL || field_type == BT_DATA_UUID16_SOME) {
			while (field_len >= 2) {
				uint16_t uuid = net_buf_simple_pull_le16(ad);
				field_len -= 2;
				if (uuid == 0x180D) { /* Heart Rate Service */
					has_hrs = true;
					break;
				}
			}
		} else {
			net_buf_simple_pull(ad, field_len);
		}

		if (has_hrs) break;
	}

	if (!has_hrs) {
		return;
	}

	bt_addr_le_to_str(addr, addr_str, sizeof(addr_str));
	printk("HR device found: %s (RSSI %d)\n", addr_str, rssi);

	/* Stop scanning before connecting */
	err = bt_le_scan_stop();
	if (err) {
		printk("Stop scan failed (err %d)\n", err);
		return;
	}

	/* Connect */
	param = BT_LE_CONN_PARAM_DEFAULT;
	err = bt_conn_le_create(addr, BT_CONN_LE_CREATE_CONN, param, &hr_conn);
	if (err) {
		printk("Create conn failed (err %d)\n", err);
		hr_conn = NULL;
		start_scan();
	}
}

static void start_scan(void)
{
	int err;
	struct bt_le_scan_param scan_param = {
		.type = BT_LE_SCAN_TYPE_ACTIVE,
		.options = BT_LE_SCAN_OPT_NONE,
		.interval = BT_GAP_SCAN_FAST_INTERVAL,
		.window = BT_GAP_SCAN_FAST_WINDOW,
	};

	err = bt_le_scan_start(&scan_param, device_found);
	if (err && err != -EALREADY) {
		printk("Scan failed to start (err %d)\n", err);
		return;
	}

	printk("Scanning...\n");
}

int main(void)
{
	int err;

	err = bt_enable(NULL);
	if (err) {
		printk("Bluetooth init failed (err %d)\n", err);
		return 0;
	}

	printk("Simple HR Central started\n");
	start_scan();

	return 0;
}
