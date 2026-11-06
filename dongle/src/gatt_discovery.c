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
#include "ecg_stream.h"

static uint8_t battery_read_func(struct bt_conn *conn, uint8_t err,
				 struct bt_gatt_read_params *params,
				 const void *data, uint16_t length)
{
	if (err) {
		log("[BAS] Battery read failed (err %u)\n", err);
		return BT_GATT_ITER_STOP;
	}

	if (!data || length < 1) {
		return BT_GATT_ITER_STOP;
	}

	const uint8_t battery_level = ((const uint8_t *)data)[0];

	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (connections[i].conn != conn) {
			continue;
		}

		struct device_info *dev_info;
		SYS_SLIST_FOR_EACH_CONTAINER(&device_list, dev_info, node) {
			if (!bt_addr_le_cmp(&dev_info->addr, bt_conn_get_dst(conn))) {
				dev_info->has_battery_service = true;
				dev_info->battery_level = (int8_t)battery_level;
				break;
			}
		}
		break;
	}

	print_device_list();

	return BT_GATT_ITER_STOP;
}

static uint8_t battery_discover_func(struct bt_conn *conn,
				     const struct bt_gatt_attr *attr,
				     struct bt_gatt_discover_params *params)
{
	struct conn_slot *slot = NULL;
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (params == &connections[i].battery_discover_params) {
			slot = &connections[i];
			break;
		}
	}

	if (!slot) {
		return BT_GATT_ITER_STOP;
	}

	if (!attr) {
		if (params->type == BT_GATT_DISCOVER_PRIMARY) {
			log("[BAS] Battery service not found\n");
		}
		(void)memset(params, 0, sizeof(*params));
		return BT_GATT_ITER_STOP;
	}

	if (params->type == BT_GATT_DISCOVER_PRIMARY) {
		const struct bt_gatt_service_val *service = attr->user_data;

		slot->battery_uuid.uuid.type = BT_UUID_TYPE_16;
		slot->battery_uuid.val = 0x2A19;
		slot->battery_discover_params.uuid = &slot->battery_uuid.uuid;
		slot->battery_discover_params.func = battery_discover_func;
		slot->battery_discover_params.start_handle = attr->handle + 1;
		slot->battery_discover_params.end_handle = service->end_handle;
		slot->battery_discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;

		int err = bt_gatt_discover(conn, &slot->battery_discover_params);
		if (err) {
			log("[BAS] Characteristic discover failed (err %d)\n", err);
		}

		return BT_GATT_ITER_STOP;
	}

	const struct bt_gatt_chrc *chrc = attr->user_data;
	uint16_t value_handle = bt_gatt_attr_value_handle(attr);

	(void)memset(&slot->battery_read_params, 0, sizeof(slot->battery_read_params));
	slot->battery_read_params.func = battery_read_func;
	slot->battery_read_params.handle_count = 1;
	slot->battery_read_params.single.handle = value_handle;
	slot->battery_read_params.single.offset = 0;

	int err = bt_gatt_read(conn, &slot->battery_read_params);
	if (err) {
		log("[BAS] Read request failed (err %d, props 0x%02x)\n", err, chrc->properties);
	}

	(void)memset(params, 0, sizeof(*params));
	return BT_GATT_ITER_STOP;
}

/* Map a characteristic UUID to what its notifications carry. */
static enum sub_kind kind_for_uuid(const struct bt_uuid *uuid)
{
	if (uuid->type == BT_UUID_TYPE_16) {
		switch (BT_UUID_16(uuid)->val) {
		case 0x2A37:
			return SUB_HR_MEASUREMENT;
		case 0x2A63:
			return SUB_CP_MEASUREMENT;
		case 0x2AD2:
			return SUB_FTMS_BIKE_DATA;
		case 0x2AD3:
			return SUB_FTMS_TRAINING;
		case 0x2ADA:
			return SUB_FTMS_STATUS;
		case 0x2AD9:
			return SUB_FTMS_CONTROL_POINT;
		default:
			return SUB_NONE;
		}
	}

