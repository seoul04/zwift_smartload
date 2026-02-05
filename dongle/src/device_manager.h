/* device_manager.h - Device list and scanning */

#ifndef DEVICE_MANAGER_H_
#define DEVICE_MANAGER_H_

#include <zephyr/bluetooth/bluetooth.h>

/* Device list */
extern sys_slist_t device_list;

/* Functions */
void device_manager_init(void);
void print_device_list(void);
void start_scan(void);
void start_advertising(void);
void cancel_connection_timeout(struct bt_conn *conn);
void save_connected_device(struct bt_conn *conn);

#endif /* DEVICE_MANAGER_H_ */
