#include "firmware/components/meter_network/ui.h"
#include <cassert>
#include <chrono>
#include <fstream>
#include <iostream>
#include <iterator>
#include <string>
#include <vector>

struct Canvas {
  int width, height;
  std::vector<uint8_t> pixels;
  size_t calls = 0;
  int clip_start = 0, clip_end;
  explicit Canvas(uint8_t position) : width(meter::orientation::width(position)),
    height(meter::orientation::height(position)), pixels(width*height*3), clip_end(height) {}
  void rect(int x,int y,int w,int h,uint32_t rgb) {
    assert(x>=0 && y>=0 && w>0 && h>0 && x+w<=width && y+h<=height);
    ++calls;
    for(int row=std::max(y,clip_start);row<std::min(y+h,clip_end);++row)for(int col=x;col<x+w;++col) {
      const size_t i=(row*width+col)*3;
      // The device's 8-bit buffer quantizes RGB to 3/3/2 bits.
      pixels[i]=(rgb>>16)&0xE0; pixels[i+1]=(rgb>>8)&0xE0; pixels[i+2]=rgb&0xC0;
    }
  }
  void save(const char *path) const {
    std::ofstream out(path,std::ios::binary);
    out << "P6\n" << width << ' ' << height << "\n255\n";
    out.write(reinterpret_cast<const char *>(pixels.data()),pixels.size());
    assert(out.good());
  }
};
static void emit(const meter::ui::Frame &f) {
  std::cout << "{\"quota\":\"" << f.quota << "\",\"status\":\"" << f.status
            << "\",\"age\":\"" << f.age << "\",\"reset\":\"" << f.reset
            << "\",\"offset\":\"" << f.offset << "\",\"due\":" << f.reset_due
            << ",\"resets\":\"" << f.resets << "\",\"resets_color\":" << f.resets_color
            << ",\"gauge\":" << f.gauge << ",\"today\":" << static_cast<int>(f.today) << ",\"bars\":[";
  for(int i=0;i<f.day_count;++i) {
    if(i)std::cout << ',';
    const auto &b=f.bars[i];
    char value[5]; meter::ui::portrait_value(b,value);
    std::cout << "{\"label\":\"" << b.label << "\",\"value\":\"" << b.value
              << "\",\"coverage\":\"" << b.coverage << "\",\"height\":" << static_cast<int>(b.height)
              << ",\"portrait_value\":\"" << value << "\",\"portrait_label\":\"" << meter::ui::weekday(b.label)
              << "\",\"width\":" << static_cast<int>(b.width) << '}';
  }
  std::cout << "]}" << '\n';
}
int main(int argc,char **argv) {
  if(argc!=5 && argc!=6)return 2;
  const int position=argc==6?std::stoi(argv[5]):0;
  assert(position>=0 && position<4);
  std::ifstream input(argv[1],std::ios::binary);
  std::string raw((std::istreambuf_iterator<char>(input)),{});
  meter::Model m; assert(meter::decode(raw.c_str(),raw.size(),m));
  if(m.observed>=0 && meter::eq(m.status,"ok")) {
    meter::State update;
    auto next=m;
    next.resets=2; next.resets_expire=next.as_of+2*meter::DAY;
    assert(update.accept(next,0));
    auto red=meter::ui::project(update,0,true);
    assert(meter::eq(red.resets,"2") && red.resets_color==meter::ui::RESET_RED);
    next.observed+=60; next.updated+=60; next.as_of+=60;
    next.resets=1; next.resets_expire+=10*meter::DAY;
    assert(update.accept(next,60000));
    auto blue=meter::ui::project(update,60000,true);
    assert(meter::eq(blue.resets,"1") && blue.resets_color==meter::ui::RESET_BLUE);
    assert(!meter::ui::equal(red,blue));
    auto conflicting=next; conflicting.resets_expire++;
    assert(!update.accept(conflicting,60000));
    next.observed+=60; next.updated+=60; next.as_of+=60;
    next.resets=0; next.resets_expire=-1;
    assert(update.accept(next,120000));
    assert(!meter::ui::project(update,120000,true).resets[0]);
  }
  meter::State s; assert(s.accept(m,0));
  uint64_t now=std::stoull(argv[3]); bool wifi=std::string(argv[4])=="online";
  if(!wifi)s.transport_failed=true;
  auto f=meter::ui::project(s,now,wifi);
  assert(meter::ui::equal(f,meter::ui::project(s,now,wifi)));
  const auto initial=meter::ui::project(s,0,wifi);
  assert(meter::ui::equal(initial,meter::ui::project(s,500,wifi)));
  if(m.observed>=0 && s.age(0)<59) assert(!meter::ui::equal(initial,meter::ui::project(s,1000,wifi)));
  auto before=meter::ui::project(s,0,true);
  if(m.observed>=0 && now>=180000)assert(!meter::ui::equal(before,f));
  Canvas c(position); meter::ui::draw(c,f,position); c.save(argv[2]); emit(f);
  {
    // Status, age, and partial coverage must not alter either layout's pixels.
    auto hidden=f;
    std::strcpy(hidden.status,"XXXXX"); std::strcpy(hidden.age,"XXXXXXXXXXXX");
    for(int i=0;i<hidden.day_count;++i) {
      if(position&1) std::strcpy(hidden.bars[i].value,"~>99");
      if(hidden.bars[i].coverage=='P') hidden.bars[i].coverage='C';
    }
    Canvas alternate(position); meter::ui::draw(alternate,hidden,position);
    assert(c.pixels==alternate.pixels);
  }
  if(m.observed>=0) {
    // Width-only changes (invisible to the old 38px bars) still trigger a redraw.
    auto narrow=m; narrow.days[0].delta=1; std::strcpy(narrow.days[0].coverage,"partial");
    auto wider=narrow; wider.days[0].delta=1.49;
    meter::State a,b; assert(a.accept(narrow,0) && b.accept(wider,0));
    const auto small=meter::ui::project(a,0,true), large=meter::ui::project(b,0,true);
    assert(meter::eq(small.bars[0].value,large.bars[0].value));
    assert(small.bars[0].height==large.bars[0].height);
    assert(!meter::ui::equal(small,large));
    // At midnight the day highlight alone can change while a minute-rounded age stays fixed.
    auto midnight=m;
    const auto boundary=m.days[1].end;
    midnight.as_of=boundary-1; midnight.observed=boundary-182; midnight.age=181;
    for(int i=0;i<midnight.day_count;++i) {
      std::strcpy(midnight.days[i].coverage,"unknown"); midnight.days[i].delta=-1;
    }
    meter::State clock; assert(clock.accept(midnight,0));
    const auto yesterday=meter::ui::project(clock,0,false), today=meter::ui::project(clock,1000,false);
    assert(meter::eq(yesterday.age,today.age) && meter::eq(yesterday.status,today.status));
    assert(yesterday.today==1 && today.today==2 && !meter::ui::equal(yesterday,today));
  }
  Canvas paged(position);
  for(int page=0;page<4;++page) {
    paged.clip_start=page*paged.height/4; paged.clip_end=(page+1)*paged.height/4;
    meter::ui::draw(paged,f,position);
  }
  assert(c.pixels==paged.pixels); // Quarter-buffer replay produces exactly one full view.
  auto start=std::chrono::steady_clock::now();
  for(int i=0;i<100;++i)meter::ui::draw(c,f,position);
  auto us=std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now()-start).count();
  std::cerr << "frame_bytes=" << sizeof(f) << " sanitized_render_mean_us=" << us/100.0 << '\n';
}