	if (uuid->type == BT_UUID_TYPE_128) {
		if (!bt_uuid_cmp(uuid, &pmd_data_uuid.uuid)) {
			return SUB_POLAR_PMD_DATA;
		}
		if (!bt_uuid_cmp(uuid, &gsp_data_uuid.uuid)) {
			return SUB_MOVESENSE_GSP_DATA;
		}
	}

	return SUB_NONE;
}

/* Continue the characteristic walk inside the current service. */
static void next_characteristic(struct bt_conn *conn, struct conn_slot *slot,
				const struct bt_gatt_attr *attr)
{
	slot->discover_params.start_handle = attr->handle + 1;
	slot->discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
	slot->discover_params.uuid = NULL;

	int err = bt_gatt_discover(conn, &slot->discover_params);
	if (err) {
		log("Discover failed (err %d)\n", err);
	}
}

/* Move on to the next service in discover_services[], or finish. */
static void next_service(struct bt_conn *conn, struct conn_slot *slot)
{
	(void)memset(&slot->discover_params, 0, sizeof(slot->discover_params));
	slot->discover_params.func = discover_func;
	slot->discover_params.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE;

	if (slot->discover_service_index >= discover_service_count - 1) {
		log("Discover complete for all services\n");
		/* Both vendor services have been walked by now, so any ECG command
		 * handle found along the way is usable. */
		ecg_stream_start(conn, slot);
		start_battery_level_check(conn, (int)(slot - connections));
		start_scan();
		return;
	}

	slot->discover_service_index++;
	slot->discover_uuid = discover_services[slot->discover_service_index];
	slot->discover_params.uuid = slot->discover_uuid;
	slot->discover_params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
	slot->discover_params.type = BT_GATT_DISCOVER_PRIMARY;

	int err = bt_gatt_discover(conn, &slot->discover_params);
	if (err) {
		log("Discover failed (err %d)\n", err);
	}
}

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
		next_service(conn, slot);
		return BT_GATT_ITER_STOP;
	}

	if (params->type == BT_GATT_DISCOVER_PRIMARY) {
		const struct bt_gatt_service_val *service = attr->user_data;

		log("[SERVICE] handle %u (index %d)\n", attr->handle,
		    slot->discover_service_index);

		/* Remember which service we are walking: characteristics are routed by
		 * UUID, but a few behaviours (continuing past the last one) depend on
		 * where we are. */
		slot->cur_service = slot->discover_service_index;

		slot->discover_params.start_handle = attr->handle + 1;
		slot->discover_params.end_handle = service->end_handle;
		slot->discover_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;
		slot->discover_params.uuid = NULL;

		err = bt_gatt_discover(conn, &slot->discover_params);
		if (err) {
			log("Discover failed (err %d)\n", err);
		}

		return BT_GATT_ITER_STOP;
	}

	if (params->type == BT_GATT_DISCOVER_CHARACTERISTIC) {
		const struct bt_gatt_chrc *chrc = attr->user_data;
		enum sub_kind kind = kind_for_uuid(chrc->uuid);

		/* Temporary: dump every characteristic so the GSP/PMD UUIDs can be
		 * checked against a real device. */
		if (chrc->uuid->type == BT_UUID_TYPE_16) {
			log("[CHRC] svc %d handle %u uuid 0x%04X props 0x%02x\n",
			    slot->cur_service, bt_gatt_attr_value_handle(attr),
			    BT_UUID_16(chrc->uuid)->val, chrc->properties);
		} else if (chrc->uuid->type == BT_UUID_TYPE_128) {
			const uint8_t *u = BT_UUID_128(chrc->uuid)->val;

			log("[CHRC] svc %d handle %u uuid128 %02X%02X%02X%02X-"
			    "%02X%02X-%02X%02X-%02X%02X-%02X%02X%02X%02X%02X%02X "
			    "props 0x%02x\n",
			    slot->cur_service, bt_gatt_attr_value_handle(attr),
			    u[15], u[14], u[13], u[12], u[11], u[10], u[9], u[8],
			    u[7], u[6], u[5], u[4], u[3], u[2], u[1], u[0],
			    chrc->properties);
		}

		/* Polar PMD control point and the GSP command characteristic are not
		 * data streams: they are the handles commands are written to. */
		if (chrc->uuid->type == BT_UUID_TYPE_128) {
			if (!bt_uuid_cmp(chrc->uuid, &pmd_control_point_uuid.uuid)) {
				slot->polar_pmd_cp_handle = bt_gatt_attr_value_handle(attr);
				log("[PMD] Control point handle: %u\n",
				    slot->polar_pmd_cp_handle);

				/* The control point reports command results as
				 * indications. Without this subscription a rejected
				 * ECG start command is silent. */
				if (chrc->properties & BT_GATT_CHRC_INDICATE) {
					int idx = slot->subscribe_count;

					if (idx < MAX_SUBSCRIPTIONS_PER_CONN) {
						struct bt_gatt_subscribe_params *sp =
							&slot->subscribe_params[idx];
						struct bt_gatt_discover_params *dp =
							&slot->ccc_disc_params[idx];

						memset(sp, 0, sizeof(*sp));
						memset(dp, 0, sizeof(*dp));
						sp->notify = notify_func;
						sp->value = BT_GATT_CCC_INDICATE;
						sp->value_handle =
							slot->polar_pmd_cp_handle;
						sp->ccc_handle =
							BT_GATT_AUTO_DISCOVER_CCC_HANDLE;
						/* The CCC lives at or just after the value handle;
						 * end_handle only bounds the search. */
						sp->end_handle = slot->polar_pmd_cp_handle + 2;
						sp->disc_params = dp;
						slot->sub_kind[idx] = SUB_POLAR_PMD_CONTROL;
						atomic_set_bit(sp->flags,
							       BT_GATT_SUBSCRIBE_FLAG_VOLATILE);

						err = bt_gatt_subscribe(conn, sp);
						if (err && err != -EALREADY) {
							log("[PMD] Subscribe to indications failed (err %d)\n",
							    err);
						} else {
							slot->subscribe_count++;
							log("[PMD] Subscribed to control point indications\n");
						}
					}
				}
			} else if (!bt_uuid_cmp(chrc->uuid, &gsp_command_uuid.uuid)) {
				slot->movesense_cmd_handle = bt_gatt_attr_value_handle(attr);
				log("[GSP] Command handle: %u\n",
				    slot->movesense_cmd_handle);
			}
		}

		if (kind == SUB_NONE) {
			next_characteristic(conn, slot, attr);
			return BT_GATT_ITER_STOP;
		}

		if (kind == SUB_FTMS_CONTROL_POINT) {
			/* Control point is indicated, not notified, and it is not a
			 * data stream: subscribe directly without a CCC search. */
			slot->ftms_control_point_handle = bt_gatt_attr_value_handle(attr);
			log("[FTMS CP] Control Point handle: %u\n",
			    slot->ftms_control_point_handle);

			int idx = slot->subscribe_count;

			if (idx < MAX_SUBSCRIPTIONS_PER_CONN) {
				struct bt_gatt_subscribe_params *sp =
					&slot->subscribe_params[idx];
				struct bt_gatt_discover_params *dp =
					&slot->ccc_disc_params[idx];

				memset(sp, 0, sizeof(*sp));
				memset(dp, 0, sizeof(*dp));
				sp->notify = ftms_cp_indicate_func;
				sp->value = BT_GATT_CCC_INDICATE;
				sp->value_handle = slot->ftms_control_point_handle;
				sp->ccc_handle = BT_GATT_AUTO_DISCOVER_CCC_HANDLE;
				sp->end_handle = slot->ftms_control_point_handle + 2;
				sp->disc_params = dp;
				slot->sub_kind[idx] = SUB_FTMS_CONTROL_POINT;

				err = bt_gatt_subscribe(conn, sp);
				if (err && err != -EALREADY) {
					log("[FTMS CP] Subscribe to indications failed (err %d)\n",
					    err);
				} else {
					log("[FTMS CP] Subscribed to indications\n");
					slot->subscribe_count++;
				}
			}

			next_characteristic(conn, slot, attr);
			return BT_GATT_ITER_STOP;
		}

		/* Data stream: remember the value handle and let Zephyr find the CCC. */
		slot->temp_value_handle = bt_gatt_attr_value_handle(attr);

		struct bt_gatt_subscribe_params *sp = NULL;
		int idx = slot->subscribe_count;

		if (idx < MAX_SUBSCRIPTIONS_PER_CONN) {
			struct bt_gatt_discover_params *dp =
				&slot->ccc_disc_params[idx];

			sp = &slot->subscribe_params[idx];
			memset(sp, 0, sizeof(*sp));
			memset(dp, 0, sizeof(*dp));
			sp->notify = notify_func;
			/* PMD Data advertises Notify and Indicate. Prefer notify and only
			 * fall back to indications for a characteristic that offers
			 * nothing else. */
			if (chrc->properties & BT_GATT_CHRC_NOTIFY) {
				sp->value = BT_GATT_CCC_NOTIFY;
			} else if (chrc->properties & BT_GATT_CHRC_INDICATE) {
				sp->value = BT_GATT_CCC_INDICATE;
			} else {
				log("[SUBSCRIBE] Characteristic %u has no notify/indicate\n",
				    slot->temp_value_handle);
				next_characteristic(conn, slot, attr);
				return BT_GATT_ITER_STOP;
			}
			sp->ccc_handle = BT_GATT_AUTO_DISCOVER_CCC_HANDLE;
			sp->value_handle = slot->temp_value_handle;
			/* Auto-CCC discovery requires a discover_params pointer and a
			 * non-zero end_handle; the CCC sits right after the value. */
			sp->end_handle = slot->temp_value_handle + 2;
			sp->disc_params = dp;
			atomic_set_bit(sp->flags, BT_GATT_SUBSCRIBE_FLAG_VOLATILE);
			slot->sub_kind[idx] = kind;

			err = bt_gatt_subscribe(conn, sp);
			if (err && err != -EALREADY) {
				log("Subscribe failed (kind %d, value 0x%04X, ccc 0x%04X, "
				    "end 0x%04X, err %d)\n",
				    (int)kind, sp->value_handle, sp->ccc_handle,
				    sp->end_handle, err);
				sp = NULL;
			} else {
				log("[SUBSCRIBED] kind %d (slot %d, sub_idx %d)\n",
				    (int)kind, (int)(slot - connections), idx);
				slot->subscribe_count++;
			}
		} else {
			log("[SUBSCRIBE] No free subscription slot!\n");
		}

		next_characteristic(conn, slot, attr);
		return BT_GATT_ITER_STOP;
	}

	return BT_GATT_ITER_STOP;
}

