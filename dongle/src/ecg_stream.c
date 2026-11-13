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

/*
 * Movesense ECG rate. The sensor's /Meas/ECG/{RequiredSampleRate} resource
 * accepts 125, 128, 200, 250, 256, 500 and 512 Hz (the MAX30004 provider's
 * AVAILABLE_ECG_SAMPLE_RATES table in the core lib); the path below is the
 * whole of the choice, because the driver clocks the AFE to whatever is asked
 * for and stamps nothing of its own.
 *
 * 256 Hz is the firmware's documented rate. It buys 3.9 ms sample spacing
 * instead of 8 ms, which matters for the beat detector: it can only place a
 * QRS apex on a whole sample before the parabola fit refines it, so the finest
 * correction it can make is half a step - 2 ms here rather than 4 ms.
 *
 * Cost on the wire is negligible: one 15-sample record is ~102 bytes, so this
 * runs ~1.7 kB/s against a 11520 B/s console link, and ECG_MAX_SAMPLES (80)
 * still covers the ~20 samples that fit one MTU-92 notification.
 *
 * This must match the number in the path. emit_ecg() divides by it to turn a
 * sample count into the milliseconds it spans, and the server copies the value
 * into the EDF header, so a mismatch silently rewrites the file's timeline.
 */
#define GSP_SAMPLE_RATE_HZ 256

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

/* GSP response codes. The command codes and HTTP statuses live in
 * ecg_stream.h so notification_handler.c can share them. */
#define GSP_RESP_COMMAND  0x01

/* Write parameters and command buffers are per slot: a write must not have its
 * backing storage reused while it is still in flight. */
static struct bt_gatt_write_params pmd_write_params[MAX_CONNECTIONS];
static struct bt_gatt_write_params gsp_write_params[MAX_CONNECTIONS];
static uint8_t gsp_cmd_buf[MAX_CONNECTIONS][32];
static uint8_t gsp_step[MAX_CONNECTIONS];
static uint8_t gsp_ref_next = 1;

/* Retry budget per connection. A failed GSP command write used to end the
 * sequence for the life of the connection, so a single transient error left
 * ECG dead while the link stayed up - indistinguishable from "the stream just
 * stopped". Retry conservatively: one attempt after a short delay, and only
 * GSP_RETRY_MAX times, so a genuinely broken peer is not hammered. */
#define GSP_RETRY_MAX 2
#define GSP_RETRY_DELAY_MS 1500

static struct k_work_delayable gsp_retry_work[MAX_CONNECTIONS];
static uint8_t gsp_retry_count[MAX_CONNECTIONS];
static uint8_t gsp_reclaim_count[MAX_CONNECTIONS];
/* A GATT write must finish before its params are reused. Two paths can now ask
 * for the next command - the write callback and the deferred repair work - so
 * without this a repair could land on top of an in-flight write and reuse
 * gsp_write_params[] while the stack still owned it. */
static uint8_t gsp_busy[MAX_CONNECTIONS];
/* Set once the watchdog has exhausted its restart budget on a connection, so the
 * "gave up" line is logged once instead of every health interval. */
static bool gsp_gave_up[MAX_CONNECTIONS];
/* Set when a SUBSCRIBE was refused with 409. The repair must not run while the
 * command sequence is still in flight (a second write would reuse the write
 * parameters of the one in flight), so it is deferred to the retry work item,
 * which runs after the sequence has settled. */
static uint8_t gsp_reclaim_pending[MAX_CONNECTIONS];

#define GSP_RECLAIM_DELAY_MS 300

/* Stream health reporting. A stream that stops producing records is the failure
 * this whole file exists to prevent, and it was invisible: the console simply
 * went quiet, which looks identical to "the strap is off". Logging a periodic
 * line with frame count, rate and worst gap makes a stall obvious and localises
 * it to connect / subscribe / decode. */
#define ECG_HEALTH_INTERVAL_MS 10000

static struct k_work_delayable ecg_health_work;

enum {
	GSP_STEP_IDLE = 0,
	GSP_STEP_UNSUBSCRIBE,
	GSP_STEP_HELLO,
	GSP_STEP_SUBSCRIBE_ECG,
	GSP_STEP_SUBSCRIBE_HR,
	/* A SUBSCRIBE was refused with 409, so the sensor is still running the
	 * resource under a reference only it knows. gsp_parse_frame() recovers that
	 * reference from the frames it keeps sending; this step then releases it
	 * with a real UNSUBSCRIBE so the resource is free to be claimed properly. */
	GSP_STEP_RECLAIM,
	GSP_STEP_RESUB_ECG,
	/* Watchdog repair: the sensor went silent, so release the reference we
	 * hold and then subscribe afresh. Re-subscribing without the release leaks
	 * one subscription per attempt, and measured on hardware the sensor stops
	 * honouring new ones after a few - the stream never came back. */
	GSP_STEP_WD_UNSUB,
	/* Terminal: the sequence finished and the stream has been requested. It is
	 * distinct from IDLE because IDLE means "start a sequence", and reaching it
	 * again after completion would re-run HELLO and re-subscribe forever. */
	GSP_STEP_DONE,
};

