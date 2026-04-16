/* egress.p4 - 出口流水线 */

control InisaEgress(inout headers_t hdr,
                    inout metadata_t meta,
                    inout standard_metadata_t smeta) {
    apply {
        // 在 INISA 阶段0 的仿真中，出口端不需要复杂的处理
        // 主要是将入口端处理好的报文直接发出去
        // 在真实的 Tofino 硬件上，这里可能会处理组播/多播逻辑
    }
}

// ====== 校验和计算 ======
// 重新计算 IPv4 的 Checksum，因为 Ingress 可能修改了 IP 头（地址翻译）
control InisaComputeChecksum(inout headers_t hdr, inout metadata_t meta) {
    apply {
        update_checksum(
            hdr.ipv4.isValid(),
            { hdr.ipv4.version,
              hdr.ipv4.ihl,
              hdr.ipv4.diffserv,
              hdr.ipv4.totalLen,
              hdr.ipv4.identification,
              hdr.ipv4.flags,
              hdr.ipv4.fragOffset,
              hdr.ipv4.ttl,
              hdr.ipv4.protocol,
              hdr.ipv4.srcAddr,
              hdr.ipv4.dstAddr },
            hdr.ipv4.hdrChecksum,
            HashAlgorithm.csum16);
    }
}

// ====== 校验和验证 ======
control InisaVerifyChecksum(inout headers_t hdr, inout metadata_t meta) {
    apply {
        // 简化处理，仿真中跳过严格的进入校验
    }
}

// ====== 逆解析器 (Deparser) ======
control InisaDeparser(packet_out packet, in headers_t hdr) {
    apply {
        // 按照从外到内的顺序，将头部重新序列化为字节流
        packet.emit(hdr.ethernet);
        packet.emit(hdr.ipv4);
        packet.emit(hdr.udp);
        packet.emit(hdr.tdtp);
    }
}