/* Format a UUID into buf as canonical text. Kept as a single string so the
 * caller can emit it with one log() call and stay atomic against ECG output. */
static void format_uuid(const struct bt_uuid *uuid, char *buf, size_t len)
{
	if (uuid->type == BT_UUID_TYPE_16) {
		snprintf(buf, len, "0x%04X", BT_UUID_16(uuid)->val);
	} else if (uuid->type == BT_UUID_TYPE_128) {
		const uint8_t *u = BT_UUID_128(uuid)->val;

		snprintf(buf, len,
			 "%02X%02X%02X%02X-%02X%02X-%02X%02X-%02X%02X-"
			 "%02X%02X%02X%02X%02X%02X",
			 u[15], u[14], u[13], u[12], u[11], u[10], u[9], u[8],
			 u[7], u[6], u[5], u[4], u[3], u[2], u[1], u[0]);
	} else {
		snprintf(buf, len, "(uuid type %u)", uuid->type);
	}
}

/* Forward declarations: the service walk hands off to the characteristic walk,
 * which can call back into the service walk when one service finishes. */
static uint8_t enum_chrc_func(struct bt_conn *conn,
			      const struct bt_gatt_attr *attr,
			      struct bt_gatt_discover_params *params);
static void enum_next_service_chrcs(struct bt_conn *conn, struct conn_slot *slot);

