#include "firmware/components/meter_network/orientation.h"
#include <cassert>
#include <iostream>
#include <string>

namespace o = meter::orientation;
struct Store {
  uint8_t disk = 0, pending = 0;
  bool present = false, save_ok = true, sync_ok = true;
  int saves = 0, syncs = 0;
  bool load(uint8_t &value) { value = disk; return present; }
  bool save(uint8_t value) { ++saves; pending = value; return save_ok; }
  bool sync() { ++syncs; if (!sync_ok) return false; disk = pending; present = true; return true; }
};

// Independent model of the ILI9341 pixel rotations used by ESPHome's display
// interface. Convert the native portrait panel back to the original view.
o::Point panel_pixel(o::Point view, int degrees) {
  o::Point native{};
  switch (degrees) {
    case 0: native = view; break;
    case 90: native = {239 - view.y, view.x}; break;
    case 180: native = {239 - view.x, 319 - view.y}; break;
    case 270: native = {view.y, 319 - view.x}; break;
    default: assert(false);
  }
  return {native.y, 239 - native.x};
}

int main(int argc, char **argv) {
  if (argc == 8) {
    const o::Calibration panel{std::stoi(argv[1]),std::stoi(argv[2]),std::stoi(argv[3]),std::stoi(argv[4]),
                               std::stoi(argv[5])!=0,std::stoi(argv[6])!=0,std::stoi(argv[7])!=0};
    // Actual CYD readings on 2026-09-12: the user held the original landscape
    // position and tapped near top-left, top-right, then bottom-left. Raw X
    // increases DOWN; raw Y increases RIGHT. Near-corner taps allow 24px inset.
    const o::Point raw[] = {{398,282},{342,3660},{3727,294}};
    const o::Point corners[4][3] = {
      {{0,0},{319,0},{0,239}}, {{239,0},{239,319},{0,0}},
      {{319,239},{0,239},{319,0}}, {{0,319},{0,0},{239,319}}
    };
    for (uint8_t pos=0;pos<4;++pos) for (int i=0;i<3;++i) {
      const auto view = o::to_view(panel.normalize(raw[i].x,raw[i].y),pos);
      assert(std::abs(view.x-corners[pos][i].x)<=24 && std::abs(view.y-corners[pos][i].y)<=24);
    }
    // The user's four CODEX presses after the repair, one in each orientation.
    // Check against the actual shared title rectangle, not a corner tolerance.
    const o::Point title_raw[] = {{570,660},{3293,400},{3624,3329},{882,3573}};
    for (uint8_t pos=0;pos<4;++pos) {
      const auto point = o::to_view(panel.normalize(title_raw[pos].x,title_raw[pos].y),pos);
      assert(o::TITLE.contains(point));
      o::Tap tap;
      tap.press(point,1000);
      assert(tap.release(1200));
      assert(!tap.release(1300));
    }
    std::cout << "configured panel calibration: 3 physical corner samples x 4 orientations and 4 measured title taps passed\n";
  } else {
    assert(argc == 1);
  }
  const o::Point bottom_midpoints[] = {{160,239},{319,120},{160,0},{0,120}};
  const o::Point title_points[] = {{45,16},{16,194},{274,223},{303,45}};
  for (uint8_t pos=0;pos<4;++pos) {
    auto bottom = o::to_view(bottom_midpoints[pos],pos);
    assert(bottom.y == o::height(pos)-1);
    assert(o::TITLE.contains(o::to_view(title_points[pos],pos)));
    // Every panel pixel maps exactly to one in-bounds logical pixel, and the
    // selected driver rotation maps it back to the same physical panel pixel.
    for(int y=0;y<240;++y) for(int x=0;x<320;++x) {
      auto view = o::to_view({x,y},pos);
      assert(view.x>=0 && view.x<o::width(pos) && view.y>=0 && view.y<o::height(pos));
      auto physical = panel_pixel(view,o::degrees(pos));
      assert(physical.x==x && physical.y==y);
    }
    assert(!o::TITLE.contains(o::to_view({-1,0},pos)));
    assert(!o::TITLE.contains(o::to_view({320,240},pos)));
    Store store; store.disk=pos; store.present=true;
    o::Rotation rotation;
    assert(rotation.restore(store) && rotation.position()==pos && store.saves==0);
    for(int tap=0;tap<4;++tap) {
      const auto next=(rotation.position()+1)%4;
      assert(rotation.advance(store) && rotation.position()==next && store.disk==next);
      o::Rotation reboot;
      assert(reboot.restore(store) && reboot.position()==next);
    }
    assert(rotation.position()==pos && store.saves==4 && store.syncs==4);
  }
  Store absent; o::Rotation rotation;
  assert(!rotation.restore(absent) && rotation.position()==0 && absent.saves==0);
  for(int invalid=4;invalid<256;++invalid) {
    Store bad; bad.present=true; bad.disk=invalid;
    assert(!rotation.restore(bad) && rotation.position()==0 && bad.saves==0);
  }
  Store failed; failed.present=true; failed.disk=3; failed.save_ok=false;
  assert(rotation.restore(failed));
  assert(!rotation.advance(failed) && rotation.position()==0 && failed.syncs==0 && failed.disk==3);
  failed.save_ok=true; failed.sync_ok=false;
  assert(!rotation.advance(failed) && rotation.position()==1 && failed.syncs==1 && failed.disk==3);
  failed.sync_ok=true;
  assert(rotation.advance(failed) && failed.disk==2);

  o::Calibration cal;
  assert(cal.normalize(280,340).x==0 && cal.normalize(280,340).y==0);
  assert(cal.normalize(3860,3860).x==319 && cal.normalize(3860,3860).y==239);
  assert(cal.normalize(0,4095).x==0 && cal.normalize(0,4095).y==239);
  assert(cal.normalize(-1,1).x==-1 && cal.normalize(1,4096).y==-1);
  cal.mirror_x=true; cal.mirror_y=true;
  assert(cal.normalize(280,340).x==319 && cal.normalize(280,340).y==239);
  cal.swap_xy=true;
  assert(cal.normalize(340,280).x==319 && cal.normalize(340,280).y==239);
  cal.x_max=cal.x_min;
  assert(cal.normalize(1000,1000).x==-1);

  o::Tap tap;
  assert(!tap.release(0));
  tap.press({45,16},1000); tap.move({46,17});
  assert(tap.release(1100));
  assert(!tap.release(1110));
  // Debounce a release/repress bounce; holding alone never emits an action.
  tap.press({45,16},1150); assert(!tap.release(1200));
  tap.press({45,16},2000);
  for(int i=0;i<100;++i) tap.move({45,16});
  tap.press({45,16},3000); // duplicate press cannot manufacture another tap
  assert(tap.release(8000));
  assert(!tap.release(8100));
  // Dragging out cancels even after returning; dragging in cannot arm a tap.
  tap.press({45,16},9000); tap.move({90,16}); tap.move({45,16});
  assert(!tap.release(9100));
  tap.press({100,100},10000); tap.move({45,16}); assert(!tap.release(10200));
  tap.press({45,16},11000); assert(!tap.release(11010)); // brief noise
  tap.press({45,16},12000); assert(!tap.release(11999)); // invalid clock ordering
  assert(o::TITLE.contains({4,0}) && o::TITLE.contains({87,31}));
  assert(!o::TITLE.contains({88,31}) && !o::TITLE.contains({87,32}));
  std::cout << "orientation: 307200 pixel round trips, edge order, all restored positions, invalid/failed persistence, calibration and tap/drag/debounce passed\n";
}
