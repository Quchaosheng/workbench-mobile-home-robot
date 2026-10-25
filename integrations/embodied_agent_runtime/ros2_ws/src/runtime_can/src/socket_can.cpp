#include "runtime_can/socket_can.hpp"

#include <algorithm>
#include <cerrno>
#include <cstring>

#include <linux/can.h>
#include <linux/can/raw.h>
#include <net/if.h>
#include <poll.h>
#include <sys/ioctl.h>
#include <sys/socket.h>
#include <unistd.h>

namespace runtime_can
{

SocketCan::~SocketCan()
{
  close();
}

bool SocketCan::open(const std::string & interface_name, std::string & error)
{
  close();
  if (interface_name.empty() || interface_name.size() >= IFNAMSIZ) {
    error = "invalid CAN interface name";
    return false;
  }

  socket_fd_ = ::socket(PF_CAN, SOCK_RAW, CAN_RAW);
  if (socket_fd_ < 0) {
    error = std::strerror(errno);
    return false;
  }

  ifreq interface_request{};
  std::memcpy(interface_request.ifr_name, interface_name.c_str(), interface_name.size() + 1);
  if (::ioctl(socket_fd_, SIOCGIFINDEX, &interface_request) < 0) {
    error = std::strerror(errno);
    close();
    return false;
  }

  sockaddr_can address{};
  address.can_family = AF_CAN;
  address.can_ifindex = interface_request.ifr_ifindex;
  if (::bind(socket_fd_, reinterpret_cast<sockaddr *>(&address), sizeof(address)) < 0) {
    error = std::strerror(errno);
    close();
    return false;
  }
  return true;
}

bool SocketCan::set_filter(std::uint32_t frame_id, std::string & error)
{
  can_filter filter{};
  filter.can_id = frame_id;
  filter.can_mask = CAN_SFF_MASK;
  if (::setsockopt(socket_fd_, SOL_CAN_RAW, CAN_RAW_FILTER, &filter, sizeof(filter)) < 0) {
    error = std::strerror(errno);
    return false;
  }
  return true;
}

bool SocketCan::send(const RawFrame & frame, std::string & error) const
{
  if (!is_open()) {
    error = "CAN socket is not open";
    return false;
  }
  if (frame.size > kPayloadSize) {
    error = "CAN frame payload is too large";
    return false;
  }

  can_frame native_frame{};
  native_frame.can_id = frame.id & CAN_SFF_MASK;
  native_frame.can_dlc = frame.size;
  std::copy_n(frame.data.begin(), frame.size, native_frame.data);
  const auto bytes_written = ::write(socket_fd_, &native_frame, sizeof(native_frame));
  if (bytes_written != static_cast<ssize_t>(sizeof(native_frame))) {
    error = std::strerror(errno);
    return false;
  }
  return true;
}

ReceiveStatus SocketCan::receive(
  RawFrame & frame, std::chrono::milliseconds timeout, std::string & error) const
{
  if (!is_open()) {
    error = "CAN socket is not open";
    return ReceiveStatus::kError;
  }

  pollfd descriptor{socket_fd_, POLLIN, 0};
  int poll_result;
  do {
    poll_result = ::poll(&descriptor, 1, static_cast<int>(timeout.count()));
  } while (poll_result < 0 && errno == EINTR);

  if (poll_result == 0) {
    return ReceiveStatus::kTimeout;
  }
  if (poll_result < 0) {
    error = std::strerror(errno);
    return ReceiveStatus::kError;
  }

  can_frame native_frame{};
  const auto bytes_read = ::read(socket_fd_, &native_frame, sizeof(native_frame));
  if (bytes_read != static_cast<ssize_t>(sizeof(native_frame))) {
    error = bytes_read < 0 ? std::strerror(errno) : "short CAN frame read";
    return ReceiveStatus::kError;
  }
  if ((native_frame.can_id & (CAN_EFF_FLAG | CAN_RTR_FLAG | CAN_ERR_FLAG)) != 0) {
    error = "unsupported extended, remote, or error CAN frame";
    return ReceiveStatus::kError;
  }
  if (native_frame.can_dlc > kPayloadSize) {
    error = "CAN frame payload is too large";
    return ReceiveStatus::kError;
  }

  frame.id = native_frame.can_id & CAN_SFF_MASK;
  frame.size = native_frame.can_dlc;
  frame.data.fill(0);
  std::copy_n(native_frame.data, frame.size, frame.data.begin());
  return ReceiveStatus::kFrame;
}

void SocketCan::close()
{
  if (socket_fd_ >= 0) {
    ::close(socket_fd_);
    socket_fd_ = -1;
  }
}

bool SocketCan::is_open() const
{
  return socket_fd_ >= 0;
}

}
