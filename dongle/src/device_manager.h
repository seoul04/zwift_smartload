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
void start_advertising(const char *device_name);
void cancel_connection_timeout(struct bt_conn *conn);
void save_connected_device(struct bt_conn *conn);

/* Scanning window control */
void start_scan_window(uint32_t duration_ms);
void stop_scan_window(void);
bool is_scan_window_active(void);

/* Disconnect all devices */
void disconnect_all_devices(void);

#endif /* DEVICE_MANAGER_H_ */
