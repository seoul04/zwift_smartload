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

/* Groups several json_out() calls into one contiguous record by holding the
 * output mutex across them. k_mutex is recursive, so the json_out() calls in
 * between re-take it safely. */
#define json_begin() k_mutex_lock(&serial_output_mutex, K_FOREVER)
#define json_end() k_mutex_unlock(&serial_output_mutex)

#define VERSION "1.21"

#define DEVICE_NAME_PREFIX "Z-Relay"

/* Dongle name reported to z_relay_server, e.g. "Z-Relay-3ED5" */
extern char device_name_buffer[32];

/* Print the {"type":"info"} identity record: the dongle's Bluetooth name,
 * address and version. Emitted once at boot and again whenever z_relay_server
 * asks with {"cmd":"info"}, so a server that starts after the dongle still
 * learns the identity instead of falling back to its own host address. */
void announce_identity(void);

#define MAX_CONNECTIONS 3  /* HR, Power Meter, Trainer */
/* Trainer needs: Indoor Bike Data, Training Status, Machine Status, Control Point.
 * An HR strap adds either a Polar PMD data subscription or a Movesense GSP one. */
#define MAX_SUBSCRIPTIONS_PER_CONN 7
#define MAX_SAVED_DEVICES 4

/* Largest ECG batch either strap sends in one frame (Polar H10 sends 73). */
#define ECG_MAX_SAMPLES 80

/* Room for one complete ECG record: header, ECG_MAX_SAMPLES signed samples, and
 * the trailer. A sample is at most "-2147483648" (11 chars) plus a separator, so
 * 80 * 12 + 64 header/trailer leaves comfortable headroom. */
#define ECG_RECORD_MAX (ECG_MAX_SAMPLES * 12 + 64)

/* GSP continuation buffer: one SBEM frame, comfortably above a 125 Hz ECG packet. */
#define GSP_BUF_SIZE 200

/* What a subscription delivers, decided by the characteristic UUID rather than by
 * which service it came from: PMD and GSP each contribute characteristics from
 * inside a 128-bit service, so a service index cannot identify them. */
enum sub_kind {
	SUB_NONE = 0,
	SUB_HR_MEASUREMENT,      /* 0x2A37  HRS  */
	SUB_CP_MEASUREMENT,      /* 0x2A63  CPS  */
	SUB_FTMS_BIKE_DATA,      /* 0x2AD2  FTMS */
	SUB_FTMS_TRAINING,       /* 0x2AD3  FTMS */
	SUB_FTMS_STATUS,         /* 0x2ADA  FTMS */
	SUB_FTMS_CONTROL_POINT,  /* 0x2AD9  FTMS, indicate */
	SUB_POLAR_PMD_DATA,      /* FB005C82-02E7-F387-1CAD-8ACD2D8DF0C8 */
	SUB_MOVESENSE_GSP_DATA,  /* 34800002-7185-4d5d-b431-630e7050e8f0 */
	SUB_POLAR_PMD_CONTROL,   /* FB005C81-02E7-F387-1CAD-8ACD2D8DF0C8, indicate */
};

/* One primary service found by the enumeration walk. */
#define ENUM_MAX_SERVICES 12
#define ENUM_MAX_CHRCS 8

struct enum_svc_entry {
	uint16_t start_handle;
	uint16_t end_handle;
	struct bt_uuid_128 uuid;
};

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
	bool has_battery_service;
	int8_t battery_level;  /* -1 if unknown, else 0..100 */
	bool is_saved;  /* true if this device was previously saved */
	int8_t rssi;    /* Last observed RSSI from scanning */
	bool role_skip_logged;
};

