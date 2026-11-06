/* ecg_stream.c - Vendor-neutral ECG sample emitter and per-vendor stream start */

#include <zephyr/kernel.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/drivers/uart.h>
#include <zephyr/sys/byteorder.h>
#include <stdio.h>
#include <string.h>
#include "common.h"
#include "ecg_stream.h"

#define PMD_SAMPLE_RATE_HZ 130
#define GSP_SAMPLE_RATE_HZ 125

/* The console UART. Writing the whole record through the fifo is what makes a
 * short write detectable; see emit_ecg() for why printf() per sample lost bytes. */
static const struct device *const uart_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

/* The only ECG configuration the Polar H10 accepts: start measurement, type 0
 * (ECG), 130 Hz, 14-bit resolution. Every H10 client hard-codes this. */
static const uint8_t pmd_ecg_start[] = {
	0x02,                   /* START_MEASUREMENT                       */
	0x00,                   /* measurement type 0 = ECG                */
	0x00, 0x01, 0x82, 0x00, /* sample rate 0x0082 = 130 Hz             */
	0x01, 0x01, 0x0E, 0x00, /* resolution  0x000E = 14 bit             */
};

/* GSP command codes and response codes. GSP_HTTP_OK lives in ecg_stream.h so
 * notification_handler.c can use the same value when it inspects responses. */
#define GSP_CMD_HELLO     0x00
#define GSP_CMD_SUBSCRIBE 0x01
#define GSP_RESP_COMMAND  0x01

/* Write parameters and command buffers are per slot: a write must not have its
 * backing storage reused while it is still in flight. */
static struct bt_gatt_write_params pmd_write_params[MAX_CONNECTIONS];
static struct bt_gatt_write_params gsp_write_params[MAX_CONNECTIONS];
static uint8_t gsp_cmd_buf[MAX_CONNECTIONS][32];
static uint8_t gsp_step[MAX_CONNECTIONS];
static uint8_t gsp_ref_next = 1;

enum {
	GSP_STEP_IDLE = 0,
	GSP_STEP_HELLO,
	GSP_STEP_SUBSCRIBE_ECG,
	GSP_STEP_SUBSCRIBE_HR,
};

static int slot_index(struct conn_slot *slot)
{
	int idx = (int)(slot - connections);

	if (idx < 0 || idx >= MAX_CONNECTIONS) {
		return -1;
	}

	return idx;
}

/* Next client reference. References must be unique until UNSUBSCRIBE, and 0 is
 * reserved by the protocol. */
static uint8_t gsp_next_ref(void)
{
	uint8_t ref = gsp_ref_next++;

	if (gsp_ref_next == 0) {
		gsp_ref_next = 1;
	}
	if (ref == 0) {
		ref = gsp_ref_next++;
	}

	return ref;
}

static void gsp_send_next(struct bt_conn *conn, struct conn_slot *slot);

static void gsp_write_done(struct bt_conn *conn, uint8_t err,
			   struct bt_gatt_write_params *params)
{
	struct conn_slot *slot = NULL;
	int idx = -1;

	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (params == &gsp_write_params[i]) {
			slot = &connections[i];
			idx = i;
			break;
		}
	}

	if (!slot || !conn) {
		return;
	}

	if (err) {
		log("[GSP] Command write failed (err %u)\n", err);
		gsp_step[idx] = GSP_STEP_IDLE;
		return;
	}

	/* Advance the sequence: HELLO first, then the stream subscriptions. */
	gsp_send_next(conn, slot);
}

