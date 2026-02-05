/* common.h - Shared definitions and structures */

#ifndef COMMON_H_
#define COMMON_H_

#include <zephyr/types.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/conn.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/slist.h>
#include <zephyr/kernel.h>

/* Mutex for serial output synchronization */
extern struct k_mutex serial_output_mutex;

/* Define log macro using printf with timestamps - thread-safe */
#define log(fmt, ...) \
	do { \
		uint32_t _ms = k_uptime_get_32(); \
		k_mutex_lock(&serial_output_mutex, K_FOREVER); \
		printf("[%u.%u] " fmt, _ms / 1000, (_ms % 1000) / 100, ##__VA_ARGS__); \
		k_mutex_unlock(&serial_output_mutex); \
	} while(0)

/* Define json_out macro for JSON output - thread-safe */
#define json_out(fmt, ...) \
	do { \
		k_mutex_lock(&serial_output_mutex, K_FOREVER); \
		printf(fmt, ##__VA_ARGS__); \
		k_mutex_unlock(&serial_output_mutex); \
	} while(0)

#define VERSION "1.15"

#define MAX_CONNECTIONS 3  /* HR, Power Meter, Trainer */
#define MAX_SUBSCRIPTIONS_PER_CONN 5  /* Trainer needs: Indoor Bike Data, Training Status, Machine Status, Control Point */
#define MAX_SAVED_DEVICES 4
#define EXCLUSIVE_WINDOW_MS (6 * 60 * 1000)  /* 6 minutes */

/* Saved device structure for NVS persistence */
struct saved_device {
	bt_addr_le_t addr;
	char name[32];
	uint8_t svc_mask;
	uint8_t valid;  /* 1 if slot contains valid data, 0 if empty */
};

/* Device info structure for tracking discovered devices */
struct device_info {
	sys_snode_t node;
	bt_addr_le_t addr;
	char name[32];
	uint32_t last_seen;
	uint8_t svc_mask;
	bool is_saved;  /* true if this device was previously saved */
	int8_t rssi;    /* Last observed RSSI from scanning */
};

/* Connection slot structure */
struct conn_slot {
	struct bt_conn *conn;
	struct bt_uuid_16 discover_uuid;
	struct bt_gatt_discover_params discover_params;
	struct bt_gatt_subscribe_params subscribe_params[MAX_SUBSCRIPTIONS_PER_CONN];  /* Embedded storage */
	int service_type[MAX_SUBSCRIPTIONS_PER_CONN]; /* 0=HR, 1=CP, 2=FTMS */
	int subscribe_count;
	int discover_service_index;
	uint16_t ftms_control_point_handle;
	struct bt_gatt_indicate_params indicate_params;
	uint16_t temp_value_handle;
	int8_t rssi;  /* Last known RSSI */
};

/* Global connection slots */
extern struct conn_slot connections[MAX_CONNECTIONS];

/* Peripheral (Zwift) connection */
extern struct bt_conn *peripheral_conn;

/* Services to discover */
extern const struct bt_uuid *discover_services[];
extern const int discover_service_count;

/* Power meter tracking */
extern uint32_t last_cp_data_time;
#define CP_TIMEOUT_MS 5000

/* Cached CP data for injection into FTMS */
struct cp_cache {
	int16_t power;
	uint16_t cadence;  /* in 0.5 rpm units (same as FTMS) */
	uint32_t timestamp;
	uint16_t last_crank_revs;
	uint16_t last_crank_time;  /* sensor time in 1/1024 second units */
	uint32_t last_crank_change_time;  /* wall clock timestamp of last crank revolution change */
	bool valid;
};
extern struct cp_cache cached_cp_data;

/* Statistics */
extern uint64_t total_rx_count;

#endif /* COMMON_H_ */
