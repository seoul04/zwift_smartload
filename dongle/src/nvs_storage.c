/* nvs_storage.c - Non-volatile storage for saved devices */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/flash.h>
#include <zephyr/storage/flash_map.h>
#include <zephyr/fs/nvs.h>
#include <zephyr/sys/rand32.h>
#include <string.h>
#include "common.h"
#include "nvs_storage.h"

#define NVS_PARTITION		storage_partition
#define NVS_PARTITION_DEVICE	FIXED_PARTITION_DEVICE(NVS_PARTITION)
#define NVS_PARTITION_OFFSET	FIXED_PARTITION_OFFSET(NVS_PARTITION)

/* NVS IDs for saved devices (1-4) */
#define NVS_DEVICE_BASE_ID	1
/* NVS ID for device suffix (5) */
#define NVS_DEVICE_SUFFIX_ID	5

struct nvs_fs nvs;  /* Non-static for use by grade_limiter */
static struct saved_device saved_devices[MAX_SAVED_DEVICES];
static bool nvs_initialized = false;

int nvs_storage_init(void)
{
	int err;
	struct flash_pages_info info;

	nvs.flash_device = NVS_PARTITION_DEVICE;
	if (!device_is_ready(nvs.flash_device)) {
		log("Flash device %s is not ready\n", nvs.flash_device->name);
		return -ENODEV;
	}

	nvs.offset = NVS_PARTITION_OFFSET;
	err = flash_get_page_info_by_offs(nvs.flash_device, nvs.offset, &info);
	if (err) {
		log("Unable to get page info (err %d)\n", err);
		return err;
	}

	nvs.sector_size = info.size;
	nvs.sector_count = 3U;  /* Use 3 sectors for wear leveling */

	err = nvs_mount(&nvs);
	if (err) {
		log("Flash Init failed (err %d)\n", err);
		return err;
	}

	log("NVS initialized: offset=0x%lx, sector_size=%u, sector_count=%u\n",
	       (unsigned long)nvs.offset, nvs.sector_size, nvs.sector_count);

	/* Load saved devices into RAM */
	memset(saved_devices, 0, sizeof(saved_devices));
	for (int i = 0; i < MAX_SAVED_DEVICES; i++) {
		ssize_t ret = nvs_read(&nvs, NVS_DEVICE_BASE_ID + i, 
		                       &saved_devices[i], sizeof(struct saved_device));
		if (ret == sizeof(struct saved_device)) {
			if (saved_devices[i].valid) {
				char addr_str[BT_ADDR_LE_STR_LEN];
				bt_addr_le_to_str(&saved_devices[i].addr, addr_str, sizeof(addr_str));
				log("Loaded saved device %d: %s (%s)\n", 
				       i, saved_devices[i].name, addr_str);
			}
		}
	}

	nvs_initialized = true;
	return 0;
}

int nvs_save_device(const bt_addr_le_t *addr, const char *name, uint8_t svc_mask)
{
	if (!nvs_initialized) {
		return -EINVAL;
	}

	int slot = -1;
	int empty_slot = -1;

	/* Check if device already exists or find empty slot */
	for (int i = 0; i < MAX_SAVED_DEVICES; i++) {
		if (saved_devices[i].valid && !bt_addr_le_cmp(&saved_devices[i].addr, addr)) {
			slot = i;
			break;
		}
		if (!saved_devices[i].valid && empty_slot == -1) {
			empty_slot = i;
		}
	}

	/* Use existing slot or first empty slot */
	if (slot == -1) {
		slot = empty_slot;
	}

	if (slot == -1) {
		log("No free slots to save device\n");
		return -ENOMEM;
	}

	/* Update in RAM */
	memcpy(&saved_devices[slot].addr, addr, sizeof(bt_addr_le_t));
	strncpy(saved_devices[slot].name, name, sizeof(saved_devices[slot].name) - 1);
	saved_devices[slot].name[sizeof(saved_devices[slot].name) - 1] = '\0';
	saved_devices[slot].svc_mask = svc_mask;
	saved_devices[slot].valid = 1;

	/* Write to NVS */
	ssize_t ret = nvs_write(&nvs, NVS_DEVICE_BASE_ID + slot, 
	                        &saved_devices[slot], sizeof(struct saved_device));
	if (ret < 0) {
		log("Failed to write device to NVS (err %d)\n", ret);
		return ret;
	}

	char addr_str[BT_ADDR_LE_STR_LEN];
	bt_addr_le_to_str(addr, addr_str, sizeof(addr_str));
	log("Saved device to slot %d: %s (%s)\n", slot, name, addr_str);

	return 0;
}

int nvs_load_devices(struct saved_device *devices, int max_devices)
{
	if (!nvs_initialized) {
		return -EINVAL;
	}

	int count = 0;
	for (int i = 0; i < MAX_SAVED_DEVICES && i < max_devices; i++) {
		if (saved_devices[i].valid) {
			memcpy(&devices[count], &saved_devices[i], sizeof(struct saved_device));
			count++;
		}
	}

	return count;
}

bool nvs_is_device_saved(const bt_addr_le_t *addr)
{
	if (!nvs_initialized) {
		return false;
	}

	for (int i = 0; i < MAX_SAVED_DEVICES; i++) {
		if (saved_devices[i].valid && !bt_addr_le_cmp(&saved_devices[i].addr, addr)) {
			return true;
		}
	}

	return false;
}

int nvs_clear_all_devices(void)
{
	if (!nvs_initialized) {
		return -EINVAL;
	}

	for (int i = 0; i < MAX_SAVED_DEVICES; i++) {
		saved_devices[i].valid = 0;
		nvs_write(&nvs, NVS_DEVICE_BASE_ID + i, 
		          &saved_devices[i], sizeof(struct saved_device));
	}

	log("Cleared all saved devices\n");
	return 0;
}

int nvs_get_device_suffix(char *suffix, int max_len)
{
	if (!nvs_initialized || !suffix || max_len < 5) {  /* Need room for "XXXX\0" */
		return -EINVAL;
	}

	char stored_suffix[8];
	ssize_t ret = nvs_read(&nvs, NVS_DEVICE_SUFFIX_ID, &stored_suffix, sizeof(stored_suffix));

	if (ret > 0) {
		/* Suffix exists in NVS, use it */
		strncpy(suffix, stored_suffix, max_len - 1);
		suffix[max_len - 1] = '\0';
		return 0;
	}

	/* Generate new random 4-digit hex suffix */
	uint16_t random_val = (uint16_t)(sys_rand32_get() & 0xFFFF);
	snprintf(stored_suffix, sizeof(stored_suffix), "%04X", random_val);

	/* Save to NVS */
	ret = nvs_write(&nvs, NVS_DEVICE_SUFFIX_ID, stored_suffix, strlen(stored_suffix) + 1);
	if (ret < 0) {
		log("Failed to write device suffix to NVS (err %d)\n", ret);
		/* Still use the generated suffix even if save failed */
	}

	strncpy(suffix, stored_suffix, max_len - 1);
	suffix[max_len - 1] = '\0';
	log("Generated new device suffix: %s\n", suffix);

	return 0;
}