/* Send the next GSP command in the sequence for this slot. */
static void gsp_send_next(struct bt_conn *conn, struct conn_slot *slot)
{
	int idx = slot_index(slot);

	if (idx < 0 || !slot->movesense_cmd_handle) {
		return;
	}

	uint8_t *buf = gsp_cmd_buf[idx];
	uint16_t len = 0;

	switch (gsp_step[idx]) {
	case GSP_STEP_IDLE:
		gsp_step[idx] = GSP_STEP_HELLO;
		/* fall through */
	case GSP_STEP_HELLO:
		buf[len++] = GSP_CMD_HELLO;
		buf[len++] = gsp_next_ref();
		gsp_step[idx] = GSP_STEP_SUBSCRIBE_ECG;
		break;

	case GSP_STEP_SUBSCRIBE_ECG: {
		static const char path[] = "/Meas/ECG/125";

		buf[len++] = GSP_CMD_SUBSCRIBE;
		slot->gsp_ref_ecg = gsp_next_ref();
		buf[len++] = slot->gsp_ref_ecg;
		memcpy(&buf[len], path, sizeof(path) - 1); /* no NUL terminator */
		len += sizeof(path) - 1;
		gsp_step[idx] = GSP_STEP_SUBSCRIBE_HR;
		break;
	}

	case GSP_STEP_SUBSCRIBE_HR: {
		static const char path[] = "/Meas/HR";
		bool have_hrs = false;

		/* HRS already delivers RR for both straps. Only fall back to the
		 * GSP HR stream when this device has no Heart Rate Service, or
		 * every beat would be reported twice. */
		for (int i = 0; i < slot->subscribe_count; i++) {
			if (slot->sub_kind[i] == SUB_HR_MEASUREMENT) {
				have_hrs = true;
				break;
			}
		}

		if (have_hrs) {
			gsp_step[idx] = GSP_STEP_IDLE;
			slot->gsp_ecg_started = 1;
			log("[GSP] ECG stream requested (ref %u)\n", slot->gsp_ref_ecg);
			return;
		}

		buf[len++] = GSP_CMD_SUBSCRIBE;
		slot->gsp_ref_hr = gsp_next_ref();
		buf[len++] = slot->gsp_ref_hr;
		memcpy(&buf[len], path, sizeof(path) - 1);
		len += sizeof(path) - 1;
		gsp_step[idx] = GSP_STEP_IDLE;
		break;
	}

	default:
		gsp_step[idx] = GSP_STEP_IDLE;
		return;
	}

	memset(&gsp_write_params[idx], 0, sizeof(gsp_write_params[idx]));
	gsp_write_params[idx].func = gsp_write_done;
	gsp_write_params[idx].handle = slot->movesense_cmd_handle;
	gsp_write_params[idx].offset = 0;
	gsp_write_params[idx].data = buf;
	gsp_write_params[idx].length = len;

	int err = bt_gatt_write(conn, &gsp_write_params[idx]);

	if (err) {
		log("[GSP] Command write failed to start (err %d)\n", err);
		gsp_step[idx] = GSP_STEP_IDLE;
		return;
	}

	log("[GSP] Command 0x%02x sent [%u bytes]\n", buf[0], len);

	if (buf[0] == GSP_CMD_SUBSCRIBE) {
		slot->gsp_ecg_started = 1;
	}
}

static void pmd_write_done(struct bt_conn *conn, uint8_t err,
			   struct bt_gatt_write_params *params)
{
	(void)conn;
	(void)params;

	if (err) {
		log("[PMD] ECG start command failed (err %u)\n", err);
		return;
	}

	log("[PMD] ECG stream requested (130 Hz, 14 bit)\n");
}

void ecg_stream_start(struct bt_conn *conn, struct conn_slot *slot)
{
	if (!conn || !slot) {
		return;
	}

	int idx = slot_index(slot);

	if (idx < 0) {
		return;
	}

	if (slot->polar_pmd_cp_handle) {
		memset(&pmd_write_params[idx], 0, sizeof(pmd_write_params[idx]));
		pmd_write_params[idx].func = pmd_write_done;
		pmd_write_params[idx].handle = slot->polar_pmd_cp_handle;
		pmd_write_params[idx].offset = 0;
		pmd_write_params[idx].data = pmd_ecg_start;
		pmd_write_params[idx].length = sizeof(pmd_ecg_start);

		int err = bt_gatt_write(conn, &pmd_write_params[idx]);

		if (err) {
			log("[PMD] ECG start write failed to start (err %d)\n", err);
		}
		return;
	}

	if (slot->movesense_cmd_handle) {
		gsp_step[idx] = GSP_STEP_IDLE;
		gsp_send_next(conn, slot);
		return;
	}

	/* Neither vendor service was discovered: this is a device that only offers
	 * the standard profiles. Nothing to start. */
}

void ecg_stream_reset(struct conn_slot *slot)
{
	int idx = slot_index(slot);

	if (idx < 0) {
		return;
	}

	gsp_step[idx] = GSP_STEP_IDLE;
	slot->ecg_clock_valid = 0;
	slot->ecg_t0_ms = 0;
	slot->gsp_ecg_started = 0;
	slot->gsp_pending = false;
	slot->gsp_len = 0;
}

