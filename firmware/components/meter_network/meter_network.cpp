#include "meter_network.h"
#include "esphome/core/log.h"
#include "esp_system.h"

namespace esphome::meter_network {
static const char *const TAG = "meter_network";
namespace {
// Stable CMR1 key and one-byte value, deliberately independent of node/build hash.
constexpr uint32_t ORIENTATION_KEY = 0x434D5231;
struct OrientationStore {
  ESPPreferenceObject &preference;
  bool load(uint8_t &value) { return preference.load(&value); }
  bool save(uint8_t value) { return preference.save(&value); }
  bool sync() { return global_preferences && global_preferences->sync(); }
};
}
void MeterNetwork::setup() {
  if (global_preferences) orientation_preference_ = global_preferences->make_preference<uint8_t>(ORIENTATION_KEY, true);
  OrientationStore store{orientation_preference_};
  const bool restored = rotation_.restore(store);
  if (display_) display_->set_rotation(static_cast<display::DisplayRotation>(meter::orientation::degrees(rotation_.position())));
  ESP_LOGI("meter_rotation", "position=%u restored=%d", rotation_.position(), restored);
  backoff_ms_ = poll_ms_;
  results_ = xQueueCreate(1, sizeof(Result));
  if (!results_ || xTaskCreate(worker, "meter_http", 16384, this, 1, &worker_) != pdPASS) {
    if (results_) { vQueueDelete(results_); results_ = nullptr; }
    last_error_ = "resources"; mark_failed();
  }
}
void MeterNetwork::dump_config() {
  ESP_LOGCONFIG(TAG, "Private meter client: poll=%ums timeout=%ums cap=4096 bytes", poll_ms_, timeout_ms_);
}
void MeterNetwork::worker(void *argument) {
  auto *self = static_cast<MeterNetwork *>(argument);
  for (;;) {
    ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    uint64_t start = meter::monotonic_ms();
    Result result;
    {
      meter::Http http;
      if (http.get(self->host_.c_str(), self->port_, self->token_.c_str(), self->timeout_ms_)) {
        result.valid = meter::decode(http.body, http.size, result.model);
        result.error = result.valid ? "none" : "payload";
        if (result.valid && result.model.stale_after * 1000 < 2 * self->poll_ms_) {
          result.valid = false; result.error = "threshold";
        }
      } else result.error = http.error;
    } // Close socket before publishing and sleeping; main loop owns all state/logs.
    result.duration = meter::monotonic_ms() - start;
    result.stack_free = uxTaskGetStackHighWaterMark(nullptr);
    xQueueOverwrite(self->results_, &result);
  }
}
void MeterNetwork::wifi_event(void *argument, esp_event_base_t, int32_t, void *data) {
  auto *self = static_cast<MeterNetwork *>(argument);
  auto *event = static_cast<wifi_event_sta_disconnected_t *>(data);
  self->wifi_reason_.store(event->reason, std::memory_order_relaxed);
}
void MeterNetwork::loop() {
  // Touch callbacks only queue this action. All pages finish before the main
  // loop changes panel geometry and renders the next frozen frame.
  if (rotation_pending_) apply_rotation_();
  if (!results_) return;
  if (!wifi_handler_) {
    // Register after all setup phases, once ESPHome has created the event loop.
    esp_event_handler_instance_register(WIFI_EVENT, WIFI_EVENT_STA_DISCONNECTED,
                                       wifi_event, this, &wifi_handler_);
  }
  uint64_t now = meter::monotonic_ms();
  Result result;
  if (xQueueReceive(results_, &result, 0) == pdTRUE) {
    busy_ = false;
    bool accepted = result.valid && state_.accept(result.model, now);
    if (!accepted) state_.transport_failed = true;
    last_error_ = accepted ? "none" : result.valid ? "ordering" : result.error;
    next_poll_ = now + (accepted ? poll_ms_ : backoff_ms_);
    backoff_ms_ = accepted ? poll_ms_ : std::min<uint32_t>(300000, backoff_ms_ * 2);
    ESP_LOGI(TAG, "poll accepted=%d error=%s duration_ms=%u heap=%u stack_free=%u", accepted,
             last_error_, result.duration, static_cast<unsigned>(esp_get_free_heap_size()), result.stack_free);
  }
  bool connected = wifi::global_wifi_component->is_connected();
  if (!connected) { state_.transport_failed = true; last_error_ = "wifi"; }
  if (connected && !busy_ && now >= next_poll_) {
    busy_ = true; xTaskNotifyGive(worker_);
  }
  if (now >= next_log_) {
    next_log_ = now + 10000;
    ESP_LOGI(TAG, "state=%s error=%s observation=%lld age=%lld remaining=%.2f busy=%d heap=%u uptime=%llu wifi_reason=%u",
      state_.status(now), last_error_, static_cast<long long>(state_.accepted ? state_.model.observed : -1),
      static_cast<long long>(state_.accepted && state_.model.observed >= 0 ? state_.age(now) : -1LL), state_.accepted ? state_.model.remaining : -1,
      busy_, static_cast<unsigned>(esp_get_free_heap_size()), static_cast<unsigned long long>(now / 1000),
      connected ? 0 : wifi_reason_.load(std::memory_order_relaxed));
  }
}
bool MeterNetwork::prepare_display() {
  auto next = meter::ui::project(state_, meter::monotonic_ms(), wifi::global_wifi_component->is_connected());
  if (frame_ready_ && frame_position_ == rotation_.position() && meter::ui::equal(frame_, next)) return false;
  frame_ = next; frame_ready_ = true;
  frame_position_ = rotation_.position();
  return true;
}
void MeterNetwork::draw(display::Display &display) {
  struct Canvas {
    display::Display &target;
    void rect(int x,int y,int w,int h,uint32_t rgb) {
      const Color color(rgb>>16,(rgb>>8)&255,rgb&255);
      if(x==0 && y==0 && w==target.get_width() && h==target.get_height()) target.fill(color);
      else target.filled_rectangle(x,y,w,h,color);
    }
  } canvas{display};
  // ESPHome replays this same frozen frame for all four buffer pages.
  meter::ui::draw(canvas, frame_, frame_position_);
}
void MeterNetwork::touch_press(int x_raw, int y_raw) {
  const auto point = meter::orientation::to_view(calibration_.normalize(x_raw, y_raw), rotation_.position());
  title_tap_.press(point, meter::monotonic_ms());
  ESP_LOGI("meter_rotation", "touch raw=%d,%d view=%d,%d position=%u", x_raw, y_raw, point.x, point.y, rotation_.position());
}
void MeterNetwork::touch_move(int x_raw, int y_raw) {
  title_tap_.move(meter::orientation::to_view(calibration_.normalize(x_raw, y_raw), rotation_.position()));
}
void MeterNetwork::touch_release() {
  if (title_tap_.release(meter::monotonic_ms())) rotation_pending_ = true;
}
void MeterNetwork::apply_rotation_() {
  rotation_pending_ = false;
  OrientationStore store{orientation_preference_};
  const bool saved = rotation_.advance(store);
  if (!saved) ESP_LOGW("meter_rotation", "Orientation save failed; current view works but may not survive power loss");
  if (display_) {
    display_->set_rotation(static_cast<display::DisplayRotation>(meter::orientation::degrees(rotation_.position())));
    next_display_log_ = 0;
    if (prepare_display()) {
      const auto start = meter::monotonic_ms();
      display_->update();
      display_updated(meter::monotonic_ms() - start);
    }
  }
  ESP_LOGI("meter_rotation", "position=%u degrees=%d saved=%d", rotation_.position(), meter::orientation::degrees(rotation_.position()), saved);
}
void MeterNetwork::display_updated(uint32_t duration_ms) {
  const uint64_t now = meter::monotonic_ms();
  if (now < next_display_log_ && displayed_observation_ == state_.model.observed &&
      meter::eq(displayed_status_,frame_.status)) return;
  next_display_log_ = now + 10000;
  displayed_observation_ = state_.model.observed;
  std::strcpy(displayed_status_,frame_.status);
  char labels[28]{}, values[80]{}, coverage[10]{};
  for(size_t i=0;i<frame_.day_count;++i) {
    if(i) { std::strcat(labels,","); std::strcat(values,","); }
    std::strcat(labels,frame_.bars[i].label); std::strcat(values,frame_.bars[i].value);
    coverage[i]=frame_.bars[i].coverage;
  }
  ESP_LOGI("meter_display", "status=%s quota=%s reset=%s offset=%s due=%d age=%s labels=%s coverage=%s values=%s observation=%lld duration_ms=%u heap=%u uptime=%llu",
           frame_.status,frame_.quota,frame_.reset,frame_.offset,frame_.reset_due,frame_.age,labels,coverage,values,
           static_cast<long long>(state_.model.observed),duration_ms,static_cast<unsigned>(esp_get_free_heap_size()),
           static_cast<unsigned long long>(now/1000));
}
} // namespace esphome::meter_network
