#pragma once
#include <algorithm>
#include <cstdint>

// Coordinates here are relative to the original 320x240 landscape view.
// The requested bottom-edge order is bottom, right, top, left.
namespace meter::orientation {
struct Point { int x, y; };
struct Rect {
  int x, y, width, height;
  constexpr bool contains(Point p) const {
    return p.x >= x && p.y >= y && p.x < x + width && p.y < y + height;
  }
};
constexpr Rect TITLE{4, 0, 84, 32};
constexpr int TITLE_X = 12, TITLE_Y = 10;
constexpr int width(uint8_t position) { return position & 1 ? 240 : 320; }
constexpr int height(uint8_t position) { return position & 1 ? 320 : 240; }
constexpr int degrees(uint8_t position) { return (450 - 90 * position) % 360; }

inline Point to_view(Point p, uint8_t position) {
  if (p.x < 0 || p.x >= 320 || p.y < 0 || p.y >= 240 || position > 3) return {-1, -1};
  switch (position) {
    case 1: return {239 - p.y, p.x};
    case 2: return {319 - p.x, 239 - p.y};
    case 3: return {p.y, 319 - p.x};
    default: return p;
  }
}

struct Calibration {
  int x_min = 280, x_max = 3860, y_min = 340, y_max = 3860;
  bool swap_xy = false, mirror_x = false, mirror_y = false;
  Point normalize(int x, int y) const {
    if (x < 0 || x > 4095 || y < 0 || y > 4095 || x_max - x_min < 10 || y_max - y_min < 10)
      return {-1, -1};
    if (swap_xy) std::swap(x, y);
    x = (std::clamp(x, x_min, x_max) - x_min) * 319 / (x_max - x_min);
    y = (std::clamp(y, y_min, y_max) - y_min) * 239 / (y_max - y_min);
    return {mirror_x ? 319 - x : x, mirror_y ? 239 - y : y};
  }
};

class Tap {
 public:
  void press(Point p, uint64_t now) {
    if (active_) return;
    active_ = true;
    eligible_ = now >= ready_after_ && TITLE.contains(p);
    pressed_at_ = now;
  }
  void move(Point p) {
    if (active_ && !TITLE.contains(p)) eligible_ = false;
  }
  bool release(uint64_t now) {
    if (!active_) return false;
    const bool accepted = eligible_ && now >= pressed_at_ && now - pressed_at_ >= 40;
    active_ = eligible_ = false;
    ready_after_ = now + 180;
    return accepted;
  }
 private:
  bool active_ = false, eligible_ = false;
  uint64_t pressed_at_ = 0, ready_after_ = 0;
};

// A store has load(uint8_t&), save(uint8_t), and sync(). The ESPHome adapter
// and fake-store tests execute the same restore/advance logic.
class Rotation {
 public:
  uint8_t position() const { return position_; }
  template<class Store> bool restore(Store &store) {
    uint8_t saved = 0;
    const bool valid = store.load(saved) && saved < 4;
    position_ = valid ? saved : 0;
    return valid;
  }
  template<class Store> bool advance(Store &store) {
    position_ = (position_ + 1) % 4;
    return store.save(position_) && store.sync();
  }
 private:
  uint8_t position_ = 0;
};
}  // namespace meter::orientation
