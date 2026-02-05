/* ftms_control_point.c - FTMS Control Point handling */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/byteorder.h>
#include <string.h>
#include "common.h"
#include "ftms_control_point.h"
#include "gatt_services.h"

/* Control Point state */
bool ftms_cp_indicate_enabled = false;
bool ftms_cp_indicating = false;

/* Grade to resistance conversion: grade -100 -> 0, grade 1900 -> 100 */
#define GRADE_RESISTANCE(x) MAX(MIN(((x) + 100) / 20, 100), 0)

/* Track if last command was converted from 0x11 to 0x04 */
static bool last_cmd_was_converted = false;

/* Buffer for Control Point indication responses */
static uint8_t ftms_cp_response[20];
static uint16_t ftms_cp_response_len;
static struct bt_gatt_indicate_params ftms_cp_ind_params;

/* Buffer for forwarding commands to trainer */
static uint8_t ftms_cp_write_buf[32];
static struct bt_gatt_write_params ftms_cp_write_params;
static bool ftms_cp_write_busy = false;

/* Work item for sending deferred Control Point responses */
struct k_work ftms_cp_response_work;

const char *ftms_cp_opcode_str(uint8_t opcode)
{
	switch (opcode) {
	case FTMS_CP_REQUEST_CONTROL:        return "Request Control";
	case FTMS_CP_RESET:                  return "Reset";
	case FTMS_CP_SET_TARGET_SPEED:       return "Set Target Speed";
	case FTMS_CP_SET_TARGET_INCLINATION: return "Set Target Inclination";
	case FTMS_CP_SET_TARGET_RESISTANCE:  return "Set Target Resistance";
	case FTMS_CP_SET_TARGET_POWER:       return "Set Target Power";
	case FTMS_CP_SET_TARGET_HEARTRATE:   return "Set Target Heart Rate";
	case FTMS_CP_START_RESUME:           return "Start/Resume";
	case FTMS_CP_STOP_PAUSE:             return "Stop/Pause";
	case FTMS_CP_SET_INDOOR_BIKE_SIM:    return "Set Indoor Bike Simulation";
	case FTMS_CP_RESPONSE_CODE:          return "Response Code";
	default:                             return "Unknown";
	}
}

void ftms_cp_ccc_cfg_changed(const struct bt_gatt_attr *attr, uint16_t value)
{
	ftms_cp_indicate_enabled = (value == BT_GATT_CCC_INDICATE);
	log("[FTMS CP] CCC changed: indications %s\n", 
	       ftms_cp_indicate_enabled ? "enabled" : "disabled");
}

static void ftms_cp_indicate_cb(struct bt_conn *conn,
				struct bt_gatt_indicate_params *params, uint8_t err)
{
	if (err) {
		log("[FTMS CP] Indication failed (err %d)\n", err);
	} else {
		log("[FTMS CP] Indication acknowledged by Zwift\n");
	}
}

static void ftms_cp_indicate_destroy(struct bt_gatt_indicate_params *params)
{
	log("[FTMS CP] Indication complete\n");
	ftms_cp_indicating = false;
}

static void ftms_cp_response_work_handler(struct k_work *work)
{
	if (peripheral_conn && ftms_cp_indicate_enabled && !ftms_cp_indicating) {
		/* Use Attribute 11 (Control Point Value) for indication */
		ftms_cp_ind_params.attr = &ftms_svc.attrs[11];
		ftms_cp_ind_params.func = ftms_cp_indicate_cb;
		ftms_cp_ind_params.destroy = ftms_cp_indicate_destroy;
		ftms_cp_ind_params.data = ftms_cp_response;
		ftms_cp_ind_params.len = ftms_cp_response_len;
		
		int err = bt_gatt_indicate(peripheral_conn, &ftms_cp_ind_params);
		if (err) {
			log("[FTMS CP] Failed to send indication (err %d)\n", err);
		} else {
			ftms_cp_indicating = true;
			log("[FTMS CP] Indication sent, waiting for ACK\n");
		}
	}
}

