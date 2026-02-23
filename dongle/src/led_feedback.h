/* led_feedback.h - LED feedback for user status indication */

#ifndef LED_FEEDBACK_H_
#define LED_FEEDBACK_H_

/**
 * Initialize LED feedback module.
 * Must be called after kernel and GPIO are ready.
 * 
 * @return 0 on success, negative errno on failure
 */
int led_feedback_init(void);

/**
 * Update LED feedback state.
 * Call this when connection count or scan window state changes.
 */
void led_feedback_update(void);

/**
 * Get current connected device count (for LED feedback).
 * Counts non-NULL connections in the connection slot array.
 * 
 * @return number of connected devices (0 to MAX_CONNECTIONS)
 */
int led_feedback_get_connection_count(void);

#endif /* LED_FEEDBACK_H_ */
