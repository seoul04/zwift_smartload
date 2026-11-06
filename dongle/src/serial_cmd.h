/* serial_cmd.h - Commands received from z_relay_server over the USB serial console */

#ifndef SERIAL_CMD_H_
#define SERIAL_CMD_H_

/*
 * One JSON object per line, e.g.
 *   {"cmd":"ftms_cp","data":"110000c8ff2833"}
 * "ftms_cp" goes through ftms_cp_handle_command() exactly like a BLE Control Point write;
 * the trainer's answer comes back as {"type":"cp_resp","ts":...,"data":"801101"}.
 */
int serial_cmd_init(void);

#endif /* SERIAL_CMD_H_ */