/* Human-readable step name. The subscribe sequence had no visibility at all:
 * the only trace of a 409 was a status line with no indication of which stage
 * had failed or what the firmware did next. */
static const char *gsp_step_name(uint8_t step)
{
	switch (step) {
	case GSP_STEP_IDLE:          return "IDLE";
	case GSP_STEP_UNSUBSCRIBE:   return "UNSUBSCRIBE(persisted)";
	case GSP_STEP_HELLO:         return "HELLO";
	case GSP_STEP_SUBSCRIBE_ECG: return "SUBSCRIBE(ECG)";
	case GSP_STEP_SUBSCRIBE_HR:  return "SUBSCRIBE(HR)";
	case GSP_STEP_RECLAIM:       return "RECLAIM(adopted ref)";
	case GSP_STEP_RESUB_ECG:     return "RESUBSCRIBE(ECG)";
	case GSP_STEP_WD_UNSUB:      return "WATCHDOG-UNSUB";
	case GSP_STEP_DONE:          return "DONE";
	default:                     return "?";
	}
}

/* How many times a 409 may be repaired on one connection. One reclaim is
 * normally enough; the second only covers a sensor that races the unsubscribe.
 * Without a cap a sensor that always answers 409 would loop the sequence. */
#define GSP_RECLAIM_MAX 2

/* Stream watchdog.
 *
 * The Movesense ends the /Meas/ECG measurement on its own while the BLE link
 * stays up: measured on hardware, GSP notifications stop dead after ~12 s while
 * HRS notifications keep arriving on the same connection for minutes. Nothing
 * on this side can observe why - the sensor simply stops sending - so the only
 * recovery is to ask for the stream again.
 *
 * Silence is treated as a stall only after the watchdog has covered the stream
 * at least once, which is what ECG_STALL_GRACE_MS allows for. The gap that
 * triggers a restart is deliberately far above one frame period (62.5 ms) so
 * ordinary jitter never restarts a healthy stream.
 */
#define ECG_STALL_GRACE_MS 4000
#define ECG_STALL_GAP_MS   3000
/* Restart budget, deliberately small.
 *
 * Measured: a stream that has genuinely died does not come back, and each
 * attempt costs a GSP reference. With a budget of 40 the firmware churned
 * through refs 106..115 in five minutes and the sensor then stopped answering
 * GSP commands altogether - the recovery mechanism was destroying the session
 * it was meant to repair. A handful of attempts covers a transient stall;
 * beyond that the honest answer is that the measurement is gone, and hammering
 * the sensor only makes the next connect harder.
 *
 * Backoff doubles per failed attempt so repeated restarts do not pile up. */
