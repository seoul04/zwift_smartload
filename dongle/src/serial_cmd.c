/* serial_cmd.c - Commands received from z_relay_server over the USB serial console */

#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/drivers/uart.h>
#include <string.h>
#include "common.h"
#include "ftms_control_point.h"
#include "serial_cmd.h"

#define SERIAL_CMD_LINE_MAX 128
#define SERIAL_CMD_QUEUE_LEN 4
#define SERIAL_CMD_DATA_MAX 32

/* Fallback drain period. The driver's RX callback is the fast path; this only
 * catches bytes it left behind. A command therefore arrives within ~this long
 * even if the callback never fires. */
#define SERIAL_CMD_POLL_INTERVAL K_MSEC(20)

static const struct device *const console_dev = DEVICE_DT_GET(DT_CHOSEN(zephyr_console));

K_MSGQ_DEFINE(serial_cmd_msgq, SERIAL_CMD_LINE_MAX, SERIAL_CMD_QUEUE_LEN, 4);

/* Only touched by serial_cmd_drain(), which runs on the system work queue */
static char rx_line[SERIAL_CMD_LINE_MAX];
static size_t rx_len;
static bool rx_overflow;

static struct k_work serial_cmd_work;
static void serial_cmd_poll_handler(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(serial_cmd_poll_work, serial_cmd_poll_handler);

/* Copy the string value of "key" into out; returns its length or -1 */
static int json_get_string(const char *line, const char *key, char *out, size_t out_size)
{
	char pattern[24];

	snprintf(pattern, sizeof(pattern), "\"%s\"", key);
	const char *p = strstr(line, pattern);

	if (!p) {
		return -1;
	}
	p += strlen(pattern);
	while (*p == ' ') {
		p++;
	}
	if (*p++ != ':') {
		return -1;
	}
	while (*p == ' ') {
		p++;
	}
	if (*p++ != '"') {
		return -1;
	}

	size_t n = 0;

	while (*p && *p != '"') {
		if (n + 1 >= out_size) {
			return -1;
		}
		out[n++] = *p++;
	}
	if (*p != '"') {
		return -1;
	}
	out[n] = '\0';
	return n;
}

static int hex_nibble(char c)
{
	if (c >= '0' && c <= '9') {
		return c - '0';
	}
	if (c >= 'a' && c <= 'f') {
		return c - 'a' + 10;
	}
	if (c >= 'A' && c <= 'F') {
		return c - 'A' + 10;
	}
	return -1;
}

static int hex_decode(const char *hex, uint8_t *out, size_t out_size)
{
	size_t len = strlen(hex);

	if (len == 0 || len % 2 || len / 2 > out_size) {
		return -1;
	}
	for (size_t i = 0; i < len / 2; i++) {
		int hi = hex_nibble(hex[2 * i]);
		int lo = hex_nibble(hex[2 * i + 1]);

		if (hi < 0 || lo < 0) {
			return -1;
		}
		out[i] = (hi << 4) | lo;
	}
	return len / 2;
}

static void serial_cmd_process(const char *line)
{
	char cmd[16];
	char hex[2 * SERIAL_CMD_DATA_MAX + 1];
	uint8_t data[SERIAL_CMD_DATA_MAX];

	if (json_get_string(line, "cmd", cmd, sizeof(cmd)) < 0) {
		log("[SERIAL] Ignoring line without cmd: %s\n", line);
		return;
	}

	if (strcmp(cmd, "ftms_cp") == 0) {
		int len = -1;

		if (json_get_string(line, "data", hex, sizeof(hex)) > 0) {
			len = hex_decode(hex, data, sizeof(data));
		}
		if (len <= 0) {
			log("[SERIAL] Invalid ftms_cp data: %s\n", line);
			return;
		}
		ftms_cp_handle_command(data, len);
	} else if (strcmp(cmd, "info") == 0) {
		/* Repeat the boot-time identity line. z_relay_server asks for it so it
		 * can advertise under the dongle's own name and Bluetooth address.
		 * Log first: at 830 B/s of ECG records the reply is otherwise
		 * indistinguishable from the boot-time announcement. */
		log("[SERIAL] info requested by server\n");
		announce_identity();
	} else if (strcmp(cmd, "gsp_reset") == 0) {
		/* Diagnostic: drop this side's subscription references without
		 * touching the sensor, which reproduces the state that produces a
		 * 409 - the sensor keeps streaming but the dongle no longer knows
		 * the reference. It is the only way to exercise the reclaim path
		 * without pulling the power or walking out of radio range. */
		bool any = false;

		for (int i = 0; i < MAX_CONNECTIONS; i++) {
			if (!connections[i].conn) {
				continue;
			}

			log("[SERIAL] gsp_reset slot %d: forgetting refs "
			    "ecg=%u hr=%u (sensor keeps streaming)\n", i,
			    connections[i].gsp_ref_ecg,
			    connections[i].gsp_ref_hr);

			/* Do NOT move them to _prev: that would make the next
			 * connection try to unsubscribe a reference that is about to
			 * be reclaimed anyway, and the point here is to force the 409
			 * discovery path. */
			connections[i].gsp_ref_ecg = 0;
			connections[i].gsp_ref_hr = 0;
			connections[i].gsp_conflict = 0;
			connections[i].gsp_ecg_started = 0;
			any = true;
		}

		if (!any) {
			log("[SERIAL] gsp_reset: nothing connected\n");
		}
	} else {
		log("[SERIAL] Unknown command: %s\n", cmd);
	}
}

/* Runs on the system work queue: Bluetooth calls and printf are safe here */
static void serial_cmd_work_handler(struct k_work *work)
{
	char line[SERIAL_CMD_LINE_MAX];

	while (k_msgq_get(&serial_cmd_msgq, line, K_NO_WAIT) == 0) {
		serial_cmd_process(line);
	}
}

/* Pull whatever the driver has buffered and split it into lines. Safe to call
 * from either the UART callback or the poller: both run as work items, and the
 * driver serialises access to its RX ring buffer. Never print from here. */
static void serial_cmd_drain(const struct device *dev)
{
	uint8_t buf[32];

	while (uart_irq_update(dev) > 0 && uart_irq_rx_ready(dev) > 0) {
		int n = uart_fifo_read(dev, buf, sizeof(buf));

		if (n <= 0) {
			break;
		}
		for (int i = 0; i < n; i++) {
			char c = buf[i];

			if (c == '\n' || c == '\r') {
				if (rx_len > 0 && !rx_overflow) {
					rx_line[rx_len] = '\0';
					if (k_msgq_put(&serial_cmd_msgq, rx_line, K_NO_WAIT) == 0) {
						k_work_submit(&serial_cmd_work);
					}
				}
				rx_len = 0;
				rx_overflow = false;
			} else if (rx_len < sizeof(rx_line) - 1) {
				rx_line[rx_len++] = c;
			} else {
				rx_overflow = true;
			}
		}
	}
}

/* Fast path. The driver invokes this only after it has re-armed the OUT
 * endpoint, and it may leave bytes in its RX ring buffer if it could not hand
 * everything to us at once. serial_cmd_poll_work() is the safety net, so a
 * command still arrives even if this callback never fires. */
static void serial_cmd_uart_cb(const struct device *dev, void *user_data)
{
	ARG_UNUSED(user_data);
	serial_cmd_drain(dev);
}

static void serial_cmd_poll_handler(struct k_work *work)
{
	ARG_UNUSED(work);

	serial_cmd_drain(console_dev);
	k_work_reschedule(&serial_cmd_poll_work, SERIAL_CMD_POLL_INTERVAL);
}

int serial_cmd_init(void)
{
	if (!device_is_ready(console_dev)) {
		log("[SERIAL] Console device not ready\n");
		return -ENODEV;
	}

	k_work_init(&serial_cmd_work, serial_cmd_work_handler);

	int err = uart_irq_callback_user_data_set(console_dev, serial_cmd_uart_cb, NULL);

	if (err) {
		log("[SERIAL] Cannot set UART callback (err %d)\n", err);
		return err;
	}
	uart_irq_rx_enable(console_dev);

	/* Also drain on a timer: see the comment on serial_cmd_uart_cb(). */
	k_work_reschedule(&serial_cmd_poll_work, SERIAL_CMD_POLL_INTERVAL);

	log("[SERIAL] Command input ready\n");
	return 0;
}