static void ftms_cp_write_cb(struct bt_conn *conn, uint8_t err,
			     struct bt_gatt_write_params *params)
{
	ftms_cp_write_busy = false;
	
	if (err) {
		log("[FTMS CP] Forwarding to trainer failed (err %u)\n", err);
	} else {
		log("[FTMS CP] Forwarding to trainer complete\n");
	}
}

ssize_t ftms_control_point_write(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				 const void *buf, uint16_t len, uint16_t offset, uint8_t flags)
{
	const uint8_t *cmd = buf;
	char addr[BT_ADDR_LE_STR_LEN];

	if (offset != 0) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_OFFSET);
	}

	if (len < 1) {
		return BT_GATT_ERR(BT_ATT_ERR_INVALID_ATTRIBUTE_LEN);
	}

	bt_addr_le_to_str(bt_conn_get_dst(conn), addr, sizeof(addr));

	/* Keep debug log for all commands */
	log("[FTMS CP] Zwift (%s) -> %s (0x%02x)\n", 
	       addr, ftms_cp_opcode_str(cmd[0]), cmd[0]);

	/* Store peripheral connection for sending responses back */
	if (!peripheral_conn) {
		peripheral_conn = bt_conn_ref(conn);
		log("[FTMS CP] Stored peripheral connection\n");
	}

	/* Convert Set Indoor Bike Simulation (0x11) to Set Target Resistance (0x04) */
	uint8_t converted_cmd[32];
	const uint8_t *forward_cmd = cmd;
	uint16_t forward_len = len;
	last_cmd_was_converted = false;
	
	if (cmd[0] == FTMS_CP_SET_INDOOR_BIKE_SIM && len >= 5) {
		int16_t wind_speed = sys_le16_to_cpu(*(uint16_t *)&cmd[1]);
		int16_t grade = sys_le16_to_cpu(*(uint16_t *)&cmd[3]);
		
		/* Convert grade (0.01% units) to resistance (unitless 0-100) */
		int16_t resistance = GRADE_RESISTANCE(grade);
		
		/* Build Set Target Resistance command */
		converted_cmd[0] = FTMS_CP_SET_TARGET_RESISTANCE;
		converted_cmd[1] = (uint8_t)resistance;
		forward_cmd = converted_cmd;
		forward_len = 2;
		last_cmd_was_converted = true;
		
		/* Log conversion */
		uint32_t now = k_uptime_get_32();
		json_out("{\"type\":\"sim\",\"ts\":%u,\"wind_speed\":%d,\"grade\":%d,\"resistance\":%d}\n",
		       now, wind_speed, grade, resistance);
		log("[FTMS CP] Converted 0x11 (grade=%d) -> 0x04 (resistance=%d)\n", grade, resistance);
	}

	/* Find trainer connection with FTMS Control Point */
	struct conn_slot *trainer_slot = NULL;
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (connections[i].conn && connections[i].ftms_control_point_handle != 0) {
			trainer_slot = &connections[i];
			log("[FTMS CP] Found trainer at slot %d, handle=%u\n", i, connections[i].ftms_control_point_handle);
			break;
		}
	}

	if (!trainer_slot) {
		char slots_str[64];
		int pos = 0;
		for (int i = 0; i < MAX_CONNECTIONS; i++) {
			if (connections[i].conn) {
				pos += snprintf(slots_str + pos, sizeof(slots_str) - pos, "%d:handle=%u ", i, connections[i].ftms_control_point_handle);
			}
		}
		log("[FTMS CP] ERROR: No trainer connection found (slots: %s)\n", slots_str);
		return len;
	}

	/* Forward command to trainer */
	if (len > sizeof(ftms_cp_write_buf)) {
		log("[FTMS CP] Error: Command too long (%u)\n", len);
		return len;
	}
	
	/* Check if a write is already in progress */
	if (ftms_cp_write_busy) {
		log("[FTMS CP] Write busy, dropping command\n");
		return len;
	}

	memcpy(ftms_cp_write_buf, forward_cmd, forward_len);
			
	ftms_cp_write_params.func = ftms_cp_write_cb;
	ftms_cp_write_params.handle = trainer_slot->ftms_control_point_handle;
	ftms_cp_write_params.offset = 0;
	ftms_cp_write_params.data = ftms_cp_write_buf;
	ftms_cp_write_params.length = forward_len;

	ftms_cp_write_busy = true;
	int err = bt_gatt_write(trainer_slot->conn, &ftms_cp_write_params);
	if (err) {
		ftms_cp_write_busy = false;
		log("[FTMS CP] Write to trainer failed (err %d)\n", err);
	} else {
		char hex_str[96];
		int pos = 0;
		for (int i = 0; i < forward_len && pos < sizeof(hex_str) - 3; i++) {
			pos += snprintf(hex_str + pos, sizeof(hex_str) - pos, "%02x ", forward_cmd[i]);
		}
		log("[FTMS CP] Forwarded to trainer [%u bytes]: %s\n", forward_len, hex_str);
	}

	return len;
}

