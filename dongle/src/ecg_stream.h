/* ecg_stream.h - Vendor-neutral ECG sample emitter and per-vendor stream start */

#ifndef ECG_STREAM_H_
#define ECG_STREAM_H_

#include <zephyr/types.h>
#include "common.h"

/* GSP command-response HTTP status codes. Shared with notification_handler.c,
 * which also inspects COMMAND_RESULT payloads. */
#define GSP_HTTP_OK 200

/* Called once discovery has walked every service, so both the Polar PMD control
 * point and the GSP command handle are known. Starts the raw ECG stream for
 * whichever strap answered. A no-op when neither vendor service was found. */
void ecg_stream_start(struct bt_conn *conn, struct conn_slot *slot);

/* Emit one ECG batch as a single JSON line:
 *   {"type":"ecg","ts":<first sample ms>,"fs":<Hz>,"uv":[...]}
 * Samples are already microvolts. The batch is placed on a sample clock that is
 * anchored on the first packet and then advanced by the sample period, so BLE
 * arrival jitter never reaches the trace. */
void emit_ecg(struct conn_slot *slot, const int32_t *uv, int count, int fs_hz);

/* Reset the sample clock for a connection slot. Called on disconnect. */
void ecg_stream_reset(struct conn_slot *slot);

/* Log a Polar PMD control point indication: [0x00][op][type][status]. */
void ecg_handle_pmd_control_point(const uint8_t *data, uint16_t length);

/* Decode a complete GSP SBEM frame whose payload starts at buf[0], routed by the
 * client reference. Returns BT_GATT_ITER_CONTINUE. */
uint8_t gsp_parse_frame(struct conn_slot *slot, uint8_t ref,
			const uint8_t *buf, uint16_t len);

#endif /* ECG_STREAM_H_ */
