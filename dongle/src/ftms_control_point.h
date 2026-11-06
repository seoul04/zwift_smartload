/* ftms_control_point.h - FTMS Control Point commands from z_relay_server, forwarded to the trainer */

#ifndef FTMS_CONTROL_POINT_H_
#define FTMS_CONTROL_POINT_H_

#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/conn.h>

/* FTMS Control Point Op Codes */
#define FTMS_CP_REQUEST_CONTROL           0x00
#define FTMS_CP_RESET                     0x01
#define FTMS_CP_SET_TARGET_SPEED          0x02
#define FTMS_CP_SET_TARGET_INCLINATION    0x03
#define FTMS_CP_SET_TARGET_RESISTANCE     0x04
#define FTMS_CP_SET_TARGET_POWER          0x05
#define FTMS_CP_SET_TARGET_HEARTRATE      0x06
#define FTMS_CP_START_RESUME              0x07
#define FTMS_CP_STOP_PAUSE                0x08
#define FTMS_CP_SET_INDOOR_BIKE_SIM       0x11
#define FTMS_CP_RESPONSE_CODE             0x80

/* FTMS Control Point result codes */
#define FTMS_CP_RESULT_SUCCESS            0x01
#define FTMS_CP_RESULT_NOT_SUPPORTED      0x02
#define FTMS_CP_RESULT_INVALID_PARAMETER  0x03
#define FTMS_CP_RESULT_FAILED             0x04

const char *ftms_cp_opcode_str(uint8_t opcode);

/* Convert (0x11 -> 0x04) and forward a Control Point command to the trainer.
 * Returns 0 if the write to the trainer was started, negative errno otherwise.
 * The trainer's response is printed as a "cp_resp" JSON line, and failures are
 * reported the same way with result FTMS_CP_RESULT_FAILED. */
int ftms_cp_handle_command(const uint8_t *cmd, uint16_t len);

/* Indication callback for Control Point responses from trainer */
uint8_t ftms_cp_indicate_func(struct bt_conn *conn,
			      struct bt_gatt_subscribe_params *params,
			      const void *data, uint16_t length);

#endif /* FTMS_CONTROL_POINT_H_ */
