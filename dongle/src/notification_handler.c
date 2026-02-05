/* notification_handler.c - Notification callback handlers */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/byteorder.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "common.h"
#include "notification_handler.h"
#include "gatt_services.h"

/* CP data cache for injection into FTMS */
struct cp_cache cached_cp_data = {0};

uint8_t notify_func(struct bt_conn *conn,
		    struct bt_gatt_subscribe_params *params,
		    const void *data, uint16_t length)
{
	if (!data) {
		log("[DEBUG] Unsubscribed value_handle=%u\n", params->value_handle);
		return BT_GATT_ITER_STOP;
	}

	/* Find which connection slot this notification belongs to */
	struct conn_slot *slot = NULL;
	int sub_idx = -1;
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		/* Skip slots without an active connection */
		if (!connections[i].conn) {
			continue;
		}
		for (int j = 0; j < connections[i].subscribe_count; j++) {
			if (&connections[i].subscribe_params[j] == params) {
				slot = &connections[i];
				sub_idx = j;
				break;
			}
		}
		if (slot) break;
	}

	if (!slot) {
		log("[DEBUG] Notification from unknown subscription\n");
		return BT_GATT_ITER_CONTINUE;
	}

	/* Get service type from the found index */
	int svc_type = slot->service_type[sub_idx];

	if (svc_type == -1) {
		log("[DEBUG] Service type not found (length=%u, handle=%u)\n", length, params->value_handle);
		return BT_GATT_ITER_CONTINUE;
	}

	log("[DEBUG] Notification: svc_type=%d, length=%u, handle=%u\n", svc_type, length, params->value_handle);

	/* Get RSSI - use a cached value since live RSSI requires async callback */
	/* We'll update it periodically in main loop instead */

	if (svc_type == 0) {
		/* HR service */
		const uint8_t *hr_data = data;
		uint8_t flags = hr_data[0];
		uint8_t hr_format = flags & 0x01;
		uint16_t heart_rate;

		if (length < 2) {
			log("[DEBUG] Invalid HR data length: %u\n", length);
			return BT_GATT_ITER_CONTINUE;
		}

		if (hr_format == 0) {
			heart_rate = hr_data[1];
		} else {
			if (length < 3) {
				log("[DEBUG] Invalid HR data length for UINT16: %u\n", length);
				return BT_GATT_ITER_CONTINUE;
			}
			heart_rate = sys_le16_to_cpu(*(uint16_t *)&hr_data[1]);
		}

		hr_measurement_len = length;
		memcpy(hr_measurement, data, length);
		bt_gatt_notify(NULL, &hr_svc.attrs[1], hr_measurement, hr_measurement_len);

		json_out("{\"type\":\"hr\",\"ts\":%u,\"bpm\":%u,\"rssi\":%d}\n", k_uptime_get_32(), heart_rate, slot->rssi);
	} else if (svc_type == 1) {
		/* CP service - always relay to Zwift immediately */
		cp_measurement_len = length;
		memcpy(cp_measurement, data, length);
		bt_gatt_notify(NULL, &cp_svc.attrs[1], cp_measurement, cp_measurement_len);
		
		/* Now parse and cache for internal use */
		last_cp_data_time = k_uptime_get_32();
		const uint8_t *cp_data = data;
		
		if (length >= 4) {
			uint16_t flags = sys_le16_to_cpu(*(uint16_t *)&cp_data[0]);
			int16_t power = sys_le16_to_cpu(*(uint16_t *)&cp_data[2]);
			int offset = 4;
			
			/* Cache power */
			cached_cp_data.power = power;
			cached_cp_data.timestamp = last_cp_data_time;
			
			json_out("{\"type\":\"cp\",\"ts\":%u,\"power\":%d,\"flags\":%u,\"rssi\":%d", last_cp_data_time, power, flags, slot->rssi);
			
			if (flags & 0x01) {
				if (length > offset) {
					uint8_t balance = cp_data[offset];
				json_out(",\"balance\":%u", balance);
					offset++;
				}
			}
			
			if (flags & 0x20) {
				if (length >= offset + 4) {
					uint16_t crank_revs = sys_le16_to_cpu(*(uint16_t *)&cp_data[offset]);
					uint16_t crank_time = sys_le16_to_cpu(*(uint16_t *)&cp_data[offset + 2]);
					
					/* Calculate cadence from crank revolution delta using sensor time */
					if (cached_cp_data.valid) {
						uint16_t rev_delta = (crank_revs >= cached_cp_data.last_crank_revs) ? 
							(crank_revs - cached_cp_data.last_crank_revs) : 
							(65536 + crank_revs - cached_cp_data.last_crank_revs);
						
						if (rev_delta > 0) {
							/* Crank position changed - calculate cadence from sensor time */
							uint16_t time_delta = (crank_time >= cached_cp_data.last_crank_time) ?
								(crank_time - cached_cp_data.last_crank_time) :
								(65536 + crank_time - cached_cp_data.last_crank_time);
							
							if (time_delta > 0) {
								/* rpm = (rev_delta / (time_delta / 1024)) * 60 */
								/* cadence (0.5 rpm) = rpm * 2 = (rev_delta * 122880) / time_delta */
								uint32_t cadence_calc = (rev_delta * 122880UL) / time_delta;
								cached_cp_data.cadence = (uint16_t)(cadence_calc > 65535 ? 65535 : cadence_calc);
							}
							cached_cp_data.last_crank_change_time = last_cp_data_time;
						} else {
							/* No change in crank position - check timeout */
							uint32_t time_since_change = last_cp_data_time - cached_cp_data.last_crank_change_time;
							if (time_since_change >= 4000) {
								cached_cp_data.cadence = 0;
							}
							/* else: keep previous cadence value */
						}
					} else {
						/* First time seeing crank data - initialize */
						cached_cp_data.last_crank_change_time = last_cp_data_time;
					}
					json_out(",\"crank_revs\":%u,\"crank_time\":%u,\"cadence\":%u", crank_revs, crank_time, cached_cp_data.cadence / 2);
					
					cached_cp_data.last_crank_revs = crank_revs;
					cached_cp_data.last_crank_time = crank_time;
					cached_cp_data.valid = true;
				}
			}
			json_out("}\n");
		}
		
		/* Send CSC notification if we have crank data */
		if (cached_cp_data.valid) {
			csc_measurement[0] = 0x02; /* Crank Revolution Data Present */
			*(uint16_t *)&csc_measurement[1] = sys_cpu_to_le16(cached_cp_data.last_crank_revs);
			*(uint16_t *)&csc_measurement[3] = sys_cpu_to_le16(cached_cp_data.last_crank_time);
			csc_measurement_len = 5;
			bt_gatt_notify(NULL, &csc_svc.attrs[1], csc_measurement, csc_measurement_len);
		}
	} else if (svc_type == 2) {
		/* FTMS Indoor Bike Data */
		const uint8_t *ftms_data = data;
		uint16_t flags = 0;
		bool cp_active = false;
		int cadence_offset = -1;
		int power_offset = -1;
		uint32_t now = k_uptime_get_32();
		
		if (length >= 2) {
			flags = sys_le16_to_cpu(*(uint16_t *)&ftms_data[0]);
			int offset = 2;
			
			/* Check if power meter is active */
			cp_active = (cached_cp_data.valid && (now - cached_cp_data.timestamp) < CP_TIMEOUT_MS);
			
			json_out("{\"type\":\"ftms\",\"ts\":%u,\"flags\":%u,\"rssi\":%d", now, flags, slot->rssi);
			
			/* Instantaneous Speed */
			if (length >= offset + 2) {
				uint16_t speed = sys_le16_to_cpu(*(uint16_t *)&ftms_data[offset]);
				json_out(",\"speed\":%u", speed);
			}
			offset += 2;
			
			if (flags & 0x0002) {
				offset += 2;
			}
			
			cadence_offset = -1;
			power_offset = -1;
			
			if (flags & 0x0004) {
				cadence_offset = offset;
				if (length >= offset + 2) {
					uint16_t ftms_cadence = sys_le16_to_cpu(*(uint16_t *)&ftms_data[offset]);
					json_out(",\"cadence\":%u", ftms_cadence / 2);
				}
				offset += 2;
			}
			
			if (flags & 0x0008) {
				offset += 2;
			}
			
			if (flags & 0x0010) {
				offset += 3;
			}
			
			if (flags & 0x0020) {
				if (length >= offset + 2) {
					int16_t resistance = sys_le16_to_cpu(*(uint16_t *)&ftms_data[offset]);
					json_out(",\"resistance\":%d", resistance);
				}
				offset += 2;
			}
			
			/* Extract/inject power */
			int16_t ftms_power = -1;
			if (flags & 0x0040) {
				power_offset = offset;
				if (length >= offset + 2) {
					ftms_power = sys_le16_to_cpu(*(uint16_t *)&ftms_data[offset]);
					json_out(",\"power\":%d", ftms_power);
				}
				offset += 2;
			}

			json_out("}\n");
		}
		/* Rebroadcast FTMS with CP power injection if active */
		ftms_measurement_len = length;
		memcpy(ftms_measurement, data, length);
		
		/* Inject power meter power if active AND power field already exists */
		/* Only replace existing power to avoid creating invalid packet structure */
		if (cp_active && cached_cp_data.power >= 0 && power_offset >= 0 && power_offset + 2 <= ftms_measurement_len) {
			*(int16_t *)&ftms_measurement[power_offset] = sys_cpu_to_le16(cached_cp_data.power);
		}
		
		bt_gatt_notify(NULL, &ftms_svc.attrs[1], ftms_measurement, ftms_measurement_len);
	} else if (svc_type == 3) {
		/* FTMS Training Status */
		log("[DEBUG] FTMS Training Status [%u bytes]\n", length);
		ftms_training_status_len = length;
		memcpy(ftms_training_status, data, length);
		bt_gatt_notify(NULL, &ftms_svc.attrs[3], ftms_training_status, ftms_training_status_len);
	} else if (svc_type == 4) {
		/* FTMS Machine Status */
		const uint8_t *status_data = data;
		uint32_t now = k_uptime_get_32();
		
		if (length >= 1) {
			uint8_t op_code = status_data[0];
			
			json_out("{\"type\":\"status\",\"ts\":%u,\"code\":%u", now, op_code);
			
			switch (op_code) {
			case 0x05:
				if (length >= 3) {
					uint16_t speed = sys_le16_to_cpu(*(uint16_t *)&status_data[1]);
					json_out(",\"speed\":%u", speed);
				}
				break;
			case 0x06:
				if (length >= 3) {
					int16_t incline = sys_le16_to_cpu(*(uint16_t *)&status_data[1]);
					json_out(",\"incline\":%d", incline);
				}
				break;
			case 0x07:
				if (length >= 2) {
					int8_t resistance = status_data[1];
					json_out(",\"resistance\":%d", resistance);
				}
				break;
			case 0x08:
				if (length >= 3) {
					int16_t power = sys_le16_to_cpu(*(uint16_t *)&status_data[1]);
					json_out(",\"target_power\":%d", power);
				}
				break;
			case 0x09:
				if (length >= 2) {
					uint8_t hr = status_data[1];
					json_out(",\"target_hr\":%u", hr);
				}
				break;
			case 0x83:
			case 0x84:
				if (length >= 2) {
					uint8_t temp = status_data[1];
					json_out(",\"temp\":%u", temp);
				}
				break;
			default:
				if (length > 1) {
						json_out(",\"data\":[");
					for (int i = 1; i < length; i++) {
							json_out("%u%s", status_data[i], (i < length - 1) ? "," : "");
					}
						json_out("]");
				}
				break;
			}
			
			json_out("}\n");
		} else {
			log("[DEBUG] FTMS Machine Status [%u bytes]\n", length);
		}
		
		ftms_machine_status_len = length;
		memcpy(ftms_machine_status, data, length);
		bt_gatt_notify(NULL, &ftms_svc.attrs[5], ftms_machine_status, ftms_machine_status_len);
	}

	total_rx_count++;

	return BT_GATT_ITER_CONTINUE;
}