#define GSP_RESTART_MAX    4
#define GSP_RESTART_BACKOFF_MS 3000

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

	gsp_busy[idx] = 0;

	if (err) {
		log("[GSP] Command write failed (err %u)\n", err);
		gsp_step[idx] = GSP_STEP_IDLE;

		/* One short, capped retry rather than abandoning the stream. Reset the
		 * step to IDLE so the sequence restarts at HELLO. */
		if (gsp_retry_count[idx] < GSP_RETRY_MAX) {
			gsp_retry_count[idx]++;
			gsp_step[idx] = GSP_STEP_IDLE;
			k_work_reschedule(&gsp_retry_work[idx],
					  K_MSEC(GSP_RETRY_DELAY_MS));
			log("[GSP] Retry %u/%u in %u ms\n", gsp_retry_count[idx],
			    GSP_RETRY_MAX, GSP_RETRY_DELAY_MS);
		} else {
			log("[GSP] Giving up on subscription after %u retries\n",
			    gsp_retry_count[idx]);
		}
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

	if (gsp_busy[idx]) {
		/* A command is in flight. Whatever was asked for will run from the
		 * write callback, so drop this request rather than racing it. */
		log("[GSP] Next command deferred: a write is already in flight\n");
		return;
	}

again:
	switch (gsp_step[idx]) {
	case GSP_STEP_IDLE:
		/* If the previous session left subscriptions behind, drop them
		 * before starting new ones. The sensor keeps a subscription alive
		 * until it is told otherwise, so without this it can continue
		 * streaming under a reference this side has already discarded. */
		if (slot->gsp_ref_ecg_prev || slot->gsp_ref_hr_prev) {
			log("[GSP] Slots enter: keeping persisted refs "
			    "ecg=%u hr=%u for UNSUBSCRIBE\n",
			    slot->gsp_ref_ecg_prev, slot->gsp_ref_hr_prev);
			gsp_step[idx] = GSP_STEP_UNSUBSCRIBE;
		} else {
			/* Say so explicitly. After a fresh flash there are no
			 * persisted references, so the sensor's own stale subscription
			 * cannot be released and the SUBSCRIBE below will be refused.
			 * That is exactly what makes the 409 look mysterious. */
			log("[GSP] Slots enter with no persisted refs "
			    "(fresh boot or new slot): cannot release the sensor's "
			    "stale subscription, expect 409 on subscribe\n");
			gsp_step[idx] = GSP_STEP_HELLO;
		}
		goto again;

	case GSP_STEP_UNSUBSCRIBE: {
		uint8_t ref = slot->gsp_ref_ecg_prev ? slot->gsp_ref_ecg_prev
						    : slot->gsp_ref_hr_prev;

		buf[len++] = GSP_CMD_UNSUBSCRIBE;
		buf[len++] = ref;
		log("[GSP] Step UNSUBSCRIBE(persisted): releasing ref %u (%s)\n",
		    ref, ref == slot->gsp_ref_ecg_prev ? "ecg" : "hr");
		if (ref == slot->gsp_ref_ecg_prev) {
			slot->gsp_ref_ecg_prev = 0;
		} else {
			slot->gsp_ref_hr_prev = 0;
		}
		/* Stay on this step until both stale references are cleared. */
		gsp_step[idx] = (slot->gsp_ref_ecg_prev || slot->gsp_ref_hr_prev)
				    ? GSP_STEP_UNSUBSCRIBE
				    : GSP_STEP_HELLO;
		break;
	}
	case GSP_STEP_HELLO:
		buf[len++] = GSP_CMD_HELLO;
		buf[len++] = gsp_next_ref();
		gsp_step[idx] = GSP_STEP_SUBSCRIBE_ECG;
		break;

	case GSP_STEP_SUBSCRIBE_ECG: {
		/* The rate here and GSP_SAMPLE_RATE_HZ must agree; see the
		 * comment on that define. */
		static const char path[] = "/Meas/ECG/256";

		buf[len++] = GSP_CMD_SUBSCRIBE;
		slot->gsp_ref_ecg = gsp_next_ref();
		buf[len++] = slot->gsp_ref_ecg;
		memcpy(&buf[len], path, sizeof(path) - 1); /* no NUL terminator */
		len += sizeof(path) - 1;
		gsp_step[idx] = GSP_STEP_SUBSCRIBE_HR;
		break;
	}

	/* Adopt the reference the sensor is actually streaming under and hand it
	 * back with a real UNSUBSCRIBE. This is the repair for a 409: the sensor
	 * refuses a second subscription for a resource that is still running, and
	 * frames from the old subscription keep arriving carrying its reference.
	 * Releasing it properly frees the resource, so the resubscribe that follows
	 * becomes an ordinary success instead of a permanently tolerated conflict.
	 *
	 * The adopted reference is used immediately and then cleared, because the
	 * subscription it belongs to ceases to exist the moment it is released. */
	case GSP_STEP_RECLAIM: {
		if (gsp_reclaim_count[idx] >= GSP_RECLAIM_MAX) {
			log("[GSP] Step RECLAIM: budget exhausted (%u), "
			    "staying on the adopted stream\n",
			    gsp_reclaim_count[idx]);
			gsp_step[idx] = GSP_STEP_DONE;
			slot->gsp_ecg_started = 1;
			slot->ecg_start_ms = k_uptime_get_32();
			return;
		}

		uint8_t stale = slot->gsp_ref_ecg;

		if (stale == 0) {
			/* Adoption has not happened yet: no frame has arrived to
			 * carry the reference. Wait rather than burning the budget;
			 * gsp_parse_frame() reschedules this step once it has a
			 * reference in hand. */
			log("[GSP] Step RECLAIM: no adopted ref yet, waiting for a "
			    "frame\n");
			return;
		}

		gsp_reclaim_count[idx]++;
		buf[len++] = GSP_CMD_UNSUBSCRIBE;
		buf[len++] = stale;
		log("[GSP] Step RECLAIM: releasing adopted ref %u "
		    "(attempt %u/%u)\n", stale, gsp_reclaim_count[idx],
		    GSP_RECLAIM_MAX);

		/* The subscription named by this reference is about to end, so stop
		 * matching frames on it. Clearing here also re-arms adoption, in
		 * case the frames keep coming under a yet newer reference. */
		slot->gsp_ref_ecg = 0;
		gsp_step[idx] = GSP_STEP_RESUB_ECG;
		break;
	}

	case GSP_STEP_RESUB_ECG: {
		static const char path[] = "/Meas/ECG/256";

		buf[len++] = GSP_CMD_SUBSCRIBE;
		slot->gsp_ref_ecg = gsp_next_ref();
		buf[len++] = slot->gsp_ref_ecg;
		memcpy(&buf[len], path, sizeof(path) - 1);
		len += sizeof(path) - 1;
		/* This is a fresh, clean subscription, so a later 409 was not caused
		 * by the conflict that was just repaired. */
		slot->gsp_conflict = 0;
		log("[GSP] Step RESUBSCRIBE(ECG): new ref %u, conflict repaired\n",
		    slot->gsp_ref_ecg);
		gsp_step[idx] = GSP_STEP_SUBSCRIBE_HR;
		break;
	}

	case GSP_STEP_SUBSCRIBE_HR: {
		static const char path[] = "/Meas/HR";
		bool have_hrs = false;

#ifdef HR_OFF
		/* Diagnostic: no Heart Rate consumer at all.
		 *
		 * On the Movesense, HRS and GSP /Meas/HR both end up at the MAX3000x,
		 * the same AFE that produces /Meas/ECG. HrBridge subscribes
		 * /Component/MAX3000x/HeartRateData when a client enables 0x2A37 and
		 * restarts the beat detector each time, so either path can disturb the
		 * ECG measurement. Measured on the host, ECG was clean 5/5 with 0x2A37
		 * left off and failed 2/4 with it on; this build tests whether the
		 * dongle's stalls follow the same rule.
		 */
		gsp_step[idx] = GSP_STEP_DONE;
		slot->gsp_ecg_started = 1;
		slot->ecg_start_ms = k_uptime_get_32();
		log("[GSP] ECG stream requested (ref %u), HR disabled by HR_OFF\n",
		    slot->gsp_ref_ecg);
		return;
#endif

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
			gsp_step[idx] = GSP_STEP_DONE;
			slot->gsp_ecg_started = 1;
			slot->ecg_start_ms = k_uptime_get_32();
			log("[GSP] ECG stream requested (ref %u)\n", slot->gsp_ref_ecg);
			return;
		}

		buf[len++] = GSP_CMD_SUBSCRIBE;
		slot->gsp_ref_hr = gsp_next_ref();
		buf[len++] = slot->gsp_ref_hr;
		memcpy(&buf[len], path, sizeof(path) - 1);
		len += sizeof(path) - 1;
		gsp_step[idx] = GSP_STEP_DONE;
		slot->gsp_ecg_started = 1;
		slot->ecg_start_ms = k_uptime_get_32();
		break;
	}

	case GSP_STEP_WD_UNSUB: {
		/* Release the silent stream's reference before subscribing again.
		 * If there is no reference to release, subscribe directly: the
		 * sensor has nothing of ours outstanding. */
		if (slot->gsp_ref_ecg == 0) {
			gsp_step[idx] = GSP_STEP_SUBSCRIBE_ECG;
			goto again;
		}

		buf[len++] = GSP_CMD_UNSUBSCRIBE;
		buf[len++] = slot->gsp_ref_ecg;
		log("[GSP] Step WATCHDOG-UNSUB: releasing silent ref %u\n",
		    slot->gsp_ref_ecg);
		slot->gsp_ref_ecg = 0;
		gsp_step[idx] = GSP_STEP_SUBSCRIBE_ECG;
		break;
	}

	case GSP_STEP_DONE:
		/* Sequence complete. Nothing more to send; the watchdog in
		 * ecg_health_handler() re-arms the stream if it goes silent. */
		return;

	default:
		gsp_step[idx] = GSP_STEP_DONE;
		return;
	}

	memset(&gsp_write_params[idx], 0, sizeof(gsp_write_params[idx]));
	gsp_write_params[idx].func = gsp_write_done;
	gsp_write_params[idx].handle = slot->movesense_cmd_handle;
	gsp_write_params[idx].offset = 0;
	gsp_write_params[idx].data = buf;
	gsp_write_params[idx].length = len;

	/* Remember what is in flight. The response arrives on a notification and
	 * carries only a reference, so without this the status line cannot say
	 * which command it belongs to. */
	slot->gsp_last_cmd = buf[0];
	slot->gsp_last_ref = (len > 1) ? buf[1] : 0;

	int err = bt_gatt_write(conn, &gsp_write_params[idx]);

	if (err) {
		log("[GSP] Command write failed to start (err %d)\n", err);
		gsp_busy[idx] = 0;
		gsp_step[idx] = GSP_STEP_IDLE;
		return;
	}

	gsp_busy[idx] = 1;

	log("[GSP] Command 0x%02x ref %u sent [%u bytes] step=%s\n", buf[0],
	    (len > 1) ? buf[1] : 0, len, gsp_step_name(gsp_step[idx]));

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

