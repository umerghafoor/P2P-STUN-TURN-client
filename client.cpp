// P2P WebRTC client (C++ / libdatachannel).
//
// libdatachannel is a lightweight, standalone WebRTC implementation
// (https://github.com/paullouisageneau/libdatachannel). It exposes the same
// primitives a "native WebRTC client" needs:
//
//   - PeerConnection (analogous to PeerConnectionFactory + PeerConnection)
//   - rtc::Configuration with ICE servers (STUN/TURN)
//   - DataChannel
//   - Observer-style callbacks (onLocalDescription, onLocalCandidate,
//     onStateChange, onGatheringStateChange, onDataChannel, etc.)
//   - Offer/Answer hooks (createDataChannel triggers an offer; setRemoteDescription
//     of an offer triggers an answer automatically)
//   - External signaling: this file uses stdin/stdout JSON, so it interoperates
//     with client.py and index.html.
//
// We pick libdatachannel rather than Google's libwebrtc because libwebrtc is
// multi-gigabyte and takes hours to build; libdatachannel is a few-hour build
// at worst and a single header to include.
//
// Build (after libdatachannel is installed system-wide or via the bundled CMake):
//   cmake -B build && cmake --build build -j
//   ./build/p2p_client offer
//   ./build/p2p_client answer

#include <rtc/rtc.hpp>

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdio>
#include <cstdlib>
#include <ctime>
#include <iomanip>
#include <iostream>
#include <memory>
#include <mutex>
#include <sstream>
#include <string>
#include <thread>
#include <variant>

namespace {

// ----- tiny logger -----
std::mutex g_log_mu;
void logmsg(const std::string& level, const std::string& cat, const std::string& msg) {
    std::lock_guard<std::mutex> lock(g_log_mu);
    auto now = std::chrono::system_clock::now();
    auto t  = std::chrono::system_clock::to_time_t(now);
    auto ms = std::chrono::duration_cast<std::chrono::milliseconds>(now.time_since_epoch()) % 1000;
    char buf[16];
    std::strftime(buf, sizeof(buf), "%H:%M:%S", std::localtime(&t));
    std::cerr << buf << "." << std::setfill('0') << std::setw(3) << ms.count() << " "
              << level << "  [" << cat << "] " << msg << "\n";
}

// ----- minimal JSON helpers (we only encode/decode {"type":"...","sdp":"..."}) -----
std::string escape_json(const std::string& s) {
    std::string out;
    out.reserve(s.size() + 16);
    for (char c : s) {
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\b': out += "\\b";  break;
            case '\f': out += "\\f";  break;
            case '\n': out += "\\n";  break;
            case '\r': out += "\\r";  break;
            case '\t': out += "\\t";  break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char tmp[8]; std::snprintf(tmp, sizeof(tmp), "\\u%04x", c);
                    out += tmp;
                } else out += c;
        }
    }
    return out;
}
std::string unescape_json_str(const std::string& s) {
    std::string out; out.reserve(s.size());
    for (std::size_t i = 0; i < s.size(); ++i) {
        if (s[i] == '\\' && i + 1 < s.size()) {
            char n = s[++i];
            switch (n) {
                case '"':  out += '"';  break;
                case '\\': out += '\\'; break;
                case '/':  out += '/';  break;
                case 'b':  out += '\b'; break;
                case 'f':  out += '\f'; break;
                case 'n':  out += '\n'; break;
                case 'r':  out += '\r'; break;
                case 't':  out += '\t'; break;
                case 'u':  if (i + 4 < s.size()) i += 4; break;  // skip — SDP uses ASCII
                default:   out += n; break;
            }
        } else out += s[i];
    }
    return out;
}
std::string extract_field(const std::string& json, const std::string& key) {
    std::string needle = "\"" + key + "\"";
    auto k = json.find(needle);
    if (k == std::string::npos) return {};
    auto colon = json.find(':', k + needle.size());
    if (colon == std::string::npos) return {};
    auto q1 = json.find('"', colon + 1);
    if (q1 == std::string::npos) return {};
    std::string val;
    for (std::size_t i = q1 + 1; i < json.size(); ++i) {
        if (json[i] == '\\' && i + 1 < json.size()) { val += json[i]; val += json[i + 1]; ++i; }
        else if (json[i] == '"') return unescape_json_str(val);
        else val += json[i];
    }
    return {};
}

// ----- SDP printing -----
void print_local_sdp(const std::string& type, const std::string& sdp) {
    std::cout << "\n===== LOCAL SDP (copy the line below to the peer) =====\n";
    std::cout << "{\"type\":\"" << escape_json(type) << "\",\"sdp\":\"" << escape_json(sdp) << "\"}\n";
    std::cout << "===== END LOCAL SDP =====\n" << std::flush;
}

std::string read_remote_sdp_json() {
    std::cerr << "\nPaste remote SDP JSON (single line) and press ENTER:\n";
    std::string line;
    std::getline(std::cin, line);
    return line;
}

// ----- shared state for waiting on connection lifecycle -----
std::mutex g_done_mu;
std::condition_variable g_done_cv;
std::atomic<bool> g_done{false};

// stdin -> data-channel send pump
void stdin_send_pump(std::shared_ptr<rtc::DataChannel> dc) {
    std::string line;
    while (!g_done.load() && std::getline(std::cin, line)) {
        if (line.empty()) continue;
        if (!dc->isOpen()) { logmsg("ERR ", "dc", "send dropped — channel not open"); continue; }
        dc->send(line);
        logmsg("SEND", "dc", line);
    }
}

