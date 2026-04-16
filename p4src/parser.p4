/* parser.p4 - TDTP 解析器实现  */
#define TDTP_UDP_PORT 9999  // TDTP 使用的 UDP 端口号

parser InisaParser(packet_in packet,
                   out headers_t hdr,
                   inout metadata_t meta,
                   inout standard_metadata_t smeta) {
    state start {
        transition parse_ethernet;
    }
    
    state parse_ethernet {
        packet.extract(hdr.ethernet);
        transition select(hdr.ethernet.etherType) {
            0x0800: parse_ipv4;
            default: accept;
        }
    }
    
    state parse_ipv4 {
        packet.extract(hdr.ipv4);
        transition select(hdr.ipv4.protocol) {
            17: parse_udp;   // UDP = 17
            default: accept;
        }
    }
    
    state parse_udp {
        packet.extract(hdr.udp);
        transition select(hdr.udp.dstPort) {
            TDTP_UDP_PORT: parse_tdtp;
            default: accept;
        }
    }
    
    state parse_tdtp {
        packet.extract(hdr.tdtp);
        meta.is_tdtp = 1;
        transition accept;
    }
}