void emit_ecg(struct conn_slot *slot, const int32_t *uv, int count, int fs_hz)
{
	if (!slot || !uv || count <= 0 || fs_hz <= 0) {
		return;
	}

	if (count > ECG_MAX_SAMPLES) {
		count = ECG_MAX_SAMPLES;
	}

	uint32_t now = k_uptime_get_32();
	uint32_t span_ms = (uint32_t)(((int64_t)count * 1000) / fs_hz);

	if (!slot->ecg_clock_valid) {
		/* The batch was captured before this packet arrived, so backdate the
		 * clock by its own duration instead of stamping arrival time. */
		slot->ecg_t0_ms = now - span_ms;
		slot->ecg_clock_valid = 1;
	}

	/* One record per write, sized to fit.
	 *
	 * This used to be a printf() per sample (~20 writes per record). It was
	 * written that way because emitting a buffered record had once been
	 * followed by the dongle going unresponsive, and the note here blamed the
	 * buffering. The real cause is elsewhere: cdc_acm_poll_out() discards the
	 * byte whenever the TX ring buffer is full and cannot wait, because
	 * `flow_ctrl` is permanently false - it is set only from a DTS property
	 * (`hw-flow-control`) that zephyr,cdc-acm-uart.yaml does not define, and
	 * CONFIG_UART_USE_RUNTIME_CONFIGURE is off so cdc_acm_configure() cannot
	 * set it either. Every printf() character therefore risks being dropped
	 * when the link falls behind at 115200 baud, and a dropped ',' or '"'
	 * turns two numbers into one - the "impossible" ECG values and the
	 * missing samples that were observed on hardware.
	 *
	 * Writing the whole record to the UART fifo in one uart_fifo_fill() call
	 * instead means the ring buffer is filled whole-record-or-not-at-all, so it
	 * can never contain half a record followed by the start of the next one.
	 * Records are ~120 bytes against a 16 KB fifo, so this does not meaningfully
	 * change how often the buffer fills; it makes a short fill detectable
	 * instead of silently corrupting the stream.
	 *
	 * The record is assembled in a file-scope buffer, never on the stack.
	 * emit_ecg() runs in the Bluetooth receive workqueue thread, whose stack is
	 * CONFIG_BT_RX_STACK_SIZE (1536 bytes here). A 1 KiB local frame overruns
	 * it, and the failure looks exactly like the "dongle goes unresponsive"
	 * symptom: the image enumerates, then locks up hard enough that USB
	 * descriptor reads time out. Access is serialized by serial_output_mutex,
	 * so a single shared buffer is safe. */
	static char rec[ECG_RECORD_MAX];

	int n = snprintf(rec, sizeof(rec), "{\"type\":\"ecg\",\"ts\":%u,\"fs\":%d,\"uv\":[",
			 (unsigned)slot->ecg_t0_ms, fs_hz);

	if (n < 0) {
		return;
	}

	for (int i = 0; i < count && n < (int)sizeof(rec); i++) {
		int w = snprintf(&rec[n], sizeof(rec) - n, "%s%d", i ? "," : "", uv[i]);

		if (w <= 0 || (size_t)(n + w) >= sizeof(rec)) {
			break;
		}
		n += w;
	}

	if ((size_t)n + 3 < sizeof(rec)) {
		n += snprintf(&rec[n], sizeof(rec) - n, "]}\n");
	}

	/* A partial record would break every consumer after it, so drop it whole
	 * and count it. The sample clock is only advanced when the record is
	 * actually sent, so a dropped batch shows up as a timestamp gap rather than
	 * as samples silently attributed to the wrong time. */
	int wrote = 0;

	k_mutex_lock(&serial_output_mutex, K_FOREVER);
	if (n > 0 && (size_t)n < sizeof(rec)) {
		/* uart_fifo_fill() rather than uart_tx(): this build selects the
		 * interrupt-driven UART API (CONFIG_UART_INTERRUPT_DRIVEN), and
		 * CONFIG_UART_ASYNC_API is off, so uart_tx() does not exist. The CDC
		 * ACM driver exposes fifo_fill and returns the number of bytes it
		 * accepted, which is exactly the signal needed here. It is callable
		 * from a thread here because CONFIG_USBD_CDC_ACM_WORKQUEUE is off, so
		 * the driver's check_wq_ctx() is a no-op. */
		wrote = uart_fifo_fill(uart_dev, (const uint8_t *)rec, n);
	}
	k_mutex_unlock(&serial_output_mutex);

	if (wrote == n) {
		slot->ecg_t0_ms += span_ms;
	} else {
		slot->ecg_dropped++;
	}
}

