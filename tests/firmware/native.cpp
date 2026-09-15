#include "firmware/components/meter_network/model.h"
#include "firmware/components/meter_network/http.h"
#include <cassert>
#include <fstream>
#include <iostream>
#include <iterator>
#include <chrono>
#include <string>

static meter::Model read_model(const char *path) {
  std::ifstream input(path, std::ios::binary);
  std::string raw((std::istreambuf_iterator<char>(input)), {});
  meter::Model m; assert(meter::decode(raw.c_str(), raw.size(), m)); return m;
}
int main(int argc, char **argv) {
  if (argc == 6 && std::string(argv[1]) == "http") {
    meter::Http http;
    bool ok = http.get(argv[2], std::atoi(argv[3]), argv[4], std::atoi(argv[5]));
    std::cout << (ok ? "valid" : http.error) << '\n'; return 0;
  }
  if (argc == 3 && std::string(argv[1]) == "fuzz") {
    std::ifstream input(argv[2], std::ios::binary);
    std::string raw((std::istreambuf_iterator<char>(input)), {});
    while(!raw.empty() && (raw.back()=='\n'||raw.back()=='\r'||raw.back()==' ')) raw.pop_back();
    meter::Model model;
    size_t cases = 0;
    for (size_t n = 0; n < raw.size(); ++n) {
      auto truncated = raw.substr(0, n); model.remaining = 42;
      assert(!meter::decode(truncated.c_str(), truncated.size(), model)); assert(model.remaining == 42); ++cases;
      for (char c : {'\\', '"', '[', '}', '\0', static_cast<char>(0xff)}) {
        auto mutated = raw; mutated[n] = c;
        meter::decode(mutated.c_str(), mutated.size(), model); ++cases;
      }
    }
    auto start = std::chrono::steady_clock::now();
    for (int i = 0; i < 10000; ++i) assert(meter::decode(raw.c_str(), raw.size(), model));
    auto elapsed = std::chrono::duration_cast<std::chrono::microseconds>(std::chrono::steady_clock::now() - start).count();
    std::cout << "decoder memory-safety cases=" << cases << " sanitized_mean_us=" << elapsed / 10000.0 << '\n';
    return 0;
  }
  if (argc == 3 && std::string(argv[1]) == "state") {
    auto m = read_model(argv[2]); meter::State state;
    assert(std::string(state.status(0)) == "unavailable");
    assert(state.accept(m, 1000)); assert(std::string(state.status(1000)) == "ok");
    auto age = state.age(11000); assert(state.accept(m, 11000)); assert(state.age(11000) == age);
    state.transport_failed = true; assert(std::string(state.status(11000)) == "stale");
    assert(state.accept(m, 12000)); assert(!state.transport_failed);
    assert(state.age(181000) >= 180); assert(std::string(state.status(181000)) == "stale");
    meter::State fractional;
    assert(fractional.accept(m, 0));
    for (uint64_t t = 900; t <= 9000; t += 900) assert(fractional.accept(m, t));
    assert(fractional.age(9000) == static_cast<uint64_t>(m.age) + 9);
    auto conflict = m; conflict.remaining -= 1; assert(!state.accept(conflict, 182000));
    assert(state.model.remaining == m.remaining && state.transport_failed);
    auto old = m; --old.observed; assert(!state.accept(old, 183000));
    old = m; --old.updated; assert(!state.accept(old, 183000));
    auto fresh = m; fresh.observed += 200; fresh.updated += 200; fresh.as_of += 200;
    assert(state.accept(fresh, 201000)); assert(state.age(201000) == static_cast<uint64_t>(fresh.age));
    assert(std::string(state.status(201000)) == "ok");
    auto other = m; other.scope[0] = other.scope[0] == 'a' ? 'b' : 'a';
    assert(state.accept(other, 202000)); assert(meter::eq(state.model.scope, other.scope));
    meter::Model unavailable; assert(!state.accept(unavailable, 203000));
    assert(state.model.observed == other.observed);
    uint64_t much_later = 202000ULL + (1ULL << 32) + 2000;
    assert(state.age(much_later) >= (1ULL << 32) / 1000);
    assert(std::string(state.status(much_later)) == "stale");
    for (size_t i = 0; i < state.model.day_count; ++i) assert(!meter::eq(state.day(i, much_later).coverage, "future"));
    std::cout << "state checks passed; model_bytes=" << sizeof(meter::Model) << " state_bytes=" << sizeof(meter::State) << '\n';
    return 0;
  }
  if (argc == 2) {
    std::ifstream input(argv[1], std::ios::binary);
    std::string raw((std::istreambuf_iterator<char>(input)), {});
    meter::Model m; std::cout << (meter::decode(raw.c_str(), raw.size(), m) ? "valid" : "invalid") << '\n';
    return 0;
  }
  return 2;
}
