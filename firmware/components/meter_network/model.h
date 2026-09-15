#pragma once

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <cstring>

// No allocations, recursive JSON tree, credentials, or platform dependencies.
namespace meter {
constexpr size_t MAX_BODY = 4096;
constexpr int64_t MAX_EPOCH = 4102444800LL;
constexpr int64_t DAY = 86400;
struct Day {
  int64_t start = -1, end = -1;
  double delta = -1;
  char label[3]{};
  char coverage[9]{};
};
struct Model {
  char scope[65]{}, timezone[65]{}, reset_local[24]{};
  char status[12]{}, reason[24]{}, source_error[24]{}, cycle_state[10]{};
  int64_t observed = -1, updated = -1, as_of = -1, age = -1;
  int64_t reset = -1, start = -1, end = -1, resets = -1, stale_after = 180;
  int64_t resets_expire = -1;
  double remaining = -1;
  uint8_t day_count = 0;
  Day days[9];
};
inline bool eq(const char *a, const char *b) { return std::strcmp(a, b) == 0; }
inline bool one_of(const char *s, const char *const *values, size_t n) {
  for (size_t i = 0; i < n; ++i) if (eq(s, values[i])) return true;
  return false;
}
template<size_t N> inline bool one_of(const char *s, const char *const (&values)[N]) {
  return one_of(s, values, N);
}

class Decoder {
 public:
  explicit Decoder(const char *input) : p_(input) {}
  bool decode(Model &m) {
    static const char *const fields[] = {"version", "scope", "bucket", "window_minutes",
      "observed_at", "updated_at", "as_of", "age_seconds", "stale_after_seconds", "status",
      "reason", "source_error", "remaining_percent", "reset_at", "reset_local", "timezone",
      "cycle", "days", "resets_available", "resets_expire_at"};
    if (!take('{')) return false;
    uint32_t seen = 0;
    for (size_t i = 0; i < 20; ++i) {
      int k = key(fields, seen); if (k < 0) return false;
      int64_t constant = 0; char bucket[6]{};
      bool ok = false;
      switch (k) {
        case 0: ok = integer(constant, 2, 2); break;
        case 1: ok = string(m.scope, true); break;
        case 2: ok = string(bucket) && eq(bucket, "codex"); break;
        case 3: ok = integer(constant, 10080, 10080); break;
        case 4: ok = integer(m.observed, 1, MAX_EPOCH, true); break;
        case 5: ok = integer(m.updated, 1, MAX_EPOCH, true); break;
        case 6: ok = integer(m.as_of, 1, MAX_EPOCH); break;
        case 7: ok = integer(m.age, 0, MAX_EPOCH, true); break;
        case 8: ok = integer(m.stale_after, 30, 3600); break;
        case 9: ok = string(m.status); break;
        case 10: ok = string(m.reason, true); break;
        case 11: ok = string(m.source_error, true); break;
        case 12: ok = number(m.remaining, 0, 100, true); break;
        case 13: ok = integer(m.reset, 1, MAX_EPOCH, true); break;
        case 14: ok = string(m.reset_local, true); break;
        case 15: ok = string(m.timezone); break;
        case 16: ok = cycle(m); break;
        case 17: ok = days(m); break;
        case 18: ok = integer(m.resets, 0, 2147483647, true); break;
        case 19: ok = integer(m.resets_expire, 1, MAX_EPOCH, true); break;
      }
      if (!ok) return false;
      if (take('}')) {
        if ((seen & ((1U << 19)-1)) != ((1U << 19)-1)) return false;
        ws(); return *p_ == '\0';
      }
      if (!take(',')) return false;
    }
    return false;
  }
 private:
  const char *p_;
  void ws() { while (*p_ == ' ' || *p_ == '\t' || *p_ == '\n' || *p_ == '\r') ++p_; }
  bool take(char c) { ws(); if (*p_ != c) return false; ++p_; return true; }
  bool null() { ws(); if (std::strncmp(p_, "null", 4) != 0) return false; p_ += 4; return true; }
  template<size_t N> bool string(char (&out)[N], bool nullable = false) {
    if (nullable && null()) { out[0] = 0; return true; }
    if (!take('"')) return false;
    size_t n = 0;
    while (*p_ != '"') {
      unsigned char c = *p_;
      if (c < 32 || c > 126) return false; // All defined v1 strings are ASCII.
      ++p_;
      if (c == '\\') {
        c = *p_++; if (c == 0) return false;
        if (c == 'u') {
          unsigned value = 0;
          for (int i = 0; i < 4; ++i) {
            const unsigned char digit = *p_;
            int h = digit >= '0' && digit <= '9' ? digit - '0' :
                    digit >= 'a' && digit <= 'f' ? digit - 'a' + 10 :
                    digit >= 'A' && digit <= 'F' ? digit - 'A' + 10 : -1;
            if (h < 0) return false;
            ++p_; value = value * 16 + h;
          }
          if (value < 32 || value > 126) return false;
          c = static_cast<unsigned char>(value);
        } else if (c != '"' && c != '\\' && c != '/') return false;
      }
      if (n + 1 >= N) return false;
      out[n++] = static_cast<char>(c);
    }
    ++p_; out[n] = 0; return n > 0;
  }
  bool number(double &out, double min, double max, bool nullable = false) {
    if (nullable && null()) { out = -1; return true; }
    ws(); const char *start = p_;
    if (*p_ == '-') ++p_;
    if (*p_ == '0') ++p_;
    else { if (*p_ < '1' || *p_ > '9') return false; while (*p_ >= '0' && *p_ <= '9') ++p_; }
    if (*p_ == '.') { ++p_; if (*p_ < '0' || *p_ > '9') return false; while (*p_ >= '0' && *p_ <= '9') ++p_; }
    if (*p_ == 'e' || *p_ == 'E') {
      ++p_; if (*p_ == '+' || *p_ == '-') ++p_;
      if (*p_ < '0' || *p_ > '9') return false;
      while (*p_ >= '0' && *p_ <= '9') ++p_;
    }
    char *end = nullptr; out = std::strtod(start, &end);
    return end == p_ && std::isfinite(out) && out >= min && out <= max;
  }
  bool integer(int64_t &out, int64_t min, int64_t max, bool nullable = false) {
    double value;
    if (!number(value, min, max, nullable) || std::floor(value) != value) return false;
    out = static_cast<int64_t>(value); return true;
  }
  template<size_t N> int key(const char *const (&fields)[N], uint32_t &seen) {
    char name[24]{}; if (!string(name) || !take(':')) return -1;
    for (size_t i = 0; i < N; ++i) if (eq(name, fields[i])) {
      if (seen & (1U << i)) return -1;
      seen |= 1U << i; return static_cast<int>(i);
    }
    return -1;
  }
  bool cycle(Model &m) {
    static const char *const fields[] = {"start_at", "end_at", "state"};
    if (!take('{')) return false;
    uint32_t seen = 0;
    for (int i = 0; i < 3; ++i) {
      int k = key(fields, seen); bool ok = false;
      if (k == 0) ok = integer(m.start, 1, MAX_EPOCH, true);
      if (k == 1) ok = integer(m.end, 1, MAX_EPOCH, true);
      if (k == 2) ok = string(m.cycle_state);
      if (!ok || !take(i == 2 ? '}' : ',')) return false;
    }
    return true;
  }
  bool days(Model &m) {
    static const char *const fields[] = {"start_at", "label", "used_delta_pp", "coverage", "end_at"};
    if (!take('[')) return false;
    if(take(']')) return true;
    for (int d = 0; d < 9; ++d) {
      if (!take('{')) return false;
      uint32_t seen = 0;
      for (int i = 0; i < 5; ++i) {
        int k = key(fields, seen); bool ok = false;
        if (k == 0) ok = integer(m.days[d].start, 1, MAX_EPOCH);
        if (k == 1) ok = string(m.days[d].label);
        if (k == 2) ok = number(m.days[d].delta, 0, 100, true);
        if (k == 3) ok = string(m.days[d].coverage);
        if (k == 4) ok = integer(m.days[d].end, 1, MAX_EPOCH);
        if (!ok || !take(i == 4 ? '}' : ',')) return false;
      }
      ++m.day_count;
      if(take(']')) return true;
      if(!take(',')) return false;
    }
    return false;
  }
};

// The host owns IANA conversion and weekday labels; no timezone DB on the CYD.
// Check the supplied local date/offset against the reset instant independently.
inline bool reset_text_valid(const Model &m) {
  const char *s = m.reset_local;
  if (std::strlen(s) != 23) return false;
  for (int i = 0; i < 23; ++i) {
    char expected = i == 4 || i == 7 ? '-' : i == 10 || i == 16 ? ' ' :
                    i == 13 || i == 20 ? ':' : 0;
    if (expected ? s[i] != expected : i == 17 ? (s[i] != '+' && s[i] != '-') : (s[i] < '0' || s[i] > '9')) return false;
  }
  auto n2 = [s](int i) { return (s[i] - '0') * 10 + s[i + 1] - '0'; };
  int y = n2(0) * 100 + n2(2), mo = n2(5), d = n2(8);
  int hour = n2(11), minute = n2(14), oh = n2(18), om = n2(21);
  static constexpr int lengths[] = {31,28,31,30,31,30,31,31,30,31,30,31};
  bool leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
  if (y < 1969 || y > 2100 || mo < 1 || mo > 12 || d < 1 ||
      d > lengths[mo - 1] + (mo == 2 && leap) || hour > 23 || minute > 59 || oh > 23 || om > 59) return false;
  y -= mo <= 2;
  const int era = y / 400, yoe = y - era * 400;
  int doy = (153 * (mo + (mo > 2 ? -3 : 9)) + 2) / 5 + d - 1;
  int64_t days = era * 146097LL + yoe * 365 + yoe / 4 - yoe / 100 + doy - 719468;
  int64_t epoch = days * DAY + hour * 3600 + minute * 60 - (s[17] == '+' ? 1 : -1) * (oh * 3600 + om * 60);
  return epoch == m.reset - m.reset % 60;
}
inline bool semantics(const Model &m) {
  if (m.resets_expire >= 0 && (m.resets <= 0 || m.observed < 0 || m.resets_expire <= m.observed)) return false;
  static const char *const errors[] = {"", "auth_missing", "auth_failed", "source_timeout", "source_unavailable", "source_invalid"};
  static const char *const cycles[] = {"unknown", "observed", "confirmed", "ambiguous"};
  static const char *const labels[] = {"M", "T", "W", "Th", "F", "Sa", "Su"};
  static const char *const coverages[] = {"unknown", "partial", "complete", "future"};
  if (!one_of(m.source_error, errors) || !one_of(m.cycle_state, cycles)) return false;
  for (const char *p = m.timezone; *p; ++p)
    if (!((*p >= 'A' && *p <= 'Z') || (*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '/' || *p == '_' || *p == '+' || *p == '-')) return false;
  if (m.observed < 0) {
    if (m.scope[0] || m.remaining != -1 || m.reset != -1 || m.reset_local[0] || m.resets != -1 ||
        m.start != -1 || m.end != -1 || !eq(m.cycle_state, "unknown") || m.age != -1 ||
        !eq(m.status, "unavailable") || !eq(m.reason, "no_observation") ||
        ((m.updated < 0) != (m.source_error[0] == 0))) return false;
    if (m.day_count != 0) return false;
    return true;
  }
  if (std::strlen(m.scope) != 64) return false;
  for (char c : m.scope) if (c && !((c >= 'a' && c <= 'f') || (c >= '0' && c <= '9'))) return false;
  if (m.remaining < 0 || m.updated < m.observed || m.reset < 0 || m.start < 1 || m.end != m.reset ||
      m.end - m.start != 7 * DAY || m.observed < m.start - 5 || m.observed >= m.end ||
      eq(m.cycle_state, "unknown") || m.day_count<7 || m.day_count>9 || !reset_text_valid(m)) return false;
  int64_t age = std::max<int64_t>(0, m.as_of - m.observed);
  const char *reason = m.as_of + 5 < std::max(m.observed, m.updated) ? "clock_error" :
                       m.source_error[0] ? "source_error" : m.as_of >= m.reset ? "reset_due" :
                       age >= m.stale_after ? "too_old" : "";
  if (m.age != age || !eq(m.reason, reason) || !eq(m.status, *reason ? "stale" : "ok")) return false;
  int64_t effective = std::max(m.as_of, std::max(m.observed, m.updated)); double total = 0;
  for (int i = 0; i < m.day_count; ++i) {
    const auto &d = m.days[i];
    if (d.start != (i ? m.days[i-1].end : m.start) || d.end<=d.start || d.end>m.end || (i==m.day_count-1 && d.end!=m.end) || !one_of(d.label, labels) || !one_of(d.coverage, coverages)) return false;
    if (d.start > effective) {
      if (!eq(d.coverage, "future") || d.delta != 0) return false;
    } else if (eq(m.cycle_state, "ambiguous")) {
      if (!eq(d.coverage, "unknown") || d.delta != -1) return false;
    } else if (eq(d.coverage, "future") || (eq(d.coverage, "unknown") != (d.delta == -1)) ||
               (eq(d.coverage, "complete") && d.end > effective)) return false;
    if (d.delta >= 0) total += d.delta;
  }
  return total <= 100;
}
inline bool decode(const char *raw, size_t size, Model &out) {
  if (size == 0 || size > MAX_BODY || std::memchr(raw, 0, size)) return false;
  // Caller provides a separate trailing NUL. The model is replaced only on success.
  if (raw[size] != 0) return false;
  Model candidate;
  if (!Decoder(raw).decode(candidate) || !semantics(candidate)) return false;
  out = candidate; return true;
}

inline bool same_facts(const Model &a, const Model &b) {
  if (a.remaining != b.remaining || a.reset != b.reset || a.resets != b.resets || a.resets_expire != b.resets_expire ||
      a.day_count != b.day_count || a.start != b.start || a.end != b.end || a.stale_after != b.stale_after ||
      !eq(a.source_error, b.source_error) || !eq(a.cycle_state, b.cycle_state) ||
      !eq(a.timezone, b.timezone) || !eq(a.reset_local, b.reset_local)) return false;
  // Only future -> unknown is an API projection at equal source/publication times.
  for (int i = 0; i < a.day_count; ++i) {
    const auto &x = a.days[i]; const auto &y = b.days[i];
    if (x.start != y.start || x.end != y.end || !eq(x.label, y.label)) return false;
    bool projection = eq(x.coverage, "future") && eq(y.coverage, "unknown") && y.delta == -1;
    if (!projection && (!eq(x.coverage, y.coverage) || x.delta != y.delta)) return false;
  }
  return true;
}
struct State {
  Model model;
  bool accepted = false, transport_failed = true;
  uint64_t accepted_ms = 0, age_base_ms = 0, as_of_base_ms = 0;
  uint64_t age(uint64_t now) const { return (age_base_ms + now - accepted_ms) / 1000; }
  uint64_t as_of(uint64_t now) const { return (as_of_base_ms + now - accepted_ms) / 1000; }
  const char *status(uint64_t now) const {
    if (!accepted || model.observed < 0) return "unavailable";
    if (transport_failed || eq(model.status, "stale") || age(now) >= static_cast<uint64_t>(model.stale_after) ||
        as_of(now) >= static_cast<uint64_t>(model.reset)) return "stale";
    return "ok";
  }
  bool accept(const Model &next, uint64_t now) {
    bool same_scope = accepted && eq(model.scope, next.scope);
    if (accepted && model.observed >= 0 && next.observed < 0) { transport_failed = true; return false; }
    if (same_scope) {
      if (next.observed < model.observed || next.updated < model.updated) { transport_failed = true; return false; }
      if (next.observed == model.observed && next.updated == model.updated && !same_facts(model, next)) {
        transport_failed = true; return false;
      }
    }
    uint64_t next_age = next.age < 0 ? 0 : static_cast<uint64_t>(next.age) * 1000;
    uint64_t next_as_of = static_cast<uint64_t>(next.as_of) * 1000;
    if (same_scope && next.observed == model.observed) {
      next_age = std::max(next_age, age_base_ms + now - accepted_ms);
      next_as_of = std::max(next_as_of, as_of_base_ms + now - accepted_ms);
    }
    model = next; accepted = true; transport_failed = false;
    age_base_ms = next_age; as_of_base_ms = next_as_of; accepted_ms = now; return true;
  }
  Day day(size_t i, uint64_t now) const {
    Day result = model.days[i];
    if (eq(result.coverage, "future") && result.start <= static_cast<int64_t>(as_of(now))) {
      std::strcpy(result.coverage, "unknown"); result.delta = -1;
    }
    return result;
  }
};
} // namespace meter