/* Connection slot structure */
struct conn_slot {
	struct bt_conn *conn;
	/* Points at a static UUID constant. It used to be an embedded bt_uuid_16,
	 * which memcpy truncated to 4 bytes and so could never match a 128-bit
	 * service. */
	const struct bt_uuid *discover_uuid;
	struct bt_gatt_discover_params discover_params;
	struct bt_gatt_subscribe_params subscribe_params[MAX_SUBSCRIPTIONS_PER_CONN];  /* Embedded storage */
	/* CONFIG_BT_GATT_AUTO_DISCOVER_CCC needs a discover_params pointer in each
	 * subscribe_params, plus a non-zero end_handle: bt_gatt_subscribe asserts on
	 * "end_handle && disc_params" and without them the CCC lookup returns
	 * -EINVAL. */
	struct bt_gatt_discover_params ccc_disc_params[MAX_SUBSCRIPTIONS_PER_CONN];
	/* Full primary-service walk (uuid == NULL) that runs before the targeted
	 * per-service discovery. It uses READ_BY_GROUP_TYPE, so unlike the filtered
	 * FIND_TYPE walk it cannot be confused with an ATT error response. */
	struct bt_gatt_discover_params enum_params;
	uint8_t enum_active;
	int enum_count;
	/* Services seen by the walk, so their characteristics can be dumped after. */
	struct enum_svc_entry enum_svcs[ENUM_MAX_SERVICES];
	int enum_svc_count;
	int enum_svc_idx;
	struct bt_gatt_discover_params enum_chrc_params;
	enum sub_kind sub_kind[MAX_SUBSCRIPTIONS_PER_CONN];
	int subscribe_count;
	int discover_service_index;  /* index into discover_services[] */
	int cur_service;             /* which service the characteristic walk is inside */
	uint16_t ftms_control_point_handle;
	uint16_t polar_pmd_cp_handle;   /* PMD control point: ECG start is written here */
	uint16_t movesense_cmd_handle;  /* GSP command characteristic */
	struct bt_gatt_write_params write_params;
	uint8_t gsp_ref_ecg;            /* GSP client reference for the ECG stream */
	uint8_t gsp_ref_hr;             /* GSP client reference for /Meas/HR */
	uint8_t gsp_ecg_started;
	uint32_t ecg_dropped;           /* ECG records discarded when the console could not take them */
	/* GSP DATA_PART2 reassembly */
	uint8_t gsp_buf[GSP_BUF_SIZE];
	uint16_t gsp_len;
	uint8_t gsp_ref;
	bool gsp_pending;
	/* ECG sample clock: anchored on the first packet, then advanced by the
	 * sample period so BLE arrival jitter never reaches the trace. */
	uint32_t ecg_t0_ms;
	uint8_t ecg_clock_valid;
	uint8_t hr_rr_misses;   /* consecutive HRS packets without RR, for the GSP fallback */
	uint8_t mtu_logged;
	struct bt_gatt_indicate_params indicate_params;
	uint16_t temp_value_handle;
	struct bt_uuid_16 battery_uuid;
	struct bt_gatt_discover_params battery_discover_params;
	struct bt_gatt_read_params battery_read_params;
	int8_t rssi;  /* Last known RSSI */
};

/* Global connection slots */
extern struct conn_slot connections[MAX_CONNECTIONS];

/* Vendor-specific services, needed for raw ECG. */
extern const struct bt_uuid_128 pmd_service_uuid;   /* Polar Measurement Data */
extern const struct bt_uuid_128 gsp_service_uuid;   /* Movesense GATT SensorData */

/* Polar PMD characteristics */
extern const struct bt_uuid_128 pmd_control_point_uuid;
extern const struct bt_uuid_128 pmd_data_uuid;

/* Movesense GSP characteristics */
extern const struct bt_uuid_128 gsp_command_uuid;
extern const struct bt_uuid_128 gsp_data_uuid;

/* Services to discover */
extern const struct bt_uuid *discover_services[];
extern const int discover_service_count;

/* Power meter tracking */
extern uint32_t last_cp_data_time;

/* Power meter crank counters, for cadence */
struct cp_cache {
	uint16_t cadence;  /* in 0.5 rpm units (same as FTMS) */
	uint16_t last_crank_revs;
	uint16_t last_crank_time;  /* sensor time in 1/1024 second units */
	uint32_t last_crank_change_time;  /* wall clock timestamp of last crank revolution change */
	bool valid;
};
extern struct cp_cache cached_cp_data;

/* Statistics */
extern uint64_t total_rx_count;

#endif /* COMMON_H_ */
