// Host harness for the T-Embed's on-board decoder
// (esphome/components/insteon_rf/decoder.cpp). Built by `make` and by
// tests/test_esphome_decoder.py, which compares its output with the Python
// reference (insteonrf.listener) on tests/data/contract-v1/*-vectors.jsonl.
//
// Input, one capture per line (blank lines ignored):
//
//   default:  <sync word hex> <base64 FIFO bytes>
//             -> one JSON array per line, capture_messages() minus the fields
//                this receiver lacks:
//                [{"frame":"0250...","copies":3,"hops_left":2}, ...]
//
//   --fold:   <t ms> <rssi dBm> <sync word hex> <base64 FIFO bytes>
//             -> the cross-capture fold the firmware runs: each copy dated at
//                its own header (t + bit offset / 9124 baud), ready messages
//                released before each capture on its own clock, the rest
//                flushed at the end; one JSON object per released message:
//                {"t_ms":..,"frame":"..","copies":N,"hops_left":H,"rssi":R}
//
// Exit status 2 on a malformed line.

#include "../esphome/components/insteon_rf/decoder.h"

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

using esphome::insteon_rf::CaptureDecoder;
using esphome::insteon_rf::DECODER_BITRATE;
using esphome::insteon_rf::DECODER_MAX_COPIES;
using esphome::insteon_rf::DecodedMessage;
using esphome::insteon_rf::FoldedMessage;
using esphome::insteon_rf::frame_hex;
using esphome::insteon_rf::MessageFolder;

namespace {

int b64_value(char c) {
  if (c >= 'A' && c <= 'Z')
    return c - 'A';
  if (c >= 'a' && c <= 'z')
    return c - 'a' + 26;
  if (c >= '0' && c <= '9')
    return c - '0' + 52;
  if (c == '+')
    return 62;
  if (c == '/')
    return 63;
  return -1;
}

bool b64_decode(const std::string &in, std::vector<uint8_t> *out) {
  out->clear();
  uint32_t acc = 0;
  int nbits = 0;
  for (char c : in) {
    if (c == '=')
      break;
    const int v = b64_value(c);
    if (v < 0)
      return false;
    acc = (acc << 6) | (uint32_t) v;
    nbits += 6;
    if (nbits >= 8) {
      nbits -= 8;
      out->push_back((uint8_t) ((acc >> nbits) & 0xFF));
    }
  }
  return true;
}

void print_folded(const FoldedMessage &f) {
  char hex[2 * esphome::insteon_rf::PLM_EXT_FRAME_LEN + 1];
  frame_hex(f.msg, hex);
  std::printf("{\"t_ms\":%lld,\"frame\":\"%s\",\"copies\":%u,\"hops_left\":%u,\"rssi\":", (long long) f.t_ms,
              hex, (unsigned) f.msg.copies, (unsigned) f.msg.hops_left);
  if (std::isnan(f.rssi))
    std::printf("null}\n");
  else
    std::printf("%.1f}\n", (double) f.rssi);
}

}  // namespace

int main(int argc, char **argv) {
  const bool fold = argc > 1 && std::strcmp(argv[1], "--fold") == 0;
  static CaptureDecoder decoder;  // ~2 KB of bit buffer: not on the stack
  MessageFolder folder;
  char line[4096];
  std::vector<uint8_t> bytes;
  unsigned lineno = 0;

  while (std::fgets(line, sizeof(line), stdin) != nullptr) {
    lineno++;
    char *save = nullptr;
    std::vector<const char *> tok;
    for (char *t = strtok_r(line, " \t\r\n", &save); t != nullptr; t = strtok_r(nullptr, " \t\r\n", &save))
      tok.push_back(t);
    if (tok.empty())
      continue;
    const size_t want = fold ? 4 : 2;
    if (tok.size() != want || !b64_decode(tok[want - 1], &bytes)) {
      std::fprintf(stderr, "line %u: malformed\n", lineno);
      return 2;
    }
    const uint32_t sync_word = (uint32_t) std::strtoul(tok[want - 2], nullptr, 16);

    if (!fold) {
      DecodedMessage msgs[DECODER_MAX_COPIES];
      const size_t n = decoder.decode(bytes.data(), bytes.size(), sync_word, msgs, DECODER_MAX_COPIES);
      std::printf("[");
      for (size_t i = 0; i < n; i++) {
        char hex[2 * esphome::insteon_rf::PLM_EXT_FRAME_LEN + 1];
        frame_hex(msgs[i], hex);
        std::printf("%s{\"frame\":\"%s\",\"copies\":%u,\"hops_left\":%u}", i ? "," : "", hex,
                    (unsigned) msgs[i].copies, (unsigned) msgs[i].hops_left);
      }
      std::printf("]\n");
      continue;
    }

    const int64_t t_ms = std::strtoll(tok[0], nullptr, 10);
    const float rssi = std::strcmp(tok[1], "null") == 0 ? NAN : std::strtof(tok[1], nullptr);
    FoldedMessage f;
    while (folder.pop_ready(t_ms, &f))
      print_folded(f);
    DecodedMessage copies[DECODER_MAX_COPIES];
    const size_t n = decoder.decode_copies(bytes.data(), bytes.size(), sync_word, copies, DECODER_MAX_COPIES);
    for (size_t i = 0; i < n; i++) {
      const int64_t t = t_ms + ((int64_t) copies[i].bit_pos * 1000 + DECODER_BITRATE / 2) / DECODER_BITRATE;
      if (folder.add(copies[i], t, rssi, &f))
        print_folded(f);
    }
  }
  if (fold) {
    FoldedMessage f;
    while (folder.pop_ready(INT64_MAX, &f))
      print_folded(f);
  }
  return 0;
}
