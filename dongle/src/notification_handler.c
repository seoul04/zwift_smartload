/* notification_handler.c - Notification callback handlers */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/sys/byteorder.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>
#include "common.h"
#include "notification_handler.h"
#include "device_manager.h"
#include "ecg_stream.h"

/* Crank counters of the power meter, for cadence */
struct cp_cache cached_cp_data = {0};

/* The first notification proves the MTU exchange has settled, which is the only
 * place the final value can be read without polling. */
static void log_mtu_once(struct conn_slot *slot, struct bt_conn *conn)
{
	if (slot->mtu_logged) {
		return;
	}

	slot->mtu_logged = 1;
	log("Negotiated ATT MTU: %u\n", bt_gatt_get_mtu(conn));
}

/* Extract RR intervals from an HR Measurement (0x2A37) packet.
 * Layout: flags, HR (1 or 2 bytes), optional energy expended (bit 3), then
 * N x uint16 RR in 1/1024 s (bit 4). The offset depends on both flag bits.
 * Returns the number of intervals written to rr_ms. */
static int parse_hr_rr(const uint8_t *d, uint16_t length, uint16_t *rr_ms, int max)
{
	if (length < 2) {
		return 0;
	}

	uint8_t flags = d[0];
	int off = 1;

	off += (flags & 0x01) ? 2 : 1;   /* heart rate format */

	if (flags & 0x08) {              /* energy expended present */
		off += 2;
	}

	if (!(flags & 0x10)) {           /* RR interval present */
		return 0;
	}

	int n = 0;

	while (off + 2 <= (int)length && n < max) {
		uint16_t raw = sys_get_le16(&d[off]);

		/* 1/1024 s -> ms. raw * 1000 / 1024 is raw * 125 / 128, so the
		 * rounding error stays under 0.5 ms while keeping this integer. */
		rr_ms[n++] = (uint16_t)(((uint32_t)raw * 1000u + 512u) / 1024u);
		off += 2;
	}

	return n;
}

/* Polar PMD Data frame: [0x00][8-byte ns timestamp][frame type][samples...].
 * Frame type 0 is uncompressed ECG with 3-byte signed little-endian uV values. */
static void handle_pmd_data(struct conn_slot *slot, const uint8_t *d, uint16_t length)
{
	if (length < 10) {
		log("[PMD] Short data frame (%u bytes)\n", length);
		return;
	}

	if (d[0] != 0x00) {
		/* Not an ECG measurement frame. */
		return;
	}

	if (d[9] != 0x00) {
		log("[PMD] ECG frame type %u not supported\n", d[9]);
		return;
	}

	int count = (length - 10) / 3;
	int32_t uv[ECG_MAX_SAMPLES];

	if (count > ECG_MAX_SAMPLES) {
		count = ECG_MAX_SAMPLES;
	}

	for (int i = 0; i < count; i++) {
		const uint8_t *p = &d[10 + i * 3];
		int32_t v = p[0] | (p[1] << 8) | (p[2] << 16);

		if (v & 0x800000) {
			v -= 0x1000000;  /* sign-extend 24-bit */
		}
		uv[i] = v;               /* already microvolts */
	}

	/* The PMD timestamp is nanoseconds since 2000 and is deliberately ignored:
	 * emit_ecg keeps a sample clock anchored on the first frame. */
	emit_ecg(slot, uv, count, 130);
}

/* Movesense GSP DATA / DATA_PART2 framing. A DATA packet starts a frame and
 * DATA_PART2 continues it; the continuation carries no length, so the frame is
 * assembled into the slot buffer and parsed when the next DATA arrives. */
