/* ftms_control_point.c - FTMS Control Point handling */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/byteorder.h>
#include <string.h>
#include "common.h"
#include "ftms_control_point.h"
#include "gatt_services.h"
#include "grade_limiter.h"
#include "grade_limiter.h"

/* Control Point state */
bool ftms_cp_indicate_enabled = false;
bool ftms_cp_indicating = false;

/* Buffer for Control Point indication responses */
static uint8_t ftms_cp_response[20];
static uint16_t ftms_cp_response_len;
static struct bt_gatt_indicate_params ftms_cp_ind_params;

/* Buffer for forwarding commands to trainer */
static uint8_t ftms_cp_write_buf[32];
static struct bt_gatt_write_params ftms_cp_write_params;

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

	if (cmd[0] == FTMS_CP_REQUEST_CONTROL) {
		log("[FTMS CP] Forwarding Request Control to trainer\n");
	}

	/* Apply grade limiting and log Set Indoor Bike Simulation */
	uint32_t now = k_uptime_get_32();
	if (cmd[0] == FTMS_CP_SET_INDOOR_BIKE_SIM && len >= 5) {
		extern uint16_t last_ftms_speed_mh;
		extern int16_t last_applied_grade;
		
		int16_t wind_speed = sys_le16_to_cpu(*(uint16_t *)&cmd[1]);
		int16_t requested_grade = sys_le16_to_cpu(*(uint16_t *)&cmd[3]);
		
		/* TODO: Grade limiting disabled for now - will revisit later
		int16_t applied_grade;
		bool limited = grade_limiter_apply(last_ftms_speed_mh, requested_grade, &applied_grade);
		
		// Store applied grade for thermal learning
		last_applied_grade = applied_grade;
		*/
		
		/* Quick test: clip grade to maximum of 3.00% (300 in 0.01% units) */
		int16_t applied_grade = requested_grade;
		bool limited = false;
		if (applied_grade > 200) {
			applied_grade = 200;
			limited = true;
		}
		last_applied_grade = applied_grade;
		
		/* Log simulation parameters with limiting info */
		json_out("{\"type\":\"sim\",\"ts\":%u,\"wind_speed\":%d,\"grade\":%d", 
		       now, wind_speed, requested_grade);
		if (len >= 6) {
			uint8_t crr = cmd[5];
			json_out(",\"crr\":%u", crr);
		}
		if (len >= 7) {
			uint8_t cw = cmd[6];
			json_out(",\"cw\":%u", cw);
		}

		json_out(",\"applied_grade\":%d", applied_grade);
		
		/* Apply the limited grade to the command buffer */
		if (limited) {
			uint16_t grade_le = sys_cpu_to_le16(applied_grade);
			memcpy((uint8_t *)&cmd[3], &grade_le, 2);
		}
		
		/* TODO: Grade modification disabled for now
		if (limited) {
			// Modify the command buffer with limited grade
			uint16_t grade_le = sys_cpu_to_le16(applied_grade);
			memcpy((uint8_t *)&cmd[3], &grade_le, 2);
		}
		*/
		
		json_out("}\n");
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

	memcpy(ftms_cp_write_buf, cmd, len);
			
	ftms_cp_write_params.func = ftms_cp_write_cb;
	ftms_cp_write_params.handle = trainer_slot->ftms_control_point_handle;
	ftms_cp_write_params.offset = 0;
	ftms_cp_write_params.data = ftms_cp_write_buf;
	ftms_cp_write_params.length = len;

	int err = bt_gatt_write(trainer_slot->conn, &ftms_cp_write_params);
	if (err) {
		log("[FTMS CP] Write to trainer failed (err %d)\n", err);
	} else {
		char hex_str[96];
		int pos = 0;
		for (int i = 0; i < len && pos < sizeof(hex_str) - 3; i++) {
			pos += snprintf(hex_str + pos, sizeof(hex_str) - pos, "%02x ", cmd[i]);
		}
		log("[FTMS CP] Forwarded to trainer [%u bytes]: %s\n", len, hex_str);
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
	grade_limiter_init();
}
