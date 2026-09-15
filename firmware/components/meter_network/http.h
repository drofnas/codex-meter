#pragma once
#include "model.h"
#include <cerrno>
#include <cstdio>
#include <fcntl.h>
#include <sys/socket.h>
#include <sys/select.h>
#include <arpa/inet.h>
#include <unistd.h>
#ifdef ESP_PLATFORM
#include "esp_timer.h"
#else
#include <chrono>
#endif

namespace meter {
inline uint64_t monotonic_ms() {
#ifdef ESP_PLATFORM
  return esp_timer_get_time() / 1000;
#else
  return std::chrono::duration_cast<std::chrono::milliseconds>(
      std::chrono::steady_clock::now().time_since_epoch()).count();
#endif
}
// Fixed working space, no redirects, DNS, decompression or persistent connections.
// Numeric LAN addresses avoid an unbounded DNS operation in the request deadline.
class Http {
 public:
  char body[MAX_BODY + 1]{};
  size_t size = 0;
  const char *error = "transport";
  ~Http() { if (fd_ >= 0) ::close(fd_); }
  bool get(const char *host, uint16_t port, const char *token, uint32_t timeout_ms) {
    deadline_ = monotonic_ms() + timeout_ms;
    fd_ = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (fd_ < 0 || ::fcntl(fd_, F_SETFL, O_NONBLOCK) < 0) return false;
    sockaddr_in address{}; address.sin_family = AF_INET; address.sin_port = htons(port);
    if (::inet_pton(AF_INET, host, &address.sin_addr) != 1) return false;
    if (::connect(fd_, reinterpret_cast<sockaddr *>(&address), sizeof(address)) < 0 && errno != EINPROGRESS) return false;
    if (!wait(true)) return false;
    int e = 0; socklen_t length = sizeof(e);
    if (::getsockopt(fd_, SOL_SOCKET, SO_ERROR, &e, &length) < 0 || e) return false;
    char request[320];
    int count = std::snprintf(request, sizeof(request),
      "GET /v2/usage HTTP/1.1\r\nHost: %s:%u\r\nAuthorization: Bearer %s\r\n"
      "Accept: application/json\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n", host, port, token);
    if (count < 0 || static_cast<size_t>(count) >= sizeof(request)) return false;
    for (int written = 0; written < count;) {
      if (!wait(true)) return false;
#ifdef MSG_NOSIGNAL
      constexpr int flags = MSG_NOSIGNAL;
#else
      constexpr int flags = 0;
#endif
      int n = ::send(fd_, request + written, count - written, flags);
      if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
      if (n <= 0) return false;
      written += n;
    }
    char line[512]; size_t headers = 0;
    if (!read_line(line, headers, 2048)) return false;
    if (std::strncmp(line, "HTTP/1.1 200 ", 13) != 0 && std::strncmp(line, "HTTP/1.0 200 ", 13) != 0) {
      error = "http_status"; return false;
    }
    int content_length = -1;
    bool chunked = false, has_type = false, has_encoding = false;
    for (;;) {
      if (!read_line(line, headers, 2048)) return false;
      if (!line[0]) break;
      char *colon = std::strchr(line, ':');
      if (!colon || colon == line) { error = "headers"; return false; }
      *colon++ = 0;
      for (char *p = line; *p; ++p) {
        if (*p >= 'A' && *p <= 'Z') *p += 'a' - 'A';
        if (!((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '-')) { error = "headers"; return false; }
      }
      while (*colon == ' ' || *colon == '\t') ++colon;
      char *last = colon + std::strlen(colon);
      while (last > colon && (last[-1] == ' ' || last[-1] == '\t')) *--last = 0;
      if (eq(line, "content-length")) {
        if (content_length >= 0 || !*colon) { error = "headers"; return false; }
        content_length = 0;
        for (const char *p = colon; *p; ++p) {
          if (*p < '0' || *p > '9') { error = "headers"; return false; }
          content_length = content_length * 10 + *p - '0';
          if (content_length > static_cast<int>(MAX_BODY)) { error = "oversized"; return false; }
        }
      } else if (eq(line, "transfer-encoding")) {
        if (chunked || !eq(colon, "chunked")) { error = "headers"; return false; }
        chunked = true;
      } else if (eq(line, "content-type")) {
        if (has_type || (std::strncmp(colon, "application/json", 16) != 0) || (colon[16] && colon[16] != ';')) {
          error = "content_type"; return false;
        }
        has_type = true;
      } else if (eq(line, "content-encoding")) {
        if (has_encoding || !eq(colon, "identity")) { error = "encoding"; return false; }
        has_encoding = true;
      }
    }
    if (!has_type || (chunked == (content_length >= 0))) { error = "headers"; return false; }
    if (!chunked) {
      if (!read_body(content_length)) return false;
    } else {
      for (;;) {
        size_t chunk_headers = 0;
        if (!read_line(line, chunk_headers, 128)) return false;
        // Chunk extensions are permitted, bounded, and ignored.
        size_t chunk = 0; const char *p = line; int digits = 0;
        while (*p && *p != ';') {
          int v = *p >= '0' && *p <= '9' ? *p - '0' : *p >= 'a' && *p <= 'f' ? *p - 'a' + 10 :
                  *p >= 'A' && *p <= 'F' ? *p - 'A' + 10 : -1;
          if (v < 0 || ++digits > 8) { error = "chunked"; return false; }
          chunk = chunk * 16 + v; ++p;
          if (chunk > MAX_BODY - size) { error = "oversized"; return false; }
        }
        if (!digits) { error = "chunked"; return false; }
        if (!chunk) {
          size_t trailers = 0;
          do { if (!read_line(line, trailers, 1024)) return false; } while (line[0]);
          break;
        }
        if (!read_body(chunk)) return false;
        int a = byte(), b = byte();
        if (a != '\r' || b != '\n') { error = "truncated"; return false; }
      }
    }
    body[size] = 0;
    if (monotonic_ms() >= deadline_) { error = "timeout"; return false; }
    error = "none"; return true;
  }
 private:
  int fd_ = -1;
  uint64_t deadline_ = 0;
  char buffer_[512]; size_t position_ = 0, available_ = 0;
  bool wait(bool write) {
    for (;;) {
      uint64_t now = monotonic_ms();
      if (now >= deadline_) { error = "timeout"; return false; }
      uint64_t left = deadline_ - now;
      timeval tv{static_cast<long>(left / 1000), static_cast<int>((left % 1000) * 1000)};
      fd_set fds; FD_ZERO(&fds); FD_SET(fd_, &fds);
      int r = ::select(fd_ + 1, write ? nullptr : &fds, write ? &fds : nullptr, nullptr, &tv);
      if (r < 0 && errno == EINTR) continue;
      if (r == 0) error = "timeout";
      return r > 0;
    }
  }
  int byte() {
    if (monotonic_ms() >= deadline_) { error = "timeout"; return -1; }
    while (position_ == available_) {
      if (!wait(false)) return -1;
      int n = ::recv(fd_, buffer_, sizeof(buffer_), 0);
      if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) continue;
      if (n <= 0) { error = "truncated"; return -1; }
      available_ = n; position_ = 0;
    }
    return static_cast<unsigned char>(buffer_[position_++]);
  }
  bool read_line(char (&out)[512], size_t &total, size_t cap) {
    size_t n = 0;
    for (;;) {
      int c = byte(); if (c < 0) return false;
      if (++total > cap) { error = "headers"; return false; }
      if (c == '\r') {
        if (byte() != '\n' || ++total > cap) { error = "headers"; return false; }
        out[n] = 0; return true;
      }
      if ((c < 32 && c != '\t') || c > 126 || n + 1 >= sizeof(out)) { error = "headers"; return false; }
      out[n++] = static_cast<char>(c);
    }
  }
  bool read_body(size_t count) {
    if (count > MAX_BODY - size) { error = "oversized"; return false; }
    for (size_t i = 0; i < count; ++i) {
      int c = byte(); if (c < 0) return false;
      body[size++] = static_cast<char>(c);
    }
    return true;
  }
};
} // namespace meter