static void handle_gsp_data(struct conn_slot *slot, const uint8_t *d, uint16_t length)
{
	if (length < 2) {
		return;
	}

	uint8_t type = d[0];
	uint8_t ref = d[1];

	if (type == 0x01) {
		/* Command response: [0x01][ref][payload].
		 *
		 * The payload is NOT always a 2-byte HTTP status. The Movesense GSP
		 * protocol uses two shapes:
		 *
		 *   HELLO     -> [0x01][ref]['H']['e']['l']['l']['o']  (7 bytes, ASCII)
		 *   otherwise -> [0x01][ref][uint16 status BE]          e.g. 0x01FB = 507
		 *
		 * Reading "Hello" as a uint16 picks up "He" = 0x6548 = 25928, which
		 * looks like a fatal HTTP error but is only the greeting. The two are
		 * unambiguous: a real status is 4 bytes total, the greeting is 7. */
		if (length == 7 && d[2] == 'H' && d[3] == 'e' && d[4] == 'l' &&
		    d[5] == 'l' && d[6] == 'o') {
			log("[GSP] HELLO acknowledged (ref %u)\n", ref);
			return;
		}

		if (length >= 4) {
			/* Big-endian, not little. The stock firmware's 507 error is
			 * {0x01, 0xFB}, and 507 == 0x01FB only when read big-endian;
			 * little-endian would give 0xFB01 == 64257. The subscribe
			 * acknowledgement is 200 == 0x00C8 and would likewise be
			 * mangled to 0xC800. */
			uint16_t status = sys_get_be16(&d[2]);

			if (status != GSP_HTTP_OK) {
				log("[GSP] Command ref %u failed with HTTP %u\n", ref, status);
			} else {
				log("[GSP] Command ref %u accepted\n", ref);
			}
		}
		return;
	}

	if (type == 0x02) {
		/* A new frame starts. If one is already buffered it never received a
		 * continuation, so parse what we have before replacing it. */
		if (slot->gsp_pending && slot->gsp_len > 0) {
			gsp_parse_frame(slot, slot->gsp_ref, slot->gsp_buf, slot->gsp_len);
		}
		slot->gsp_pending = false;
		slot->gsp_len = 0;
	} else if (type != 0x03) {
		return;
	}

	if (slot->gsp_len + (length - 2) > GSP_BUF_SIZE) {
		log("[GSP] Frame overflow (%u + %u bytes), dropping\n",
		    slot->gsp_len, length - 2);
		slot->gsp_pending = false;
		slot->gsp_len = 0;
		return;
	}

	/* Buffer the SBEM payload without the 2-byte GSP header. */
	memcpy(&slot->gsp_buf[slot->gsp_len], &d[2], length - 2);
	slot->gsp_len += length - 2;
	slot->gsp_ref = ref;

	if (type == 0x02) {
		/* Remember the frame in case a continuation follows. */
		slot->gsp_pending = true;
	}
}

static int get_battery_level_for_conn(struct bt_conn *conn)
{
	const bt_addr_le_t *dst = bt_conn_get_dst(conn);
	struct device_info *dev_info;

	SYS_SLIST_FOR_EACH_CONTAINER(&device_list, dev_info, node) {
		if (!bt_addr_le_cmp(&dev_info->addr, dst)) {
			return dev_info->battery_level;
		}
	}

	return -1;
}

