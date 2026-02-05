/* gatt_services.c - GATT service definitions */

#include <zephyr/bluetooth/bluetooth.h>
#include <zephyr/bluetooth/gatt.h>
#include <zephyr/bluetooth/uuid.h>
#include "common.h"
#include "gatt_services.h"
#include "ftms_control_point.h"

/* Measurement buffers */
uint8_t hr_measurement[20];
uint16_t hr_measurement_len;

uint8_t csc_measurement[11];
uint16_t csc_measurement_len;

uint8_t cp_measurement[34];
uint16_t cp_measurement_len;

uint8_t ftms_measurement[64];
uint16_t ftms_measurement_len;

uint8_t ftms_training_status[20];
uint16_t ftms_training_status_len;

uint8_t ftms_machine_status[20];
uint16_t ftms_machine_status_len;

/* Heart Rate Service */
BT_GATT_SERVICE_DEFINE(hr_svc,
	BT_GATT_PRIMARY_SERVICE(BT_UUID_HRS),
	BT_GATT_CHARACTERISTIC(BT_UUID_HRS_MEASUREMENT,
			       BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_NONE,
			       NULL, NULL, NULL),
	BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

/* Cycling Speed and Cadence Service */
BT_GATT_SERVICE_DEFINE(csc_svc,
	BT_GATT_PRIMARY_SERVICE(BT_UUID_DECLARE_16(0x1816)),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(0x2A5B), /* CSC Measurement */
			       BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_NONE,
			       NULL, NULL, NULL),
	BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

/* Cycling Power Service */
BT_GATT_SERVICE_DEFINE(cp_svc,
	BT_GATT_PRIMARY_SERVICE(BT_UUID_DECLARE_16(0x1818)),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(0x2A63), /* CP Measurement */
			       BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_NONE,
			       NULL, NULL, NULL),
	BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);

/* Fitness Machine Service */
BT_GATT_SERVICE_DEFINE(ftms_svc,
	BT_GATT_PRIMARY_SERVICE(BT_UUID_DECLARE_16(0x1826)),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(0x2AD2), /* Indoor Bike Data */
			       BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_NONE,
			       NULL, NULL, NULL),
	BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(0x2AD3), /* Training Status */
			       BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_NONE,
			       NULL, NULL, NULL),
	BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(0x2ADA), /* Fitness Machine Status */
			       BT_GATT_CHRC_NOTIFY,
			       BT_GATT_PERM_NONE,
			       NULL, NULL, NULL),
	BT_GATT_CCC(NULL, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
	BT_GATT_CHARACTERISTIC(BT_UUID_DECLARE_16(0x2AD9), /* Fitness Machine Control Point */
			       BT_GATT_CHRC_WRITE | BT_GATT_CHRC_INDICATE,
			       BT_GATT_PERM_WRITE,
			       NULL, ftms_control_point_write, NULL),
	BT_GATT_CCC(ftms_cp_ccc_cfg_changed, BT_GATT_PERM_READ | BT_GATT_PERM_WRITE),
);
