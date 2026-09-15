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
struct Bar {
  char label[3] = "--", value[8] = "?";
  uint8_t height = 0;
  char coverage = 'U'; // complete, partial, unknown, future
};
struct Frame {
  char quota[8] = "--%", status[6] = "WAIT", age[24] = "CONNECTING WIFI";
  char reset[24] = "--", offset[10] = "--", date[11] = "--", time[9] = "--";
  char resets[11]{};
  uint32_t resets_color = MUTED;
  uint8_t day_count = 7;
  uint16_t gauge = 0;
  bool stale = false, reset_due = false, known = false;
  Bar bars[9];
};
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
  const int hour=(m.reset_local[11]-'0')*10+m.reset_local[12]-'0';
  std::snprintf(f.time,sizeof(f.time),"%d:%.2s %s",hour%12?hour%12:12,m.reset_local+14,hour<12?"AM":"PM");
  std::snprintf(f.reset,sizeof(f.reset),"%s %s",f.date,f.time);
  const int oh=(m.reset_local[18]-'0')*10+m.reset_local[19]-'0', om=(m.reset_local[21]-'0')*10+m.reset_local[22]-'0';
  if(om) std::snprintf(f.offset,sizeof(f.offset),"(%c%d:%02d)",m.reset_local[17],oh,om);
  else if(oh) std::snprintf(f.offset,sizeof(f.offset),"(%c%d)",m.reset_local[17],oh);
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
    std::strcpy(b.label, d.label);
    b.coverage = eq(d.coverage, "complete") ? 'C' : eq(d.coverage, "partial") ? 'P' :
                 eq(d.coverage, "future") ? 'F' : 'U';
    if (b.coverage == 'U') continue;
    if (b.coverage == 'F') { std::strcpy(b.value, "0>"); continue; }
    const char *prefix = b.coverage == 'P' ? "~" : "";
    if (d.delta > 0 && d.delta < 1) std::snprintf(b.value, sizeof(b.value), "%s<1", prefix);
    else std::snprintf(b.value, sizeof(b.value), "%s%.0f", prefix, d.delta);
    b.height = d.delta <= 0 ? 0 : std::max(1L, std::lround(d.delta * BAR_HEIGHT / 100));
  }
  return f;
}
inline bool equal(const Frame &a, const Frame &b) {
  if (!eq(a.resets,b.resets) || a.resets_color != b.resets_color || !eq(a.quota,b.quota) || !eq(a.status,b.status) || !eq(a.age,b.age) ||
      !eq(a.reset,b.reset) || !eq(a.offset,b.offset) || a.gauge != b.gauge ||
      a.stale != b.stale || a.reset_due != b.reset_due || a.known != b.known || a.day_count != b.day_count) return false;
  for (size_t i=0;i<a.day_count;++i) {
    const auto &x=a.bars[i]; const auto &y=b.bars[i];
    if (!eq(x.label,y.label) || !eq(x.value,y.value) || x.height!=y.height || x.coverage!=y.coverage) return false;
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
template<class Canvas> void text(Canvas &c,int x,int y,const char *s,int scale,uint32_t color) {
  for(;*s;++s,x+=6*scale) {
    const auto *g=glyph(*s);
    for(int col=0;col<5;++col)for(int row=0;row<7;++row)
      if(g[col]&(1<<row))c.rect(x+col*scale,y+row*scale,scale,scale,color);
  }
}
template<class Canvas> void draw(Canvas &c,const Frame &f,uint8_t position=0) {
  const bool portrait = position & 1;
  const int width = orientation::width(position), height = orientation::height(position);
  c.rect(0,0,width,height,BG);
  const auto accent = f.stale ? WARN : GOOD;
  text(c,orientation::TITLE_X,orientation::TITLE_Y,"CODEX",2,INK);
  c.rect(width-90,14,6,6,f.known?accent:MUTED);
  text(c,width-76,10,f.status,2,f.known?accent:MUTED);
  const int quota_y = portrait ? 42 : 35;
  const int counter_right = width-18;
  const int counter_left = portrait ? 12+text_width(f.quota,6)+2 : 258;
  // Very long counts need the landscape label's space; keep that label below.
  const bool compact_label = !portrait && f.resets[0] &&
      7+4+text_width(f.resets,1) > counter_right-counter_left;
  if (f.resets[0]) {
    const int left = compact_label ? 180 : counter_left;
    const int scale = 14+4+text_width(f.resets,2) <= counter_right-left ? 2 : 1;
    const int icon = 7*scale, group = icon+4+text_width(f.resets,scale);
    const int x = counter_right-group, y = quota_y;
    static constexpr uint8_t reload[7] = {0x1C,0x22,0x41,0x41,0x45,0x26,0x16};
    for(int col=0;col<7;++col)for(int row=0;row<7;++row)
      if(reload[col]&(1<<row))c.rect(x+col*scale,y+row*scale,scale,scale,f.resets_color);
    text(c,x+icon+4,y,f.resets,scale,f.resets_color);
  }
  text(c,12,quota_y,f.quota,6,f.known?accent:MUTED);
  if (portrait) {
    text(c,12,98,"WEEKLY REMAINING",2,MUTED);
    text(c,12,131,f.age,1,f.stale?WARN:MUTED);
  } else {
    if (compact_label) text(c,180,58,"WEEKLY REMAINING",1,MUTED);
    else { text(c,180,38,"WEEKLY",2,INK); text(c,180,58,"REMAINING",2,MUTED); }
    text(c,180,77,f.age,1,f.stale?WARN:MUTED);
  }
  const int gauge_y = portrait ? 119 : 88;
  const int gauge = (f.gauge * (width-24) + 148) / 296;
  c.rect(12,gauge_y,width-24,5,TRACK);
  if(gauge)c.rect(12,gauge_y,gauge,5,accent);
  text(c,12,portrait?153:100,f.reset_due?"RESET DUE - LAST KNOWN":"RESETS AT",1,f.reset_due?WARN:MUTED);
  if(portrait) {
    text(c,12,165,f.date,2,INK);
    text(c,12,186,f.time,2,INK);
    text(c,12+text_width(f.time,2)+12,186,f.offset,2,INK);
  } else {
    text(c,12,112,f.reset,2,INK);
    const int x=12+text_width(f.reset,2)+12;
    const int scale=x+text_width(f.offset,2)<=width-12?2:1;
    text(c,x,112+(2-scale)*7,f.offset,scale,INK);
  }
  text(c,12,portrait?216:138,"DAILY USE / WEEKLY PP",1,MUTED);
  text(c,width-66,portrait?216:138,"0-100",1,MUTED);
  const int baseline = portrait ? 279 : 197;
  const int slots=f.day_count, spacing=(width-24)/slots, bar_width=std::min(portrait?18:22,spacing-6);
  for(int i=0;i<slots;++i) {
    const auto &b=f.bars[i]; int center=12+(2*i+1)*(width-24)/(2*slots), x=center-bar_width/2;
    uint32_t color=b.coverage=='P'?WARN:b.coverage=='C'?GOOD:MUTED;
    text(c,center-text_width(b.value,1)/2,portrait?232:151,b.value,1,color);
    // Zero-height future/measured bars remain zero; explicit values distinguish them.
    c.rect(x,baseline,bar_width,1,TRACK);
    if(b.height) {
      int y=baseline-b.height;
      c.rect(x,y,bar_width,b.height,color);
      if(b.coverage=='P')for(int row=y+2;row<baseline;row+=4)c.rect(x,row,bar_width,1,BG);
    }
    text(c,center-text_width(b.label,2)/2,portrait?287:203,b.label,2,b.coverage=='F'?MUTED:INK);
  }
  text(c,12,portrait?309:226,"~PARTIAL  ?UNKNOWN  >FUTURE",1,MUTED);
}
} // namespace meter::ui
