/* notification_handler.h - Notification callback handlers */

#ifndef NOTIFICATION_HANDLER_H_
#define NOTIFICATION_HANDLER_H_

#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/conn.h>

/* Main notification callback */
uint8_t notify_func(struct bt_conn *conn,
		    struct bt_gatt_subscribe_params *params,
		    const void *data, uint16_t length);

#endif /* NOTIFICATION_HANDLER_H_ */
