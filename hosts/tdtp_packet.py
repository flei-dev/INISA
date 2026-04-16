#!/usr/bin/env python3
# tdtp_packet.py - TDTP 协议的 Scapy 定义
from scapy.all import *

TDTP_PORT = 9999

# TDTP 操作码
TDTP_READ     = 0x01
TDTP_WRITE    = 0x02
TDTP_STATUS   = 0x03
TDTP_ACK      = 0x04
TDTP_PREFETCH = 0x05

# TDTP 标志位
TDTP_FLAG_PUSH     = 0x0001
TDTP_FLAG_URGENT   = 0x0002
TDTP_FLAG_LAST     = 0x0004
TDTP_FLAG_PREFETCH = 0x0008

class TDTP(Packet):
    name = 'TDTP'
    fields_desc = [
        ByteField('version', 1),
        ByteField('opcode', TDTP_READ),
        ShortField('taskId', 0),
        IntField('batchId', 0),
        IntField('objectId', 0),
        IntField('seqNum', 0),
        IntField('offset', 0),
        IntField('length', 0),
        ShortField('flags', 0),
    ]

# 绑定 : UDP 端口 9999 -> TDTP
bind_layers(UDP, TDTP, dport=TDTP_PORT)

def make_tdtp_read(task_id, batch_id, obj_id, length=4096):
    """ 构造 TDTP READ 报文 """
    return (Ether(dst='ff:ff:ff:ff:ff:ff') /
            IP(dst='10.0.2.2') /
            UDP(dport=TDTP_PORT) /
            TDTP(opcode=TDTP_READ, taskId=task_id,
                 batchId=batch_id, objectId=obj_id,
                 length=length))

def make_tdtp_status(task_id, batch_id, comp_time, load_time):
    """ 构造 TDTP STATUS 报文
        comp_time/load_time 编码在 offset 和 length 字段中 """
    return (Ether(dst='ff:ff:ff:ff:ff:ff') /
            IP(dst='10.0.1.1') /
            UDP(dport=TDTP_PORT) /
            TDTP(opcode=TDTP_STATUS, taskId=task_id,
                 batchId=batch_id,
                 offset=comp_time,   # 复用 offset 字段存 comp_time(us)
                 length=load_time))  # 复用 length 字段存 load_time(us)

def make_tdtp_prefetch(task_id, batch_id, obj_id):
    """ 构造 TDTP PREFETCH 报文 """
    return (Ether(dst='ff:ff:ff:ff:ff:ff') /
            IP(dst='10.0.2.2') /
            UDP(dport=TDTP_PORT) /
            TDTP(opcode=TDTP_PREFETCH, taskId=task_id,
                 batchId=batch_id, objectId=obj_id,
                 flags=TDTP_FLAG_PREFETCH))