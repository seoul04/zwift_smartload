/* gatt_services.h - GATT service definitions */

#ifndef GATT_SERVICES_H_
#define GATT_SERVICES_H_

#include <zephyr/bluetooth/gatt.h>

/* Service definitions */
extern const struct bt_gatt_service_static hr_svc;
extern const struct bt_gatt_service_static csc_svc;
extern const struct bt_gatt_service_static cp_svc;
extern const struct bt_gatt_service_static ftms_svc;

/* Measurement buffers */
extern uint8_t hr_measurement[20];
extern uint16_t hr_measurement_len;

extern uint8_t csc_measurement[11];
extern uint16_t csc_measurement_len;

extern uint8_t cp_measurement[34];
extern uint16_t cp_measurement_len;

extern uint8_t ftms_measurement[64];
extern uint16_t ftms_measurement_len;

extern uint8_t ftms_training_status[20];
extern uint16_t ftms_training_status_len;

extern uint8_t ftms_machine_status[20];
extern uint16_t ftms_machine_status_len;

#endif /* GATT_SERVICES_H_ */
