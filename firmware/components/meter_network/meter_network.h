#pragma once
#include "esphome/core/component.h"
#include "esphome/components/wifi/wifi_component.h"
#include "esphome/components/display/display.h"
#include "esphome/core/preferences.h"
#include "model.h"
#include "http.h"
#include "ui.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/queue.h"
#include <string>
#include <atomic>
#include "esp_event.h"
#include "esp_wifi.h"
#include "esp_system.h"

namespace esphome::meter_network {
class MeterNetwork : public Component {
 public:
  void configure(const char *host, uint16_t port, const char *token, uint32_t poll, uint32_t timeout) {
    host_ = host; port_ = port; token_ = token; poll_ms_ = poll; timeout_ms_ = timeout;
  }
  void setup() override;
  void loop() override;
  void dump_config() override;
  const meter::State &state() const { return state_; }
  bool prepare_display();
  void draw(display::Display &display);
  void display_updated(uint32_t duration_ms);
  void set_display(display::Display *value) { display_ = value; }
  void set_touch_calibration(int x_min, int x_max, int y_min, int y_max, bool swap, bool mirror_x, bool mirror_y) {
    calibration_ = {x_min, x_max, y_min, y_max, swap, mirror_x, mirror_y};
  }
  void touch_press(int x_raw, int y_raw);
  void touch_move(int x_raw, int y_raw);
  void touch_release();
 protected:
  struct Result {
    meter::Model model;
    const char *error = "transport";
    bool valid = false;
    uint32_t duration = 0;
    uint32_t stack_free = 0;
  };
  std::string host_, token_;
  uint16_t port_ = 8080;
  uint32_t poll_ms_ = 60000, timeout_ms_ = 10000, backoff_ms_ = 60000;
  uint64_t next_poll_ = 0, next_log_ = 0;
  QueueHandle_t results_ = nullptr;
  TaskHandle_t worker_ = nullptr;
  bool busy_ = false;
  meter::State state_;
  const char *last_error_ = "starting";
  esp_event_handler_instance_t wifi_handler_ = nullptr;
  std::atomic<uint16_t> wifi_reason_{0};
  meter::ui::Frame frame_;
  bool frame_ready_ = false;
  display::Display *display_ = nullptr;
  meter::orientation::Calibration calibration_;
  meter::orientation::Tap title_tap_;
  meter::orientation::Rotation rotation_;
  ESPPreferenceObject orientation_preference_;
  uint8_t frame_position_ = 0;
  bool rotation_pending_ = false;
  uint64_t next_display_log_ = 0;
  int64_t displayed_observation_ = -2;
  char displayed_status_[6]{};
  static void wifi_event(void *argument, esp_event_base_t base, int32_t event, void *data);
  static void worker(void *argument);
  void apply_rotation_();
};
} // namespace esphome::meter_network