static void gsp_retry_handler(struct k_work *work)
{
	/* Recover the slot index from the work item. */
	int idx = (int)((struct k_work_delayable *)work - gsp_retry_work);

	if (idx < 0 || idx >= MAX_CONNECTIONS) {
		return;
	}

	struct conn_slot *slot = &connections[idx];

	if (!slot->conn) {
		return;
	}

	if (gsp_busy[idx]) {
		/* The sequence is mid-write. The callback advances it, and a repair
		 * that wants to run will be picked up on the next scheduling. */
		if (gsp_reclaim_pending[idx]) {
			log("[GSP] Repair postponed: command write in flight\n");
			k_work_reschedule(&gsp_retry_work[idx],
					  K_MSEC(GSP_RECLAIM_DELAY_MS));
		}
		return;
	}

	/* A refused SUBSCRIBE is repaired before anything else: the resource is
	 * still running under the reference the frames carry, so releasing it is
	 * what makes the next subscribe succeed. */
	if (gsp_reclaim_pending[idx]) {
		gsp_reclaim_pending[idx] = 0;
		log("[GSP] Running deferred repair for the refused subscribe\n");
		gsp_step[idx] = GSP_STEP_RECLAIM;
		gsp_send_next(slot->conn, slot);
		return;
	}

	log("[GSP] Retrying subscription on slot %d from step %s\n", idx,
	    gsp_step_name(gsp_step[idx]));
	gsp_send_next(slot->conn, slot);
}

