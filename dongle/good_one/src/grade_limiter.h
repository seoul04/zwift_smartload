#ifndef GRADE_LIMITER_H
#define GRADE_LIMITER_H

#include <stdint.h>
#include <stdbool.h>

/* Speed range: 1000-3000 (0.01 km/h units = 10-30 km/h), 50 buckets = 40 units per bucket */
#define SPEED_MIN 1000
#define SPEED_MAX 3000
#define NUM_BUCKETS 50
#define BUCKET_WIDTH 40

/* Grade is in 0.01% units (e.g., 500 = 5.00%) */
#define MAX_GRADE_INITIAL 2000  /* 20% initial safe limit */

/**
 * Initialize the grade limiter module
 * Loads persisted limits from NVS if available
 */
void grade_limiter_init(void);

/**
 * Apply grade limiting based on current speed
 * @param speed_mh Current speed in decimeters per hour
 * @param requested_grade Requested grade from Zwift (0.01% units)
 * @param applied_grade Output: limited grade to apply (0.01% units)
 * @return true if grade was limited, false if unchanged
 */
bool grade_limiter_apply(uint16_t speed_mh, int16_t requested_grade, int16_t *applied_grade);

/**
 * Learn from a thermal release event
 * Reduces the safe limit for the current speed bucket
 * @param speed_mh Speed at which thermal release occurred
 * @param grade_at_release Grade that caused the release
 */
void grade_limiter_learn(uint16_t speed_mh, int16_t grade_at_release);

/**
 * Periodic decay function - call once per hour
 * Increases all limits by 1/10% to adapt to changing conditions
 */
void grade_limiter_decay(void);

/**
 * Save current limits to NVS
 */
void grade_limiter_save(void);

/**
 * Print the grade limiter table in JSON format
 */
void grade_limiter_print_table(void);

/**
 * Update active time tracking for decay
 * Call this periodically with trainer connection or power status
 * @param is_active true if trainer connected or power > 50W
 */
void grade_limiter_update_active_time(bool is_active);

#endif /* GRADE_LIMITER_H */
