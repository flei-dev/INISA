/* headers.p4 - TDTP 协议头部定义  */

// 标准以太网头部
header ethernet_t {
    bit<48> dstAddr;
    bit<48> srcAddr;
    bit<16> etherType;
}

// 标准 IPv4 头部
header ipv4_t {
    bit<4>  version;
    bit<4>  ihl;
    bit<8>  diffserv;
    bit<16> totalLen;
    bit<16> identification;
    bit<3>  flags;
    bit<13> fragOffset;
    bit<8>  ttl;
    bit<8>  protocol;
    bit<16> hdrChecksum;
    bit<32> srcAddr;
    bit<32> dstAddr;
}

// 标准 UDP 头部
header udp_t {
    bit<16> srcPort;
    bit<16> dstPort;
    bit<16> length;
    bit<16> checksum;
}

// ======== TDTP 协议头部（ 26 字节） ========
header tdtp_t {
    bit<8>  version;     // 协议版本 (1B)
    bit<8>  opcode;      // 操作码 (1B)
                         // 0x01=READ, 0x02=WRITE, 
                         // 0x03=STATUS, 0x04=ACK, 
                         // 0x05=PREFETCH
    bit<16> taskId;      // 训练任务 ID (2B)
    bit<32> batchId;     // 当前 mini-batch 序号 (4B)
    bit<32> objectId;    // 存储对象 ID (4B)
    bit<32> seqNum;      // TDTP 序列号 (4B)
    bit<32> offset;      // 对象内偏移 (4B)
    bit<32> length;      // 数据长度 (4B)
    bit<16> flags;       // 标志位 (2B)
                         // bit0=PUSH, bit1=URGENT, 
                         // bit2=LAST, bit3=PREFETCH
}

// 元数据（在匹配-动作流水线中使用）
struct metadata_t {
    bit<1>  is_tdtp;         // 是否为 TDTP 报文
    bit<3>  priority_class;  // 优先级分类 (0-3)
    bit<32> storage_ip;      // 地址翻译后的存储节点 IP
    bit<16> storage_port;    // 存储节点端口
    bit<2>  meter_color;     // [新增] 用于存储流量计量的颜色结果
}

struct headers_t {
    ethernet_t ethernet;
    ipv4_t     ipv4;
    udp_t      udp;
    tdtp_t     tdtp;
}