void wire_data_channel(std::shared_ptr<rtc::DataChannel> dc) {
    logmsg("INFO", "dc", "attached label=" + dc->label());

    dc->onOpen([dc]() {
        logmsg("OK  ", "dc", "OPEN — type messages and press ENTER to send");
        std::thread(stdin_send_pump, dc).detach();
    });
    dc->onClosed([]() { logmsg("WARN", "dc", "CLOSED"); });
    dc->onError([](std::string e) { logmsg("ERR ", "dc", "error: " + e); });
    dc->onMessage([](rtc::message_variant msg) {
        if (std::holds_alternative<std::string>(msg)) {
            logmsg("RECV", "dc", std::get<std::string>(msg));
        } else {
            auto& bin = std::get<rtc::binary>(msg);
            logmsg("RECV", "dc", "(" + std::to_string(bin.size()) + " bytes binary)");
        }
    });
}

void wire_peer_connection(std::shared_ptr<rtc::PeerConnection> pc) {
    pc->onStateChange([](rtc::PeerConnection::State s) {
        std::ostringstream o; o << s;
        logmsg("INFO", "pc", "state -> " + o.str());
        if (s == rtc::PeerConnection::State::Failed ||
            s == rtc::PeerConnection::State::Closed ||
            s == rtc::PeerConnection::State::Disconnected) {
            { std::lock_guard<std::mutex> lk(g_done_mu); g_done = true; }
            g_done_cv.notify_all();
        }
    });
    pc->onGatheringStateChange([](rtc::PeerConnection::GatheringState s) {
        std::ostringstream o; o << s;
        logmsg("INFO", "pc", "gatheringState -> " + o.str());
    });
    pc->onSignalingStateChange([](rtc::PeerConnection::SignalingState s) {
        std::ostringstream o; o << s;
        logmsg("INFO", "pc", "signalingState -> " + o.str());
    });
    pc->onLocalCandidate([](rtc::Candidate c) {
        std::ostringstream o; o << c;
        logmsg("DBG ", "ice", "local candidate: " + o.str());
    });
    pc->onLocalDescription([](rtc::Description desc) {
        // libdatachannel emits the local description AFTER ICE gathering completes
        // (when iceUdpMux=true / default), so this is the right moment to print it.
        std::string type = desc.typeString();
        std::string sdp  = std::string(desc);
        logmsg("INFO", "sdp", "local description ready (type=" + type + ")");
        print_local_sdp(type, sdp);
    });
}

rtc::Configuration build_config(int argc, char** argv) {
    rtc::Configuration c;
    std::string stun = "stun:stun.l.google.com:19302";
    std::string turn, turn_user, turn_pass;
    for (int i = 2; i < argc; ++i) {
        std::string a = argv[i];
        auto take = [&](const std::string& flag, std::string& out) {
            if (a == flag && i + 1 < argc) { out = argv[++i]; return true; }
            return false;
        };
        if (take("--stun", stun)) continue;
        if (take("--turn", turn)) continue;
        if (take("--turn-user", turn_user)) continue;
        if (take("--turn-pass", turn_pass)) continue;
    }
    if (!stun.empty()) c.iceServers.emplace_back(stun);
    if (!turn.empty()) {
        rtc::IceServer s(turn);
        s.username = turn_user;
        s.password = turn_pass;
        c.iceServers.push_back(s);
    }
    logmsg("INFO", "pc", "configured " + std::to_string(c.iceServers.size()) + " ICE server(s)");
    return c;
}

int run_offer(int argc, char** argv) {
    auto pc = std::make_shared<rtc::PeerConnection>(build_config(argc, argv));
    wire_peer_connection(pc);

    auto dc = pc->createDataChannel("chat");      // triggers offer creation
    logmsg("INFO", "dc", "createDataChannel(chat) — this triggers the offer");
    wire_data_channel(dc);

    // local description will be printed via onLocalDescription
    std::string remote = read_remote_sdp_json();
    std::string type = extract_field(remote, "type");
    std::string sdp  = extract_field(remote, "sdp");
    if (type != "answer") { logmsg("ERR ", "sdp", "expected answer, got: " + type); return 1; }
    logmsg("INFO", "sdp", "setRemoteDescription(answer)");
    pc->setRemoteDescription(rtc::Description(sdp, type));

    std::unique_lock<std::mutex> lk(g_done_mu);
    g_done_cv.wait(lk, [] { return g_done.load(); });
    return 0;
}

int run_answer(int argc, char** argv) {
    auto pc = std::make_shared<rtc::PeerConnection>(build_config(argc, argv));
    wire_peer_connection(pc);

    pc->onDataChannel([](std::shared_ptr<rtc::DataChannel> dc) {
        logmsg("OK  ", "pc", "remote DataChannel: " + dc->label());
        wire_data_channel(dc);
    });

    std::string remote = read_remote_sdp_json();
    std::string type = extract_field(remote, "type");
    std::string sdp  = extract_field(remote, "sdp");
    if (type != "offer") { logmsg("ERR ", "sdp", "expected offer, got: " + type); return 1; }
    logmsg("INFO", "sdp", "setRemoteDescription(offer)  (answer is generated automatically)");
    pc->setRemoteDescription(rtc::Description(sdp, type));

    // local description (the answer) will be printed via onLocalDescription
    std::unique_lock<std::mutex> lk(g_done_mu);
    g_done_cv.wait(lk, [] { return g_done.load(); });
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2 || (std::string(argv[1]) != "offer" && std::string(argv[1]) != "answer")) {
        std::cerr << "usage: " << argv[0] << " {offer|answer} [--stun URL] [--turn URL --turn-user U --turn-pass P]\n";
        return 2;
    }
    rtc::InitLogger(rtc::LogLevel::Info);
    return std::string(argv[1]) == "offer" ? run_offer(argc, argv) : run_answer(argc, argv);
}
