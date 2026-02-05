#include "grade_limiter.h"
#include "common.h"
#include <zephyr/kernel.h>
#include <zephyr/settings/settings.h>
#include <zephyr/fs/nvs.h>
#include <zephyr/logging/log_ctrl.h>
#include <string.h>

/* NVS storage */
#define GRADE_LIMITS_NVS_ID 100  /* Base ID for grade limit storage */
static struct nvs_fs *nvs_handle;
extern struct nvs_fs nvs;  /* Defined in nvs_storage.c */

/* Lookup table: max safe grade for each speed bucket (in 0.01% units) */
static int16_t max_safe_grade[NUM_BUCKETS];

/* Current grade being applied (for learning when thermal release occurs) */
static int16_t current_applied_grade = 0;
static uint16_t current_speed = 0;

/* Active time tracking for decay (in seconds) */
static uint32_t active_time_seconds = 0;
static uint32_t last_active_check_time = 0;
static uint32_t last_decay_at_seconds = 0;
#define DECAY_INTERVAL_SECONDS (3600)  /* 1 hour of active use */
#define ACTIVE_CHECK_INTERVAL_MS (1000)  /* Check every second */

/**
 * Convert speed to bucket index
 * Returns -1 if speed is outside the restricted range
 */
static int speed_to_bucket(uint16_t speed_mh)
{
	if (speed_mh < SPEED_MIN || speed_mh >= SPEED_MAX) {
		return -1;  /* Outside restricted range */
	}
	
	int bucket = (speed_mh - SPEED_MIN) / BUCKET_WIDTH;
	if (bucket >= NUM_BUCKETS) {
		bucket = NUM_BUCKETS - 1;
	}
	
	return bucket;
}

/**
 * Load grade limits from NVS
 */
static void load_limits_from_nvs(void)
{
	int ret;
	
	/* Try to load the entire array from NVS */
	ret = nvs_read(&nvs, GRADE_LIMITS_NVS_ID, max_safe_grade, sizeof(max_safe_grade));
	
	if (ret == sizeof(max_safe_grade)) {
		log("Loaded grade limits from NVS");
	} else {
		/* Initialize with default values */
		log("Initializing grade limits to defaults");
		for (int i = 0; i < NUM_BUCKETS; i++) {
			max_safe_grade[i] = MAX_GRADE_INITIAL;
		}
	}
}

void grade_limiter_init(void)
{
	nvs_handle = &nvs;
	active_time_seconds = 0;
	last_decay_at_seconds = 0;
	last_active_check_time = k_uptime_get_32();
	
	/* Load persisted limits or initialize to defaults */
	load_limits_from_nvs();
	
	log("Grade limiter initialized: %d buckets, %d-%d m/h range", 
	    NUM_BUCKETS, SPEED_MIN, SPEED_MAX);
}

bool grade_limiter_apply(uint16_t speed_mh, int16_t requested_grade, int16_t *applied_grade)
{
	int bucket;
	bool limited = false;
	
	/* Store current state for learning */
	current_speed = speed_mh;
	
	/* Check if speed is in restricted range */
	bucket = speed_to_bucket(speed_mh);
	if (bucket < 0) {
		/* No restriction - rider safety (too slow) or sufficient cooling (too fast) */
		*applied_grade = requested_grade;
		current_applied_grade = requested_grade;
		return false;
	}
	
	/* Apply limit if necessary */
	if (requested_grade > max_safe_grade[bucket]) {
		*applied_grade = max_safe_grade[bucket];
		limited = true;
	} else {
		*applied_grade = requested_grade;
	}
	
	current_applied_grade = *applied_grade;
	return limited;
}

void grade_limiter_learn(uint16_t speed_mh, int16_t grade_at_release)
{
	int bucket = speed_to_bucket(speed_mh);
	
	if (bucket < 0) {
		return;  /* Outside restricted range, nothing to learn */
	}
	
	/* Reduce the limit by 10% below the grade that caused release */
	int16_t new_limit = (grade_at_release * 90) / 100;
	
	/* Don't let it drop below 1% (100 in 0.01% units) */
	if (new_limit < 100) {
		new_limit = 100;
	}
	
	/* Only update if it's stricter than current limit */
	if (new_limit < max_safe_grade[bucket]) {
		max_safe_grade[bucket] = new_limit;
		log("Learned: bucket %d (speed ~%d m/h) limit reduced to %d.%02d%%",
		    bucket, SPEED_MIN + bucket * BUCKET_WIDTH,
		    new_limit / 100, new_limit % 100);
		
		/* Save to NVS */
		grade_limiter_save();
	}
}

void grade_limiter_decay(void)
{
	/* Check if an hour of active use has passed */
	if ((active_time_seconds - last_decay_at_seconds) < DECAY_INTERVAL_SECONDS) {
		return;
	}
	
	last_decay_at_seconds = active_time_seconds;
	
	/* Increase all limits by 1/10% (10 units in 0.01% scale) */
	bool changed = false;
	for (int i = 0; i < NUM_BUCKETS; i++) {
		if (max_safe_grade[i] < MAX_GRADE_INITIAL) {
			max_safe_grade[i] += 10;
			if (max_safe_grade[i] > MAX_GRADE_INITIAL) {
				max_safe_grade[i] = MAX_GRADE_INITIAL;
			}
			changed = true;
		}
	}
	
	if (changed) {
		log("Grade limits decayed (+0.10%%) at %u hours active", active_time_seconds / 3600);
		grade_limiter_save();
	}
}

void grade_limiter_save(void)
{
	int ret = nvs_write(&nvs, GRADE_LIMITS_NVS_ID, max_safe_grade, sizeof(max_safe_grade));
	
	if (ret < 0) {
		log("Failed to save grade limits to NVS: %d", ret);
	}
}

void grade_limiter_print_table(void)
{
	uint32_t now = k_uptime_get_32();

	json_out("{\"type\":\"grade_table\",\"ts\":%u,\"active_hours\":%u,\"buckets\":[",
	       now, active_time_seconds / 3600);
	for (int i = 0; i < NUM_BUCKETS; i++) {
		uint16_t speed_start = SPEED_MIN + (i * BUCKET_WIDTH);
		uint16_t speed_end = speed_start + BUCKET_WIDTH - 1;
		json_out("{\"start\":%u,\"end\":%u,\"max_grade\":%d}%s",
		       speed_start, speed_end, max_safe_grade[i],
		       (i < NUM_BUCKETS - 1) ? "," : "");
	}
	json_out("]}\n");
}

void grade_limiter_update_active_time(bool is_active)
{
	uint32_t now = k_uptime_get_32();
	uint32_t elapsed_ms = now - last_active_check_time;
	
	/* Only update if at least 1 second has passed */
	if (elapsed_ms >= ACTIVE_CHECK_INTERVAL_MS) {
		if (is_active) {
			/* Increment active time by elapsed seconds */
			active_time_seconds += elapsed_ms / 1000;
			
			/* Check if we should decay */
			grade_limiter_decay();
		}
		last_active_check_time = now;
	}
}
