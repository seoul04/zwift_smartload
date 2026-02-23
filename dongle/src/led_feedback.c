/* led_feedback.c - LED feedback for user status indication */

#include <zephyr/kernel.h>
#include <zephyr/drivers/gpio.h>
#include "common.h"
#include "led_feedback.h"
#include "device_manager.h"

/* LED configuration - use led0 alias from devicetree */
#define LED0_NODE DT_ALIAS(led0)

#if DT_NODE_HAS_STATUS(LED0_NODE, okay)
static const struct gpio_dt_spec led = GPIO_DT_SPEC_GET(LED0_NODE, gpios);
static bool led_available = true;
#else
/* Fallback if no LED defined in devicetree */
static bool led_available = false;
static struct gpio_dt_spec led;
#endif

/* LED pattern state machine */
enum led_mode {
	LED_MODE_IDLE,           /* No devices connected, slow single blink */
	LED_MODE_PAIRING,        /* Scan window active, slow steady blink */
	LED_MODE_CONNECTED,      /* N devices connected, N blinks then pause */
};

static enum led_mode current_mode = LED_MODE_IDLE;
static int blink_count = 0;           /* Current blink within sequence */
static int target_blinks = 0;         /* Number of blinks for connected devices */
static bool led_on = false;           /* Current LED state */

/* Timer for LED pattern */
static struct k_work_delayable led_work;

/* Timing constants (in milliseconds) */
#define PAIRING_BLINK_ON_MS      500    /* Slow blink during pairing */
#define PAIRING_BLINK_OFF_MS     500

#define CONNECTED_BLINK_ON_MS    100    /* Quick pulse for connection count */
#define CONNECTED_BLINK_OFF_MS   200
#define CONNECTED_PAUSE_MS       1200   /* Pause between blink groups */

#define IDLE_BLINK_ON_MS         150    /* Brief pulse when idle */
#define IDLE_BLINK_OFF_MS        2500   /* Long off period when idle */

static void set_led(bool on)
{
	if (!led_available) {
		return;
	}
	led_on = on;
	/* LEDs are often active-low, but GPIO_DT_SPEC handles this via flags */
	gpio_pin_set_dt(&led, on ? 1 : 0);
}

int led_feedback_get_connection_count(void)
{
	int count = 0;
	for (int i = 0; i < MAX_CONNECTIONS; i++) {
		if (connections[i].conn != NULL) {
			count++;
		}
	}
	return count;
}

static void led_work_handler(struct k_work *work)
{
	uint32_t next_delay_ms = 0;
	
	/* Check if we should be in pairing mode */
	if (is_scan_window_active()) {
		current_mode = LED_MODE_PAIRING;
	} else {
		int conn_count = led_feedback_get_connection_count();
		if (conn_count == 0) {
			current_mode = LED_MODE_IDLE;
		} else {
			current_mode = LED_MODE_CONNECTED;
			target_blinks = conn_count;
		}
	}
	
	switch (current_mode) {
	case LED_MODE_PAIRING:
		/* Simple toggle for pairing mode */
		if (led_on) {
			set_led(false);
			next_delay_ms = PAIRING_BLINK_OFF_MS;
		} else {
			set_led(true);
			next_delay_ms = PAIRING_BLINK_ON_MS;
		}
		break;
		
	case LED_MODE_IDLE:
		/* Slow single pulse when no devices connected */
		if (led_on) {
			set_led(false);
			next_delay_ms = IDLE_BLINK_OFF_MS;
		} else {
			set_led(true);
			next_delay_ms = IDLE_BLINK_ON_MS;
		}
		break;
		
	case LED_MODE_CONNECTED:
		/* Blink N times for N connected devices, then pause */
		if (led_on) {
			/* LED was on, turn it off */
			set_led(false);
			blink_count++;
			
			if (blink_count >= target_blinks) {
				/* Completed all blinks, long pause */
				blink_count = 0;
				next_delay_ms = CONNECTED_PAUSE_MS;
			} else {
				/* Short pause between blinks */
				next_delay_ms = CONNECTED_BLINK_OFF_MS;
			}
		} else {
			/* LED was off, turn it on */
			set_led(true);
			next_delay_ms = CONNECTED_BLINK_ON_MS;
		}
		break;
	}
	
	/* Schedule next LED update */
	k_work_reschedule(&led_work, K_MSEC(next_delay_ms));
}

void led_feedback_update(void)
{
	if (!led_available) {
		return;
	}
	
	/* Reset pattern state when mode changes */
	blink_count = 0;
	led_on = false;
	set_led(false);
	
	/* Reschedule immediately to pick up new state */
	k_work_reschedule(&led_work, K_NO_WAIT);
}

int led_feedback_init(void)
{
	if (!led_available) {
		log("LED feedback: No LED available in devicetree\n");
		return -ENODEV;
	}
	
	if (!gpio_is_ready_dt(&led)) {
		log("LED feedback: LED device not ready\n");
		led_available = false;
		return -ENODEV;
	}
	
	int err = gpio_pin_configure_dt(&led, GPIO_OUTPUT_INACTIVE);
	if (err < 0) {
		log("LED feedback: Failed to configure LED (err %d)\n", err);
		led_available = false;
		return err;
	}
	
	/* Initialize work item */
	k_work_init_delayable(&led_work, led_work_handler);
	
	log("LED feedback initialized\n");
	
	/* Start LED pattern */
	led_feedback_update();
	
	return 0;
}
