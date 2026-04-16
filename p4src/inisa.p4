/* inisa.p4 - INISA 主程序组装文件 */

#include <core.p4>
#include <v1model.p4>

// 引入我们自己定义的各个模块
#include "headers.p4"
#include "parser.p4"
#include "ingress.p4"
#include "egress.p4"

// ====== 组装 V1Switch 架构 ======
V1Switch(
    InisaParser(),
    InisaVerifyChecksum(),
    InisaIngress(),
    InisaEgress(),
    InisaComputeChecksum(),
    InisaDeparser()  // <--- 这里替换掉之前的 packet_out()
) main;