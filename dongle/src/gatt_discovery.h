/* gatt_discovery.h - GATT service/characteristic discovery */

#ifndef GATT_DISCOVERY_H_
#define GATT_DISCOVERY_H_

#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/conn.h>

/* Discovery callback */
uint8_t discover_func(struct bt_conn *conn,
		      const struct bt_gatt_attr *attr,
		      struct bt_gatt_discover_params *params);

void start_discovery(struct bt_conn *conn, int slot_idx);

#endif /* GATT_DISCOVERY_H_ */