/* Ask for the 409 repair once the command sequence has settled. Called from the
 * response handler, which runs in the ATT notification context and must not
 * start a GATT write of its own. */
void gsp_note_conflict(struct conn_slot *slot)
{
	int idx = slot_index(slot);

	if (idx < 0) {
		return;
	}

	slot->gsp_conflict = 1;

	/* Nothing to release and no sequence in flight: the reference has to be
	 * learned from a frame first. gsp_parse_frame() schedules this when it
	 * adopts one. */
	gsp_step[idx] = GSP_STEP_RECLAIM;
	gsp_reclaim_pending[idx] = 1;
	k_work_reschedule(&gsp_retry_work[idx], K_MSEC(GSP_RECLAIM_DELAY_MS));
	log("[GSP] 409 noted; repair scheduled in %u ms\n",
	    GSP_RECLAIM_DELAY_MS);
}

/* Periodic one-line stream health. Silent when nothing is connected, so it does
 * not add noise to an idle console. */
static void ecg_health_handler(struct k_work *work)
{
	(void)work;

	uint32_t now = k_uptime_get_32();

	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		struct conn_slot *slot = &connections[i];

		if (!slot->conn) {
			continue;
		}

		if (!slot->movesense_cmd_handle) {
			continue;  /* not a Movesense peer */
		}

		uint32_t since = slot->ecg_last_frame_ms
				     ? (now - slot->ecg_last_frame_ms)
				     : 0;
		bool stalled = false;

		/* Stream watchdog.
		 *
		 * Measured behaviour: the sensor stops sending GSP frames while HRS
		 * keeps notifying on the same connection, so the link is healthy and
		 * the only observable is silence. Nothing restarts the measurement by
		 * itself, so a dead stream stays dead until the device is power
		 * cycled - the "flaky ECG" symptom exactly.
		 *
		 * Re-subscribe for the same resource: the sensor is not streaming, so
		 * there is nothing to conflict with, and if it has quietly started
		 * again the 409 path already handles it. */
		if (slot->gsp_ecg_started && (now - slot->ecg_start_ms) > ECG_STALL_GRACE_MS) {
			/* Time since the stream last delivered a record. If it has
			 * never delivered one, measure from the request instead, so a
			 * subscribe that produced nothing is retried too. */
			uint32_t silence = slot->ecg_last_frame_ms
					     ? (now - slot->ecg_last_frame_ms)
					     : (now - slot->ecg_start_ms);

			if (silence > ECG_STALL_GAP_MS) {
				stalled = true;

				/* Only repair while the sensor is still talking to us.
				 * If no GSP notification of any kind has arrived recently,
				 * the sensor is not merely silent on ECG - the whole GSP
				 * session is gone, and re-subscribing just consumes
				 * references. A reconnect is the only thing that helps, and
				 * the connection timeout will produce one. */
				if (slot->gsp_ecg_started &&
				    gsp_step[i] == GSP_STEP_DONE &&
				    !gsp_busy[i] &&
				    slot->gsp_restarts < GSP_RESTART_MAX) {
					slot->gsp_restarts++;
					log("[ECG] slot %d: no ECG for %lums while the "
					    "link is up; releasing ref %u and "
					    "re-subscribing (attempt %u/%u)\n",
					    i, (unsigned long)silence,
					    slot->gsp_ref_ecg, slot->gsp_restarts,
					    GSP_RESTART_MAX);
					gsp_step[i] = GSP_STEP_WD_UNSUB;
					gsp_send_next(slot->conn, slot);
				} else if (slot->gsp_restarts >= GSP_RESTART_MAX) {
					/* Budget spent. Say so once rather than every
					 * interval. */
					if (!gsp_gave_up[i]) {
						gsp_gave_up[i] = true;
						log("[ECG] slot %d: stream did not "
						    "recover after %u attempts; "
						    "reconnecting, since only a new "
						    "connection revives the sensor's "
						    "measurement\n",
						    i, slot->gsp_restarts);

						/* Measured: once the sensor abandons GSP it
						 * ignores every further command for that
						 * session - notifs freeze while HRS keeps
						 * notifying on the same link. Re-subscribing
						 * cannot recover it; a fresh connection can.
						 * Drop the link and let the existing scan and
						 * reconnect logic bring it back. */
						bt_conn_disconnect(slot->conn,
							BT_HCI_ERR_REMOTE_USER_TERM_CONN);
					}
				}
			}
		}

		log("[ECG] slot %d: state=%s notifs=%u all=%u hr=%u "
		    "frames=%u(sent %u, unknown %u) samples=%u dropped=%u "
		    "conflict=%u ref=%u/%u last=%lums ago maxgap=%ums restarts=%u%s\n",
		    i, gsp_step_name(gsp_step[i]), slot->gsp_notifs,
		    slot->all_notifs, slot->hr_notifs,
		    slot->gsp_frames_done, slot->ecg_frames,
		    slot->gsp_frames_unknown, slot->ecg_samples,
		    slot->ecg_dropped, slot->gsp_conflict, slot->gsp_ref_ecg,
		    slot->gsp_ref_hr, (unsigned long)since,
		    slot->ecg_gap_ms_max, slot->gsp_restarts,
		    stalled ? "  <-- STALLED" : "");
	}

	k_work_reschedule(&ecg_health_work, K_MSEC(ECG_HEALTH_INTERVAL_MS));
}

