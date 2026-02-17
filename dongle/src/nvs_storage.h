/* nvs_storage.h - Non-volatile storage for saved devices */

#ifndef NVS_STORAGE_H_
#define NVS_STORAGE_H_

#include <zephyr/bluetooth/bluetooth.h>
#include "common.h"

/* NVS initialization */
int nvs_storage_init(void);

/* Save a device to NVS (replaces existing entry if addr matches) */
int nvs_save_device(const bt_addr_le_t *addr, const char *name, uint8_t svc_mask);

/* Load all saved devices into memory */
int nvs_load_devices(struct saved_device *devices, int max_devices);

/* Check if a device is saved */
bool nvs_is_device_saved(const bt_addr_le_t *addr);

/* Get saved device info (returns svc_mask, or 0 if not found) */
uint8_t nvs_get_saved_svc_mask(const bt_addr_le_t *addr);

/* Get saved device name (returns NULL if not found) */
const char *nvs_get_saved_name(const bt_addr_le_t *addr);

/* Clear all saved devices */
int nvs_clear_all_devices(void);

/* Get device suffix from unique hardware ID (computed each time, not persisted) */
int nvs_get_device_suffix(char *suffix, int max_len);

#endif /* NVS_STORAGE_H_ */