/* Walks every primary service. Runs before the targeted discovery so the log
 * shows what the peer really exposes, including 128-bit services. */
static uint8_t enum_discover_func(struct bt_conn *conn,
				  const struct bt_gatt_attr *attr,
				  struct bt_gatt_discover_params *params)
{
	struct conn_slot *slot = NULL;

	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (params == &connections[i].enum_params) {
			slot = &connections[i];
			break;
		}
	}

	if (!slot) {
		return BT_GATT_ITER_STOP;
	}

	if (!attr) {
		log("[ENUM] done: %d primary service(s)\n", slot->enum_count);
		/* Now walk the characteristics of each service found. */
		slot->enum_svc_idx = 0;
		enum_next_service_chrcs(conn, slot);
		return BT_GATT_ITER_STOP;
	}

	const struct bt_gatt_service_val *service = attr->user_data;
	char uuid[40];

	format_uuid(service->uuid, uuid, sizeof(uuid));

	slot->enum_count++;
	log("[ENUM] service %d handle %u-%u uuid %s\n",
	    slot->enum_count, attr->handle, service->end_handle, uuid);

	/* Remember the range so its characteristics can be listed afterwards. */
	if (slot->enum_svc_count < ENUM_MAX_SERVICES) {
		struct enum_svc_entry *e = &slot->enum_svcs[slot->enum_svc_count];

		e->start_handle = attr->handle;
		e->end_handle = service->end_handle;
		if (service->uuid->type == BT_UUID_TYPE_128) {
			memcpy(&e->uuid, service->uuid, sizeof(struct bt_uuid_128));
		} else {
			memset(&e->uuid, 0, sizeof(e->uuid));
			e->uuid.uuid.type = BT_UUID_TYPE_16;
			e->uuid.val[0] = BT_UUID_16(service->uuid)->val & 0xFF;
			e->uuid.val[1] = BT_UUID_16(service->uuid)->val >> 8;
		}
		slot->enum_svc_count++;
	}

	return BT_GATT_ITER_CONTINUE;
}

