#pragma once
#include "model.h"
#include "orientation.h"
#include <cstdio>

// Landscape and portrait views shared by the device and native bounds checks.
// Colors are RGB integers; a platform adapter supplies rect(x,y,w,h,color).
namespace meter::ui {
constexpr uint32_t BG = 0x101820, INK = 0xF3F6F7, MUTED = 0xAAB8C2;
constexpr uint32_t TRACK = 0x324450, GOOD = 0x63DFC1, WARN = 0xFFC269;
constexpr uint32_t RESET_BLUE = 0x60BFFF, RESET_YELLOW = 0xFFE060, RESET_RED = 0xFF6060;
constexpr int WIDTH = 320, HEIGHT = 240, BAR_HEIGHT = 38;
constexpr int PORTRAIT_BAR_WIDTH = 168;
struct Bar {
  char label[3] = "--", value[8] = "?";
  uint8_t height = 0;
  char coverage = 'U'; // complete, partial, unknown, future
  uint8_t percent = 0, width = 0; // Portrait values; retain full precision when sizing the fill.
};
struct Frame {
  char quota[8] = "--%", status[6] = "WAIT", age[24] = "CONNECTING WIFI";
  char reset[24] = "--", offset[10] = "--", date[11] = "--", time[9] = "--";
  char resets[11]{};
  uint32_t resets_color = MUTED;
  uint8_t day_count = 7;
  uint16_t gauge = 0;
  bool stale = false, reset_due = false, known = false;
  int8_t today = -1;
  Bar bars[9];
};
static_assert(sizeof(Frame) <= 256, "Keep the frozen display frame bounded");
inline Frame project(const State &state, uint64_t now, bool wifi) {
  Frame f;
  f.known = state.accepted && state.model.observed >= 0;
  f.stale = !eq(state.status(now), "ok");
  std::strcpy(f.status, f.known ? (f.stale ? "STALE" : "FRESH") : "WAIT");
  if (!f.known) {
    std::strcpy(f.age, wifi ? "WAITING FOR DATA" : "CONNECTING WIFI");
    return f;
  }
  const auto &m = state.model;
  const uint64_t as_of = state.as_of(now);
  if (m.resets > 0 && (m.resets_expire < 0 || as_of < static_cast<uint64_t>(m.resets_expire))) {
    std::snprintf(f.resets, sizeof(f.resets), "%lld", static_cast<long long>(m.resets));
    if (m.resets_expire >= 0 && !eq(m.reason, "clock_error")) {
      const uint64_t left = static_cast<uint64_t>(m.resets_expire) - as_of;
      f.resets_color = left < 4*DAY ? RESET_RED : left <= 7*DAY ? RESET_YELLOW : RESET_BLUE;
    }
  }
  if (m.remaining > 0 && m.remaining < 1) std::strcpy(f.quota, "<1%");
  else if (m.remaining > 99 && m.remaining < 100) std::strcpy(f.quota, ">99%");
  else std::snprintf(f.quota, sizeof(f.quota), "%.0f%%", m.remaining);
  f.gauge = static_cast<uint16_t>(std::lround(m.remaining * 296 / 100));
  std::memcpy(f.date,m.reset_local,10); f.date[10]=0;
  const unsigned hour=(m.reset_local[11]-'0')*10+m.reset_local[12]-'0';
  std::snprintf(f.time,sizeof(f.time),"%u:%.2s %s",hour%12?hour%12:12,m.reset_local+14,hour<12?"AM":"PM");
  std::snprintf(f.reset,sizeof(f.reset),"%s %s",f.date,f.time);
  // The decoder validates these digits; explicit bounds also prove the buffer sizes to GCC.
  const unsigned oh=static_cast<unsigned>((m.reset_local[18]-'0')*10+m.reset_local[19]-'0')%100;
  const unsigned om=static_cast<unsigned>((m.reset_local[21]-'0')*10+m.reset_local[22]-'0')%100;
  if(om) std::snprintf(f.offset,sizeof(f.offset),"(%c%u:%02u)",m.reset_local[17],oh,om);
  else if(oh) std::snprintf(f.offset,sizeof(f.offset),"(%c%u)",m.reset_local[17],oh);
  else std::strcpy(f.offset,"(0)");
  f.day_count=m.day_count;
  f.reset_due = state.as_of(now) >= static_cast<uint64_t>(m.reset);
  uint64_t age = state.age(now);
  const char *unit = "S";
  if (age >= 86400) { age /= 86400; unit = "D"; }
  else if (age >= 3600) { age /= 3600; unit = "H"; }
  else if (age >= 60) { age /= 60; unit = "M"; }
  // Bound the display text even after an arbitrarily long monotonic outage.
  std::snprintf(f.age, sizeof(f.age), "%s %s%llu%s", wifi ? "AGE" : "OFFLINE",
                age > 99999 ? ">" : "", static_cast<unsigned long long>(std::min<uint64_t>(age, 99999)), unit);
  for (size_t i = 0; i < f.day_count; ++i) {
    const Day d = state.day(i, now); auto &b = f.bars[i];
    if (as_of >= static_cast<uint64_t>(d.start) && as_of < static_cast<uint64_t>(d.end))
      f.today = static_cast<int8_t>(i);
    std::strcpy(b.label, d.label);
    b.coverage = eq(d.coverage, "complete") ? 'C' : eq(d.coverage, "partial") ? 'P' :
                 eq(d.coverage, "future") ? 'F' : 'U';
    if (b.coverage == 'U') continue;
    if (b.coverage == 'F') { std::strcpy(b.value, "0"); continue; }
    b.percent = static_cast<uint8_t>(std::lround(d.delta));
    b.width = d.delta <= 0 ? 0 : std::max(1L, std::lround(d.delta * PORTRAIT_BAR_WIDTH / 100));
    std::snprintf(b.value, sizeof(b.value), "%u", static_cast<unsigned>(b.percent));
    b.height = d.delta <= 0 ? 0 : std::max(1L, std::lround(d.delta * BAR_HEIGHT / 100));
  }
  return f;
}
inline bool equal(const Frame &a, const Frame &b) {
  if (!eq(a.resets,b.resets) || a.resets_color != b.resets_color || !eq(a.quota,b.quota) || !eq(a.status,b.status) || !eq(a.age,b.age) ||
      !eq(a.reset,b.reset) || !eq(a.offset,b.offset) || a.gauge != b.gauge ||
      a.stale != b.stale || a.reset_due != b.reset_due || a.known != b.known || a.day_count != b.day_count || a.today != b.today) return false;
  for (size_t i=0;i<a.day_count;++i) {
    const auto &x=a.bars[i]; const auto &y=b.bars[i];
    if (!eq(x.label,y.label) || !eq(x.value,y.value) || x.height!=y.height || x.coverage!=y.coverage ||
        x.percent!=y.percent || x.width!=y.width) return false;
  }
  return true;
}
inline const uint8_t *glyph(char c) {
  static constexpr uint8_t digits[10][5] = {
    {0x3E,0x51,0x49,0x45,0x3E},{0x00,0x42,0x7F,0x40,0x00},{0x42,0x61,0x51,0x49,0x46},
    {0x21,0x41,0x45,0x4B,0x31},{0x18,0x14,0x12,0x7F,0x10},{0x27,0x45,0x45,0x45,0x39},
    {0x3C,0x4A,0x49,0x49,0x30},{0x01,0x71,0x09,0x05,0x03},{0x36,0x49,0x49,0x49,0x36},
    {0x06,0x49,0x49,0x29,0x1E}};
  static constexpr uint8_t letters[26][5] = {
    {0x7E,0x11,0x11,0x11,0x7E},{0x7F,0x49,0x49,0x49,0x36},{0x3E,0x41,0x41,0x41,0x22},
    {0x7F,0x41,0x41,0x22,0x1C},{0x7F,0x49,0x49,0x49,0x41},{0x7F,0x09,0x09,0x09,0x01},
    {0x3E,0x41,0x49,0x49,0x7A},{0x7F,0x08,0x08,0x08,0x7F},{0x00,0x41,0x7F,0x41,0x00},
    {0x20,0x40,0x41,0x3F,0x01},{0x7F,0x08,0x14,0x22,0x41},{0x7F,0x40,0x40,0x40,0x40},
    {0x7F,0x02,0x0C,0x02,0x7F},{0x7F,0x04,0x08,0x10,0x7F},{0x3E,0x41,0x41,0x41,0x3E},
    {0x7F,0x09,0x09,0x09,0x06},{0x3E,0x41,0x51,0x21,0x5E},{0x7F,0x09,0x19,0x29,0x46},
    {0x46,0x49,0x49,0x49,0x31},{0x01,0x01,0x7F,0x01,0x01},{0x3F,0x40,0x40,0x40,0x3F},
    {0x1F,0x20,0x40,0x20,0x1F},{0x3F,0x40,0x38,0x40,0x3F},{0x63,0x14,0x08,0x14,0x63},
    {0x07,0x08,0x70,0x08,0x07},{0x61,0x51,0x49,0x45,0x43}};
  static constexpr uint8_t symbols[][5] = {
    {0,0,0,0,0},{0x63,0x13,0x08,0x64,0x63},{0,0x36,0x36,0,0},{0x08,0x08,0x08,0x08,0x08},
    {0x08,0x08,0x3E,0x08,0x08},{0x02,0x01,0x51,0x09,0x06},{0,0x60,0x60,0,0},
    {0,0x41,0x22,0x14,0x08},{0x20,0x10,0x08,0x04,0x02},{0x08,0x04,0x08,0x10,0x08},
    {0x08,0x14,0x22,0x41,0},{0x7F,0x08,0x04,0x04,0x78},{0x20,0x54,0x54,0x54,0x78},
    {0x3C,0x40,0x40,0x20,0x7C},{0,0x1C,0x22,0x41,0},{0,0x41,0x22,0x1C,0}};
  if(c>='0'&&c<='9')return digits[c-'0'];
  if(c>='A'&&c<='Z')return letters[c-'A'];
  const char *keys=" %:-+?.>/~<hau()";
  for(int i=0;keys[i];++i)if(c==keys[i])return symbols[i];
  return symbols[5]; // Missing glyphs stay visible during validation.
}
inline int text_width(const char *s, int scale) { return *s ? (6 * std::strlen(s)-1)*scale : 0; }
template<class Canvas> void text(Canvas &c,int x,int y,const char *s,int scale,uint32_t color,int fill_end=-1) {
  for(;*s;++s,x+=6*scale) {
    const auto *g=glyph(*s);
    for(int col=0;col<5;++col)for(int row=0;row<7;++row)
      // Portrait bar values use scale 1, switching ink exactly at the fill boundary.
      if(g[col]&(1<<row))c.rect(x+col*scale,y+row*scale,scale,scale,x+col*scale<fill_end?BG:color);
  }
}
template<class Canvas> void reset_counter(Canvas &c,const Frame &f,int left,int right,int y) {
  if (!f.resets[0]) return;
  const int scale = 14+4+text_width(f.resets,2) <= right-left ? 2 : 1;
  const int icon = 7*scale, group = icon+4+text_width(f.resets,scale);
  const int x = right-group;
  static constexpr uint8_t reload[7] = {0x1C,0x22,0x41,0x41,0x45,0x26,0x16};
  for(int col=0;col<7;++col)for(int row=0;row<7;++row)
    if(reload[col]&(1<<row))c.rect(x+col*scale,y+row*scale,scale,scale,f.resets_color);
  text(c,x+icon+4,y,f.resets,scale,f.resets_color);
}
inline const char *weekday(const char *label) {
  static constexpr const char *short_labels[] = {"M","T","W","Th","F","Sa","Su"};
  static constexpr const char *names[] = {"MON","TUE","WED","THU","FRI","SAT","SUN"};
  for (size_t i=0;i<7;++i) if (eq(label,short_labels[i])) return names[i];
  return "--";
}
inline void portrait_value(const Bar &b,char (&value)[5]) {
  if (b.coverage=='U') std::strcpy(value,"?");
  else std::snprintf(value,sizeof(value),"%u%%",static_cast<unsigned>(b.percent));
}
template<class Canvas> void draw_portrait(Canvas &c,const Frame &f) {
  c.rect(0,0,240,320,BG);
  const auto accent = f.stale ? WARN : GOOD;
  text(c,orientation::TITLE_X,orientation::TITLE_Y,"CODEX",2,INK);
  reset_counter(c,f,orientation::TITLE.x+orientation::TITLE.width,228,10);
  text(c,12,42,f.quota,6,f.known?accent:MUTED);
  text(c,156,49,"WEEKLY",1,INK);
  text(c,156,64,"REMAINING",1,MUTED);
  c.rect(12,92,216,5,TRACK);
  const int gauge = (f.gauge*216+148)/296;
  if(gauge)c.rect(12,92,gauge,5,accent);
  text(c,12,106,f.reset_due?"RESET DUE - LAST KNOWN":"RESETS AT",1,f.reset_due?WARN:MUTED);
  text(c,12,119,f.reset,1,INK);
  text(c,12+text_width(f.reset,1)+6,119,f.offset,1,INK);
  c.rect(12,135,216,1,TRACK);
  text(c,12,145,"DAILY USAGE %",1,MUTED);
  for(int i=0;i<f.day_count;++i) {
    const auto &b=f.bars[i];
    const int y=162+i*(f.day_count==9?18:20);
    const auto color=i==f.today?RESET_YELLOW:GOOD;
    text(c,12,y,weekday(b.label),2,color);
    c.rect(60,y,PORTRAIT_BAR_WIDTH,14,TRACK);
    if(b.width)c.rect(60,y,b.width,14,color);
    char value[5]; portrait_value(b,value);
    text(c,60+(PORTRAIT_BAR_WIDTH-text_width(value,1))/2,y+4,value,1,INK,60+b.width);
  }
}
template<class Canvas> void draw(Canvas &c,const Frame &f,uint8_t position=0) {
  if (position & 1) { draw_portrait(c,f); return; }
  const int width = orientation::width(position), height = orientation::height(position);
  c.rect(0,0,width,height,BG);
  const auto accent = f.stale ? WARN : GOOD;
  text(c,orientation::TITLE_X,orientation::TITLE_Y,"CODEX",2,INK);
  reset_counter(c,f,orientation::TITLE.x+orientation::TITLE.width,width-12,10);
  const int quota_y = 35;
  text(c,12,quota_y,f.quota,6,f.known?accent:MUTED);
  text(c,180,38,"WEEKLY",2,INK);
  text(c,180,58,"REMAINING",2,MUTED);
  const int gauge_y = 88;
  const int gauge = (f.gauge * (width-24) + 148) / 296;
  c.rect(12,gauge_y,width-24,5,TRACK);
  if(gauge)c.rect(12,gauge_y,gauge,5,accent);
  text(c,12,100,f.reset_due?"RESET DUE - LAST KNOWN":"RESETS AT",1,f.reset_due?WARN:MUTED);
  text(c,12,112,f.reset,2,INK);
  const int offset_x=12+text_width(f.reset,2)+12;
  const int offset_scale=offset_x+text_width(f.offset,2)<=width-12?2:1;
  text(c,offset_x,112+(2-offset_scale)*7,f.offset,offset_scale,INK);
  text(c,12,138,"DAILY USAGE %",1,MUTED);
  const int baseline = 197;
  const int slots=f.day_count, spacing=(width-24)/slots, bar_width=std::min(22,spacing-6);
  for(int i=0;i<slots;++i) {
    const auto &b=f.bars[i]; int center=12+(2*i+1)*(width-24)/(2*slots), x=center-bar_width/2;
    const bool today=i==f.today;
    const uint32_t color=today?RESET_YELLOW:(b.coverage=='P'||b.coverage=='C')?GOOD:MUTED;
    text(c,center-text_width(b.value,1)/2,151,b.value,1,color);
    // Future and measured zero share a plain zero and retain zero bar height.
    c.rect(x,baseline,bar_width,1,TRACK);
    if(b.height) {
      int y=baseline-b.height;
      c.rect(x,y,bar_width,b.height,color);
    }
    text(c,center-text_width(b.label,2)/2,203,b.label,2,today?RESET_YELLOW:b.coverage=='F'?MUTED:INK);
  }
}
} // namespace meter::ui
