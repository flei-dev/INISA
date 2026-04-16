/* ingress.p4 - 入口流水线 (v3 修复版: 增加 ACK 反向路由) */
control InisaIngress(inout headers_t hdr,
                     inout metadata_t meta,
                     inout standard_metadata_t smeta) {

    // ====== 功能 1: 对象地址翻译表 (READ/PREFETCH → 存储节点) ======
    action set_storage_dest(bit<32> node_ip,
                            bit<16> node_port,
                            bit<9>  egress_port) {
        hdr.ipv4.dstAddr = node_ip;
        hdr.udp.dstPort = node_port;
        smeta.egress_spec = egress_port;
        meta.storage_ip = node_ip;
    }

    action drop() {
        mark_to_drop(smeta);
    }

    table object_address_translation {
        key = {
            hdr.tdtp.objectId: exact;
        }
        actions = {
            set_storage_dest;
            drop;
        }
        size = 65536;
        default_action = drop();
    }

    // ====== 功能 1b: ACK 反向路由表 (ACK → GPU 节点) ======
    // ACK 报文根据 taskId 路由回对应的 GPU 训练节点
    action set_gpu_dest(bit<32> gpu_ip,
                        bit<16> gpu_port,
                        bit<9>  egress_port) {
        hdr.ipv4.dstAddr = gpu_ip;
        hdr.udp.dstPort = gpu_port;
        smeta.egress_spec = egress_port;
    }

    table task_to_gpu {
        key = {
            hdr.tdtp.taskId: exact;
        }
        actions = {
            set_gpu_dest;
            drop;
        }
        size = 1024;
        default_action = drop();
    }

    // ====== 功能 2: 优先级分类 ======
    action set_priority(bit<3> prio) {
        smeta.priority = prio;
        meta.priority_class = prio;
    }

    table priority_classify {
        key = {
            hdr.tdtp.flags: ternary;
        }
        actions = {
            set_priority;
        }
        const entries = {
            0x0002 &&& 0x0002: set_priority(0);
            0x0008 &&& 0x0008: set_priority(2);
        }
        default_action = set_priority(1);
    }

    // ====== 功能 3: per-task 流量计量 (meter) ======
    direct_meter<bit<2>>(MeterType.bytes) task_meter;

    action read_task_meter() {
        task_meter.read(meta.meter_color);
    }

    table task_metering {
        key = {
            hdr.tdtp.taskId: exact;
        }
        actions = {
            read_task_meter;
        }
        meters = task_meter;
        size = 1024;
        default_action = read_task_meter();
    }

    // ====== 功能 4: STATUS 报文克隆到控制平面 ======
    action clone_to_controller() {
        clone(CloneType.I2E, 100);
    }

    // ====== 主流程 ======
    apply {
        if (hdr.tdtp.isValid()) {
            // STATUS 报文克隆一份到控制平面
            if (hdr.tdtp.opcode == 0x03) {
                clone_to_controller();
            }

            // ★ 关键修复: ACK 报文走反向路由（按 taskId → GPU）
            if (hdr.tdtp.opcode == 0x04) {
                task_to_gpu.apply();
            } else {
                // READ / PREFETCH / STATUS / WRITE 走对象地址翻译（按 objectId → 存储）
                object_address_translation.apply();
            }

            // 优先级分类
            priority_classify.apply();

            // 流量计量
            task_metering.apply();
        } else {
            drop();
        }
    }
}