/* Continue to the next service's characteristic walk, or finish enumeration. */
static void enum_next_service_chrcs(struct bt_conn *conn, struct conn_slot *slot)
{
	if (slot->enum_svc_idx >= slot->enum_svc_count) {
		log("[ENUM] characteristics done\n");
		slot->enum_active = 0;
		start_discovery(conn, (int)(slot - connections));
		return;
	}

	struct enum_svc_entry *e = &slot->enum_svcs[slot->enum_svc_idx];

	(void)memset(&slot->enum_chrc_params, 0, sizeof(slot->enum_chrc_params));
	slot->enum_chrc_params.func = enum_chrc_func;
	slot->enum_chrc_params.start_handle = e->start_handle + 1;
	slot->enum_chrc_params.end_handle = e->end_handle;
	slot->enum_chrc_params.uuid = NULL;
	slot->enum_chrc_params.type = BT_GATT_DISCOVER_CHARACTERISTIC;

	int err = bt_gatt_discover(conn, &slot->enum_chrc_params);
	if (err) {
		log("[ENUM] chrc discover failed (err %d) for service %d\n",
		    err, slot->enum_svc_idx + 1);
		slot->enum_svc_idx++;
		enum_next_service_chrcs(conn, slot);
	}
}

/* Lists every characteristic of one service. */
static uint8_t enum_chrc_func(struct bt_conn *conn,
			      const struct bt_gatt_attr *attr,
			      struct bt_gatt_discover_params *params)
{
	struct conn_slot *slot = NULL;

	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (params == &connections[i].enum_chrc_params) {
			slot = &connections[i];
			break;
		}
	}

	if (!slot) {
		return BT_GATT_ITER_STOP;
	}

	if (!attr) {
		slot->enum_svc_idx++;
		enum_next_service_chrcs(conn, slot);
		return BT_GATT_ITER_STOP;
	}

	const struct bt_gatt_chrc *chrc = attr->user_data;
	char uuid[40];

	format_uuid(chrc->uuid, uuid, sizeof(uuid));

	log("[ENUM]   svc %d chrc handle %u uuid %s props 0x%02x\n",
	    slot->enum_svc_idx + 1, bt_gatt_attr_value_handle(attr), uuid,
	    chrc->properties);

	return BT_GATT_ITER_CONTINUE;
}