void ecg_stream_init(void)
{
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		k_work_init_delayable(&gsp_retry_work[i], gsp_retry_handler);
		gsp_retry_count[i] = 0;
		gsp_reclaim_count[i] = 0;
		gsp_reclaim_pending[i] = 0;
	}

	k_work_init_delayable(&ecg_health_work, ecg_health_handler);
	k_work_reschedule(&ecg_health_work, K_MSEC(ECG_HEALTH_INTERVAL_MS));
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
		gsp_retry_count[idx] = 0;
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
	gsp_retry_count[idx] = 0;
	gsp_reclaim_count[idx] = 0;
	gsp_reclaim_pending[idx] = 0;
	gsp_busy[idx] = 0;
	gsp_gave_up[idx] = false;
	slot->ecg_clock_valid = 0;
	slot->ecg_t0_ms = 0;
	slot->ecg_acc = 0;
	slot->gsp_ecg_started = 0;
	slot->gsp_pending = false;
	slot->gsp_len = 0;
	slot->gsp_conflict = 0;
	slot->gsp_unsub_status = 0;
	/* Health counters are per connection: keeping them would make the periodic
	 * line report a rate that spans a reconnect and hide a fresh stall. */
	slot->ecg_frames = 0;
	slot->ecg_samples = 0;
	slot->ecg_gap_ms_max = 0;
	slot->ecg_last_frame_ms = 0;
	slot->gsp_notifs = 0;
	slot->gsp_frames_done = 0;
	slot->gsp_frames_unknown = 0;
	slot->all_notifs = 0;
	slot->hr_notifs = 0;
	slot->gsp_restarts = 0;
	slot->ecg_start_ms = 0;
	/* Forget the references too. A slot is reused for the next device, and a
	 * stale reference here silently blackholes every ECG frame the new stream
	 * sends, because gsp_parse_frame() matches on the reference value. Keep
	 * them in the _prev fields so the next connection can unsubscribe them
	 * before subscribing again - but only when there is something to keep,
	 * since this runs both on disconnect and when a slot is reallocated. */
	if (slot->gsp_ref_ecg) {
		slot->gsp_ref_ecg_prev = slot->gsp_ref_ecg;
	}
	if (slot->gsp_ref_hr) {
		slot->gsp_ref_hr_prev = slot->gsp_ref_hr;
	}
	slot->gsp_ref_ecg = 0;
	slot->gsp_ref_hr = 0;
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

	/* Advance the sample clock by exactly count*1000/fs_hz, carrying the
	 * remainder instead of truncating it. 15 samples at 256 Hz is 58.59375 ms;
	 * rounding that to 58 ms on every packet loses ~10 ms per second, so the
	 * trace clock walked away from k_uptime_get_32() by over a minute an hour
	 * and could not be aligned with the R-R intervals (which are stamped from
	 * true uptime). Keeping the remainder makes the two clocks agree to the
	 * millisecond indefinitely. */
	uint32_t span_num = (uint32_t)count * 1000u;
	uint32_t fs = (uint32_t)fs_hz;
	uint32_t span_ms = span_num / fs;
	uint16_t acc_next = (uint16_t)(slot->ecg_acc + (span_num % fs));

	if (acc_next >= fs) {
		span_ms += acc_next / fs;
		acc_next = (uint16_t)(acc_next % fs);
	}

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
		slot->ecg_acc = acc_next;

		/* Stream health. A gap between records is the only local evidence
		 * that the sensor stopped sending or the decode stopped matching,
		 * so the largest one seen is kept and reported periodically. */
		if (slot->ecg_last_frame_ms) {
			uint32_t gap = now - slot->ecg_last_frame_ms;

			if (gap > slot->ecg_gap_ms_max) {
				slot->ecg_gap_ms_max = gap;
			}
		}
		slot->ecg_last_frame_ms = now;
		slot->ecg_frames++;
		slot->ecg_samples += (uint32_t)count;
	} else {
		/* Commit only on a full write: the remainder belongs to a span that
		 * was never applied, so keeping it would push the clock ahead by a
		 * fraction of a millisecond per drop. */
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

	if (len < 4) {
		log("[GSP] Frame too short (%u bytes)\n", len);
		return BT_GATT_ITER_CONTINUE;
	}

	/* Layout after notification_handler.c strips [0x02][ref]:
	 *   [uint32 timestamp ms][int32 samples...]
	 * so the payload header is 4 bytes, not 6. This was established from a
	 * captured frame: bytes 0-3 read as 10840 ms (plausible for a stream that
	 * has just started), while bytes 2-5 read as 38,207,488 ms (10.6 hours).
	 * Reading samples at offset 6 therefore dropped the real first sample and
	 * decoded the rest one 32-bit word out of step - which still produced
	 * plausible-looking microvolts because the data is densely packed, and is
	 * why the error survived so long. A 68-byte frame divides exactly by 4
	 * only at offset 4, confirming it. */
	uint32_t ts = sys_get_le32(&buf[0]);

	/* Adopt a live stream whose reference we do not hold.
	 *
	 * Movesense keeps a measurement running after a subscriber disappears, and
	 * rejects a new SUBSCRIBE for the same resource with HTTP 409 Conflict
	 * while that old stream is still active. The reference is chosen by the
	 * sensor, so after a reconnect we cannot know it - but the frames arrive
	 * anyway, carrying it. Taking the reference back off the wire is the only
	 * way out of that state; otherwise ECG stays dead for the whole
	 * connection even though the sensor is streaming.
	 *
	 * Only adopt when we hold no ECG reference: a valid one takes precedence,
	 * so a stray frame can never displace a working stream. */
	if (slot->gsp_ref_ecg == 0 && ref != 0 && ref != slot->gsp_ref_hr &&
	    len > 6) {
		int adopted_idx = slot_index(slot);

		slot->gsp_ref_ecg = ref;
		log("[GSP] Adopted live stream ref %u as ECG (%u bytes, %u samples)\n",
		    ref, len, (unsigned)((len - 4) / 4));

		/* Now that the reference is known it can be released properly. Only
		 * do this when the subscription was actually refused: otherwise this
		 * is an ordinary stream whose ref we allocated, and unsubscribing it
		 * would tear down a working subscription. */
		if (adopted_idx >= 0 && slot->gsp_conflict &&
		    gsp_reclaim_count[adopted_idx] < GSP_RECLAIM_MAX) {
			log("[GSP] Ref %u arrived for the refused subscribe; "
			    "scheduling release + resubscribe\n",
			    ref);
			gsp_reclaim_pending[adopted_idx] = 1;
			k_work_reschedule(&gsp_retry_work[adopted_idx],
					  K_MSEC(GSP_RECLAIM_DELAY_MS));
		}
	}

	if (ref == slot->gsp_ref_ecg) {
		/* Samples are plain int32 counts, not Q16.16. A captured frame read
		 * as int32 at offset 4 gives 583, 584, 592 - consecutive samples in
		 * an ECG trace. Read as Q16.16 at offset 6 it gives 584.00, 592.00:
		 * the same numbers, one sample late, because the low 16 bits of the
		 * timestamp happen to be zero. That is why the old offset produced
		 * a plausible-looking trace while silently dropping the first
		 * sample of every frame. */
		int count = (int)((len - 4) / 4);
		int32_t uv[ECG_MAX_SAMPLES];

		if (count <= 0) {
			return BT_GATT_ITER_CONTINUE;
		}
		if (count > ECG_MAX_SAMPLES) {
			count = ECG_MAX_SAMPLES;
		}

		for (int i = 0; i < count; i++) {
			int32_t raw = (int32_t)sys_get_le32(&buf[4 + i * 4]);

			/* Convert counts to microvolts at 0.38147 uV per count.
			 * Multiply before dividing so the sub-uV remainder survives,
			 * and divide (not shift) so negative samples are not biased
			 * toward negative infinity by one LSB. */
			int64_t scaled = (int64_t)raw * 38147;

			uv[i] = (int32_t)(scaled / 100000);
		}

		emit_ecg(slot, uv, count, GSP_SAMPLE_RATE_HZ);
	} else if (ref == slot->gsp_ref_hr) {
		/* Layout is the documented WB_RES::HRData type, not a hand-rolled
		 * header. From MovesenseCoreLib/resources/movesense-api/types.yaml:
		 *
		 *   HRData:
		 *     average : float   - filtered/averaged heart rate, bpm, 0.0=invalid
		 *     rrData  : uint16[] - R-R intervals in ms, 240..4000, one per beat
		 *
		 * SBEM serializes them in declaration order, so after the [0x02][ref]
		 * prefix that notification_handler.c already stripped:
		 *
		 *   offset 0 : float32  average bpm
		 *   offset 4 : uint16[] rrData in ms
		 *
		 * The same yaml notes "single rr-interval value is returned after each
		 * heart beat", which is why a frame is normally 6 bytes: one float and
		 * one interval.
		 *
		 * This branch previously read the intervals at offset 6, i.e. two bytes
		 * past the end of a 6-byte frame. That made count = (6-6)/2 = 0 and the
		 * early return below fired on every frame, so GSP HR silently produced
		 * no output at all - while looking like it had handled the frame.
		 *
		 * Capacities: minItems 1, maxItems 60 per the schema, so up to 60
		 * intervals can arrive in one notification.
		 */
		int count = (int)((len - 4) / 2);

		if (count <= 0 || count > 60) {
			return BT_GATT_ITER_CONTINUE;
		}

		/* Range check straight from the schema (240..4000 ms, i.e. 15..250
		 * bpm). Reading intervals out of step yields timestamp/float
		 * fragments, which land outside this window, so this catches a
		 * future layout drift rather than forwarding nonsense. */
		int plausible = 0;

		for (int i = 0; i < count; i++) {
			uint16_t rr = sys_get_le16(&buf[4 + i * 2]);

			if (rr >= 240 && rr <= 4000) {
				plausible++;
			}
		}

		static uint32_t hr_implausible_logged;

		if (plausible * 2 < count) {
			if (hr_implausible_logged < 3) {
				hr_implausible_logged++;
				log("[GSP] HR frame %u bytes, only %d/%d intervals in "
				    "240-4000 ms at offset 4; layout may differ. First "
				    "bytes: %02x %02x %02x %02x %02x %02x\n",
				    len, plausible, count, buf[0], buf[1], buf[2],
				    buf[3], len > 4 ? buf[4] : 0, len > 5 ? buf[5] : 0);
			}
			return BT_GATT_ITER_CONTINUE;
		}

		/* average is a float, and 0.0 means "no valid heart rate yet". The
		 * server reads a "bpm" field, so forward it; sending only rr left the
		 * dashboard with no heart rate at all. */
		uint32_t avg_bits = sys_get_le32(&buf[0]);
		float avg_bpm;

		memcpy(&avg_bpm, &avg_bits, sizeof(avg_bpm));
		int bpm_x10 = (int)(avg_bpm * 10.0f + 0.5f);

		if (bpm_x10 < 0 || avg_bpm > 300.0f) {
			bpm_x10 = 0;
		}

		json_begin();
		if (bpm_x10 > 0) {
			json_out("{\"type\":\"hr\",\"ts\":%u,\"rssi\":%d,"
				 "\"bpm\":%d.%d,\"rr\":[",
				 k_uptime_get_32(), slot->rssi,
				 bpm_x10 / 10, bpm_x10 % 10);
		} else {
			json_out("{\"type\":\"hr\",\"ts\":%u,\"rssi\":%d,\"rr\":[",
				 k_uptime_get_32(), slot->rssi);
		}
		for (int i = 0; i < count; i++) {
			json_out("%s%u", i ? "," : "", sys_get_le16(&buf[4 + i * 2]));
		}
		json_out("]}\n");
		json_end();
	} else {
		/* Rate-limit this: an unrecognised reference arrives once per frame,
		 * which at 256 Hz floods the console and starves the ECG records
		 * themselves. Log the first occurrence of each reference, dump the
		 * first frame's bytes once so the payload can be identified from a
		 * capture, and then stay quiet until the reference changes. */
		static uint8_t last_unknown_ref;
		static uint32_t unknown_ref_count;

		unknown_ref_count++;
		slot->gsp_frames_unknown++;
		if (ref != last_unknown_ref) {
			log("[GSP] Frame for unknown ref %u (%u bytes), %u so far\n",
			    ref, len, (unsigned)unknown_ref_count);
			/* Build the hex dump in one buffer and log it once. Logging
			 * each byte separately prefixes every byte with log()'s own
			 * timestamp, which makes the dump unreadable and has already
			 * led to a wrong conclusion about the frame layout. */
			if (len <= 72) {
				char hex[3 * 72 + 1];
				uint16_t n = 0;

				for (uint16_t i = 0; i < len && n + 3 < sizeof(hex); i++) {
					n += snprintf(&hex[n], sizeof(hex) - n, "%02x",
						      buf[i]);
				}
				log("[GSP] First frame: %s\n", hex);
			}
			last_unknown_ref = ref;
		}
	}

	(void)ts;
	return BT_GATT_ITER_CONTINUE;
}
