/* ftms_control_point.h - FTMS Control Point handling */

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

/* Control Point state */
extern bool ftms_cp_indicate_enabled;
extern bool ftms_cp_indicating;
extern struct k_work ftms_cp_response_work;

/* Functions */
void ftms_control_point_init(void);
const char *ftms_cp_opcode_str(uint8_t opcode);

/* GATT callbacks */
void ftms_cp_ccc_cfg_changed(const struct bt_gatt_attr *attr, uint16_t value);
ssize_t ftms_control_point_write(struct bt_conn *conn, const struct bt_gatt_attr *attr,
				 const void *buf, uint16_t len, uint16_t offset, uint8_t flags);

/* Indication callback for Control Point responses from trainer */
uint8_t ftms_cp_indicate_func(struct bt_conn *conn,
			      struct bt_gatt_subscribe_params *params,
			      const void *data, uint16_t length);

#endif /* FTMS_CONTROL_POINT_H_ */
