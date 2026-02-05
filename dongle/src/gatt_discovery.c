/* gatt_discovery.c - GATT service/characteristic discovery */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include <string.h>
#include "common.h"
#include "gatt_discovery.h"
#include "notification_handler.h"
#include "ftms_control_point.h"
#include "device_manager.h"

uint8_t discover_func(struct bt_conn *conn,
		      const struct bt_gatt_attr *attr,
		      struct bt_gatt_discover_params *params)
{
	int err;

	/* Find which connection slot this discovery belongs to */
	struct conn_slot *slot = NULL;
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (params == &connections[i].discover_params) {
			slot = &connections[i];
			break;
		}
	}

	if (!slot) {
		log("Discovery from unknown connection\n");
		return BT_GATT_ITER_STOP;
	}

	if (!attr) {
		log("Discover complete for service %d\n", slot->discover_service_index);
		(void)memset(params, 0, sizeof(*params));
		slot->discover_params.func = discover_func;
		slot->discover_params.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE;

		/* Proceed to next service if queued */
		if (slot->discover_service_index < discover_service_count - 1) {
			slot->discover_service_index++;
			memcpy(&slot->discover_uuid, discover_services[slot->discover_service_index], sizeof(slot->discover_uuid));
			slot->discover_params.uuid = &slot->discover_uuid.uuid;
			slot->discover_params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
			slot->discover_params.type = BT_GATT_DISCOVER_PRIMARY;
			err = bt_gatt_discover(conn, &slot->discover_params);
			if (err) {
				log("Discover failed (err %d)\n", err);
			}
		} else {
			start_scan();
		}

		return BT_GATT_ITER_STOP;
	}

	log("[ATTRIBUTE] handle %u\n", attr->handle);

	if (params->type == BT_GATT_DISCOVER_PRIMARY) {
		/* Discover characteristics for this service */
		slot->discover_params.start_handle = attr->handle + 1;
		slot->discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
		slot->discover_params.uuid = NULL;

		err = bt_gatt_discover(conn, &slot->discover_params);
		if (err) {
			log("Discover failed (err %d)\n", err);
		}
	} else if (params->type == BT_GATT_DISCOVER_CHARACTERISTIC) {
		/* Check if this is Control Point characteristic (0x2AD9) for FTMS */
		const struct bt_gatt_chrc *chrc = attr->user_data;
		uint16_t char_uuid = BT_UUID_16(chrc->uuid)->val;
		
		if (slot->discover_service_index == 2) {
			log("[FTMS] Found characteristic UUID 0x%04x, properties 0x%02x at handle %u\n", 
			       char_uuid, chrc->properties, attr->handle);
		}
		
		if (slot->discover_service_index == 2 && char_uuid == 0x2AD9) {
			/* Found FTMS Control Point */
			slot->ftms_control_point_handle = bt_gatt_attr_value_handle(attr);
			log("[FTMS CP] Control Point handle: %u\n", slot->ftms_control_point_handle);
			
			int idx = slot->subscribe_count;
			if (idx >= MAX_SUBSCRIPTIONS_PER_CONN) {
				log("[FTMS CP] No free subscription slot!\n");
				return BT_GATT_ITER_STOP;
			}
			
			struct bt_gatt_subscribe_params *sp = &slot->subscribe_params[idx];
			memset(sp, 0, sizeof(*sp));
			sp->notify = ftms_cp_indicate_func;
			sp->value = BT_GATT_CCC_INDICATE;
			sp->value_handle = slot->ftms_control_point_handle;
			sp->ccc_handle = attr->handle + 2;
			
			slot->service_type[idx] = slot->discover_service_index;
			
			err = bt_gatt_subscribe(conn, sp);
			if (err && err != -EALREADY) {
				log("[FTMS CP] Subscribe to indications failed (err %d)\n", err);
			} else {
				log("[FTMS CP] Subscribed to indications\n");
				slot->subscribe_count++;
			}
			
			/* Continue discovering other characteristics */
			slot->discover_params.start_handle = attr->handle + 1;
			slot->discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
			slot->discover_params.uuid = NULL;
			err = bt_gatt_discover(conn, &slot->discover_params);
			if (err) {
				log("Discover failed (err %d)\n", err);
			}
			return BT_GATT_ITER_STOP;
		}
		
		/* Check if characteristic supports notify/indicate */
		if (!(chrc->properties & (BT_GATT_CHRC_NOTIFY | BT_GATT_CHRC_INDICATE))) {
			log("[SKIP] Characteristic 0x%04x has no notify/indicate\n", char_uuid);
			slot->discover_params.start_handle = attr->handle + 1;
			err = bt_gatt_discover(conn, &slot->discover_params);
			if (err) {
				log("Discover failed (err %d)\n", err);
				start_scan();
			}
			return BT_GATT_ITER_STOP;
		}

		/* For HR service, prioritize HR Measurement (0x2A37) */
		if (slot->discover_service_index == 0 && char_uuid != 0x2A37) {
			log("[SKIP] Skipping Char 0x%04x in HR Service (looking for 0x2A37)\n", char_uuid);
			slot->discover_params.start_handle = attr->handle + 1;
			slot->discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
			slot->discover_params.uuid = NULL;
			err = bt_gatt_discover(conn, &slot->discover_params);
			if (err) {
				log("Discover failed (err %d)\n", err);
				start_scan();
			}
			return BT_GATT_ITER_STOP;
		}
		
		/* Discover CCC descriptor */
		memcpy(&slot->discover_uuid, BT_UUID_GATT_CCC, sizeof(slot->discover_uuid));
		slot->discover_params.uuid = &slot->discover_uuid.uuid;
		slot->discover_params.start_handle = attr->handle + 2;
		slot->discover_params.type = BT_GATT_DISCOVER_DESCRIPTOR;
		
		slot->temp_value_handle = bt_gatt_attr_value_handle(attr);

		err = bt_gatt_discover(conn, &slot->discover_params);
		if (err) {
			log("Discover failed (err %d)\n", err);
		}
	} else {
		/* Found CCC descriptor, subscribe */
		int idx = slot->subscribe_count;
		
		if (idx >= MAX_SUBSCRIPTIONS_PER_CONN) {
			log("[SUBSCRIBE] No free subscription slot!\n");
			return BT_GATT_ITER_STOP;
		}
		
		struct bt_gatt_subscribe_params *sp = &slot->subscribe_params[idx];
		memset(sp, 0, sizeof(*sp));
		
		sp->notify = notify_func;
		sp->value = BT_GATT_CCC_NOTIFY;
		sp->ccc_handle = attr->handle;
		sp->value_handle = slot->temp_value_handle;
		/* Mark as volatile so Zephyr doesn't persist across disconnects */
		atomic_set_bit(sp->flags, BT_GATT_SUBSCRIBE_FLAG_VOLATILE);

		slot->service_type[idx] = slot->discover_service_index;

		err = bt_gatt_subscribe(conn, sp);
		if (err == -EALREADY) {
			/* Shouldn't happen with VOLATILE flag, but handle just in case */
			log("[SUBSCRIBE] -EALREADY despite VOLATILE flag\n");
		} else if (err) {
			log("Subscribe failed (err %d)\n", err);
		} else {
			log("[SUBSCRIBED] service %d (slot %d, sub_idx %d)\n", 
			       slot->discover_service_index, (int)(slot - connections), slot->subscribe_count);
			slot->subscribe_count++;
		}

		/* For FTMS, continue discovering more characteristics */
		if (slot->discover_service_index == 2) {
			slot->discover_params.start_handle = attr->handle + 1;
			slot->discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
			slot->discover_params.uuid = NULL;

			err = bt_gatt_discover(conn, &slot->discover_params);
			if (err) {
				log("Discover failed (err %d)\n", err);
			}
		} else if (slot->discover_service_index < discover_service_count - 1) {
			/* Move to next service */
			slot->discover_service_index++;
			const char *svc_names[] = {"HRS", "CPS", "FTMS"};
			log("Switching discovery to service %s (Index %d)\n", 
			       svc_names[slot->discover_service_index], slot->discover_service_index);
			
			memcpy(&slot->discover_uuid, discover_services[slot->discover_service_index], 
			       sizeof(slot->discover_uuid));
			slot->discover_params.uuid = &slot->discover_uuid.uuid;
			slot->discover_params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
			slot->discover_params.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE;
			slot->discover_params.type = BT_GATT_DISCOVER_PRIMARY;
			slot->discover_params.func = discover_func;
			
			err = bt_gatt_discover(conn, &slot->discover_params);
			if (err) {
				log("Discover failed (err %d)\n", err);
				start_scan();
			}
		} else {
			log("Discover complete for all services\n");
			start_scan();
		}

		return BT_GATT_ITER_STOP;
	}

	return BT_GATT_ITER_STOP;
}

void start_discovery(struct bt_conn *conn, int slot_idx)
{
	struct conn_slot *slot = &connections[slot_idx];
	
	slot->subscribe_count = 0;
	slot->discover_service_index = 0;

	memcpy(&slot->discover_uuid, discover_services[0], sizeof(slot->discover_uuid));
	slot->discover_params.uuid = &slot->discover_uuid.uuid;
	slot->discover_params.func = discover_func;
	slot->discover_params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
	slot->discover_params.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE;
	slot->discover_params.type = BT_GATT_DISCOVER_PRIMARY;

	int err = bt_gatt_discover(conn, &slot->discover_params);
	if (err) {
		log("Discover failed(err %d)\n", err);
		start_scan();
	}
}
