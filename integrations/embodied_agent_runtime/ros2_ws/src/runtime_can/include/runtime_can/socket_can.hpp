#pragma once

#include "runtime_can/protocol.hpp"

#include <chrono>
#include <cstdint>
#include <string>

namespace runtime_can
{

enum class ReceiveStatus
{
  kFrame,
  kTimeout,
  kError
};

class SocketCan
{
public:
  SocketCan() = default;
  ~SocketCan();

  SocketCan(const SocketCan &) = delete;
  SocketCan & operator=(const SocketCan &) = delete;

  bool open(const std::string & interface_name, std::string & error);
  bool set_filter(std::uint32_t frame_id, std::string & error);
  bool send(const RawFrame & frame, std::string & error) const;
  ReceiveStatus receive(
    RawFrame & frame, std::chrono::milliseconds timeout, std::string & error) const;
  void close();
  bool is_open() const;

private:
  int socket_fd_{-1};
};

}