void ecg_handle_pmd_control_point(const uint8_t *data, uint16_t length)
{
	if (!data || length < 3) {
		return;
	}

	/* Response: [0x00][op code][measurement type][status][...] */
	if (data[0] == 0x00) {
		uint8_t op = data[1];
		uint8_t status = (length >= 4) ? data[3] : 0xFF;

		if (status == 0) {
			log("[PMD] Control point op 0x%02x accepted\n", op);
		} else {
			log("[PMD] Control point op 0x%02x failed with error %u\n", op, status);
		}
	} else if (data[0] == 0xF0) {
		log("[PMD] Control point notification type 0xF0 [%u bytes]\n", length);
	}
}

uint8_t gsp_parse_frame(struct conn_slot *slot, uint8_t ref,
			const uint8_t *buf, uint16_t len)
{
	if (!slot || !buf) {
		return BT_GATT_ITER_CONTINUE;
	}

	if (len < 6) {
		log("[GSP] Frame too short (%u bytes)\n", len);
		return BT_GATT_ITER_CONTINUE;
	}

	/* Layout: [0x02][ref][uint32 timestamp ms][payload...] */
	uint32_t ts = sys_get_le32(&buf[2]);

	if (ref == slot->gsp_ref_ecg) {
		int count = (int)((len - 6) / 4);
		int32_t uv[ECG_MAX_SAMPLES];

		if (count <= 0) {
			return BT_GATT_ITER_CONTINUE;
		}
		if (count > ECG_MAX_SAMPLES) {
			count = ECG_MAX_SAMPLES;
		}

		for (int i = 0; i < count; i++) {
			int32_t raw = (int32_t)sys_get_le32(&buf[6 + i * 4]);

			/* Each sample is Q16.16 fixed point, NOT a plain ADC count.
			 *
			 * Measured, not assumed: across four captures of the live stream,
			 * 100% of samples are an exact integer multiple of 65536
			 * (residual < 1e-4 count), so the integer part sits in the upper
			 * 16 bits. An earlier revision of this file scaled the raw value
			 * directly, which reads the fractional bits as magnitude and
			 * reports every sample ~65536x too large - tens of volts.
			 *
			 * That comment argued Q16.16 "would need values >= 65536, so
			 * shifting yields 0 for every sample". That only holds for
			 * positive counts; a count of -1 is 0xFFFF0000 = -65536, which
			 * shifts to -1. The stream does contain large negative values,
			 * which is what made the flat-line theory fit.
			 *
			 * Divide rather than shift: >> rounds toward negative infinity and
			 * would bias every negative sample by one LSB. Multiply first so
			 * the sub-LSB remainder survives the division. */
			int64_t scaled = (int64_t)raw * 38147;   /* LSB = 0.38147 uV */

			scaled /= 65536;                         /* Q16.16 -> integer counts */
			uv[i] = (int32_t)(scaled / 100000);
		}

		emit_ecg(slot, uv, count, GSP_SAMPLE_RATE_HZ);
	} else if (ref == slot->gsp_ref_hr) {
		/* Layout: [0x02][ref][uint32 timestamp ms][uint16 rr ms]...
		 *
		 * No array header and no averaged bpm on the wire: the firmware
		 * forwards one R-R interval per beat, because Movesense exposes a
		 * per-beat resource and forwarding only the first interval of a
		 * batch would throw the rest away. Earlier revisions of this file
		 * expected [float avg][uint8 count][rr...], which read the low half
		 * of the first interval as a count and then shifted every real
		 * interval by two bytes - the JSON came out empty or wrong. */
		int count = (int)((len - 6) / 2);

		if (count <= 0) {
			return BT_GATT_ITER_CONTINUE;
		}

		/* GSP RR intervals are already in milliseconds, unlike the HRS
		 * 1/1024 s units, so both end up in ms in the JSON. */
		json_begin();
		json_out("{\"type\":\"hr\",\"ts\":%u,\"rssi\":%d,\"rr\":[",
			 k_uptime_get_32(), slot->rssi);
		for (int i = 0; i < count; i++) {
			json_out("%s%u", i ? "," : "", sys_get_le16(&buf[6 + i * 2]));
		}
		json_out("]}\n");
		json_end();
	} else {
		log("[GSP] Frame for unknown ref %u (%u bytes)\n", ref, len);
	}

	(void)ts;
	return BT_GATT_ITER_CONTINUE;
}