void start_enumeration(struct bt_conn *conn, int slot_idx)
{
	struct conn_slot *slot = &connections[slot_idx];

	slot->enum_active = 1;
	slot->enum_count = 0;
	slot->enum_svc_count = 0;
	slot->enum_svc_idx = 0;
	(void)memset(&slot->enum_params, 0, sizeof(slot->enum_params));

	slot->enum_params.func = enum_discover_func;
	slot->enum_params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
	slot->enum_params.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE;
	/* uuid == NULL makes this a READ_BY_GROUP_TYPE walk of every service. */
	slot->enum_params.uuid = NULL;
	slot->enum_params.type = BT_GATT_DISCOVER_PRIMARY;

	int err = bt_gatt_discover(conn, &slot->enum_params);
	if (err) {
		log("[ENUM] discover failed (err %d)\n", err);
		slot->enum_active = 0;
		start_discovery(conn, slot_idx);
	}
}

void start_discovery(struct bt_conn *conn, int slot_idx)
{
	struct conn_slot *slot = &connections[slot_idx];

	slot->enum_active = 0;
	slot->subscribe_count = 0;
	slot->discover_service_index = 0;
	slot->cur_service = 0;
	slot->temp_value_handle = 0;
	slot->polar_pmd_cp_handle = 0;
	slot->movesense_cmd_handle = 0;
	slot->gsp_len = 0;
	slot->gsp_pending = false;
	slot->gsp_ecg_started = 0;
	slot->hr_rr_misses = 0;
	slot->ecg_clock_valid = 0;

	slot->discover_uuid = discover_services[0];
	slot->discover_params.uuid = slot->discover_uuid;
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

void start_battery_level_check(struct bt_conn *conn, int slot_idx)
{
	struct conn_slot *slot = &connections[slot_idx];

	(void)memset(&slot->battery_discover_params, 0, sizeof(slot->battery_discover_params));
	(void)memset(&slot->battery_read_params, 0, sizeof(slot->battery_read_params));

	slot->battery_uuid.uuid.type = BT_UUID_TYPE_16;
	slot->battery_uuid.val = 0x180F;
	slot->battery_discover_params.uuid = &slot->battery_uuid.uuid;
	slot->battery_discover_params.func = battery_discover_func;
	slot->battery_discover_params.start_handle = BT_ATT_FIRST_ATTRIBUTE_HANDLE;
	slot->battery_discover_params.end_handle = BT_ATT_LAST_ATTRIBUTE_HANDLE;
	slot->battery_discover_params.type = BT_GATT_DISCOVER_PRIMARY;

	int err = bt_gatt_discover(conn, &slot->battery_discover_params);
	if (err) {
		log("[BAS] Discover failed (err %d)\n", err);
	}
}