static void json_out_battery_field(int battery_level)
{
	if (battery_level >= 0) {
		json_out(",\"battery_level\":%d", battery_level);
	} else {
		json_out(",\"battery_level\":null");
	}
}

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

	/* Route by characteristic UUID: PMD and GSP both live inside a 128-bit
	 * service, so a service index cannot identify what a notification carries. */
	enum sub_kind kind = slot->sub_kind[sub_idx];

	if (kind == SUB_NONE) {
		log("[DEBUG] Unknown subscription kind (length=%u, handle=%u)\n", length, params->value_handle);
		return BT_GATT_ITER_CONTINUE;
	}

	log_mtu_once(slot, conn);

	if (kind == SUB_POLAR_PMD_CONTROL) {
		ecg_handle_pmd_control_point(data, length);
	} else if (kind == SUB_POLAR_PMD_DATA) {
		handle_pmd_data(slot, data, length);
	} else if (kind == SUB_MOVESENSE_GSP_DATA) {
		handle_gsp_data(slot, data, length);
	} else if (kind == SUB_HR_MEASUREMENT) {
		const uint8_t *hr_data = data;
		int battery_level = get_battery_level_for_conn(conn);
		uint16_t rr[8];
		int rr_count = 0;

		/* Bug fix: the flags byte used to be read before the length check. */
		if (length < 2) {
			log("[DEBUG] Invalid HR data length: %u\n", length);
			return BT_GATT_ITER_CONTINUE;
		}

		uint8_t flags = hr_data[0];
		uint16_t heart_rate;

		if (flags & 0x01) {
			if (length < 3) {
				log("[DEBUG] Invalid HR data length for UINT16: %u\n", length);
				return BT_GATT_ITER_CONTINUE;
			}
			heart_rate = sys_get_le16(&hr_data[1]);
		} else {
			heart_rate = hr_data[1];
		}

		rr_count = parse_hr_rr(hr_data, length, rr, ARRAY_SIZE(rr));

		/* Track how many consecutive packets lack RR: a strap that never sends
		 * RR through HRS needs the GSP /Meas/HR fallback. */
		if (rr_count > 0) {
			slot->hr_rr_misses = 0;
		} else if (slot->hr_rr_misses < 255) {
			slot->hr_rr_misses++;
		}

		json_begin();
		json_out("{\"type\":\"hr\",\"ts\":%u,\"bpm\":%u,\"rssi\":%d", k_uptime_get_32(), heart_rate, slot->rssi);
		json_out_battery_field(battery_level);

		if (rr_count > 0) {
			/* Integer milliseconds, so the server never needs to know which
			 * strap produced them. */
			json_out(",\"rr\":[");
			for (int i = 0; i < rr_count; i++) {
				json_out("%s%u", i ? "," : "", rr[i]);
			}
			json_out("]");
		}
		json_out("}\n");
		json_end();
	} else if (kind == SUB_CP_MEASUREMENT) {
		/* CP service */
		int battery_level = get_battery_level_for_conn(conn);
		last_cp_data_time = k_uptime_get_32();
		const uint8_t *cp_data = data;
		
		if (length >= 4) {
			uint16_t flags = sys_le16_to_cpu(*(uint16_t *)&cp_data[0]);
			int16_t power = sys_le16_to_cpu(*(uint16_t *)&cp_data[2]);
			int offset = 4;
			
			json_begin();
			json_out("{\"type\":\"cp\",\"ts\":%u,\"power\":%d,\"flags\":%u,\"rssi\":%d", last_cp_data_time, power, flags, slot->rssi);
			json_out_battery_field(battery_level);
			
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
			json_end();
		}
	} else if (kind == SUB_FTMS_BIKE_DATA) {
		/* FTMS Indoor Bike Data */
		const uint8_t *ftms_data = data;
		int battery_level = get_battery_level_for_conn(conn);
		uint16_t flags = 0;
		uint32_t now = k_uptime_get_32();
		
		if (length >= 2) {
			flags = sys_le16_to_cpu(*(uint16_t *)&ftms_data[0]);
			int offset = 2;
			
			json_begin();
			json_out("{\"type\":\"ftms\",\"ts\":%u,\"flags\":%u,\"rssi\":%d", now, flags, slot->rssi);
			json_out_battery_field(battery_level);
			
			/* Instantaneous Speed */
			if (length >= offset + 2) {
				uint16_t speed = sys_le16_to_cpu(*(uint16_t *)&ftms_data[offset]);
				json_out(",\"speed\":%u", speed);
			}
			offset += 2;
			
			if (flags & 0x0002) {
				offset += 2;
			}
			
			if (flags & 0x0004) {
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
			
			if (flags & 0x0040) {
				if (length >= offset + 2) {
					int16_t ftms_power = sys_le16_to_cpu(*(uint16_t *)&ftms_data[offset]);
					json_out(",\"power\":%d", ftms_power);
				}
				offset += 2;
			}

			json_out("}\n");
			json_end();
		}
	} else if (kind == SUB_FTMS_TRAINING) {
		/* FTMS Training Status. Previously unreachable: the service index could
		 * never be 3, so these packets fell through to the Indoor Bike Data
		 * branch and were parsed as speed/power. */
		const uint8_t *ts_data = data;

		if (length >= 1) {
			json_begin();
			json_out("{\"type\":\"training_status\",\"ts\":%u,\"flags\":%u",
				 k_uptime_get_32(), ts_data[0]);
			if (length >= 2) {
				json_out(",\"status\":%u", ts_data[1]);
			}
			json_out("}\n");
			json_end();
		}
	} else if (kind == SUB_FTMS_STATUS) {
		/* FTMS Machine Status */
		const uint8_t *status_data = data;
		uint32_t now = k_uptime_get_32();
		
		if (length >= 1) {
			uint8_t op_code = status_data[0];
			
			json_begin();
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
			json_end();
		} else {
			log("[DEBUG] FTMS Machine Status [%u bytes]\n", length);
		}
	}

	total_rx_count++;

	return BT_GATT_ITER_CONTINUE;
}