uint8_t ftms_cp_indicate_func(struct bt_conn *conn,
			      struct bt_gatt_subscribe_params *params,
			      const void *data, uint16_t length)
{
	if (!data) {
		log("[FTMS CP] Indication unsubscribed\n");
		params->value_handle = 0U;
		return BT_GATT_ITER_STOP;
	}

	const uint8_t *response = data;

	/* Log trainer response */
	char hex_str[64];
	int pos = 0;
	for (int i = 0; i < length && pos < sizeof(hex_str) - 3; i++) {
		pos += snprintf(hex_str + pos, sizeof(hex_str) - pos, "%02x ", response[i]);
	}
	log("[FTMS CP] Trainer response [%u bytes]: %s\n", length, hex_str);

	if (length >= 3 && response[0] == FTMS_CP_RESPONSE_CODE) {
		uint8_t req_opcode = response[1];
		uint8_t result = response[2];
		log("[FTMS CP] Response to %s: %s\n",
		       ftms_cp_opcode_str(req_opcode),
		       result == 0x01 ? "Success" : result == 0x02 ? "Not Supported" :
		       result == 0x03 ? "Invalid Parameter" : result == 0x04 ? "Failed" : "Unknown");
	}

	/* Forward indication back to Zwift if connected */
	if (peripheral_conn && ftms_cp_indicate_enabled) {
		if (length > sizeof(ftms_cp_response)) {
			log("[FTMS CP] Response too long (%u), truncating\n", length);
			length = sizeof(ftms_cp_response);
		}
		ftms_cp_response_len = length;
		memcpy(ftms_cp_response, data, length);
		
		/* Convert response opcode from 0x04 back to 0x11 if needed */
		if (last_cmd_was_converted && ftms_cp_response_len >= 3 && 
		    ftms_cp_response[0] == FTMS_CP_RESPONSE_CODE && 
		    ftms_cp_response[1] == FTMS_CP_SET_TARGET_RESISTANCE) {
			ftms_cp_response[1] = FTMS_CP_SET_INDOOR_BIKE_SIM;
			log("[FTMS CP] Converted response 0x04 -> 0x11 for Zwift\n");
			last_cmd_was_converted = false;
		}
		
		k_work_submit(&ftms_cp_response_work);
		log("[FTMS CP] Queued response for forwarding\n");
	} else if (peripheral_conn && !ftms_cp_indicate_enabled) {
		log("[FTMS CP] Cannot send indication - CCC not configured\n");
	}

	return BT_GATT_ITER_CONTINUE;
}

void ftms_control_point_init(void)
{
	k_work_init(&ftms_cp_response_work, ftms_cp_response_work_handler);
}
