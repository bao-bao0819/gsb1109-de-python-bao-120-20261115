# coding: utf-8
"""DAQmx 共享原始缓冲区逐通道报告

构造一个 DAQmx TDMS 段，其中三个逻辑通道共享同一个原始缓冲区
(raw_buffer_index = 0，每采样点 5 字节交错存储)：

    字节 0-1 : ai0  Int16  格式变更 scaler (scale_id=0)，带线性缩放属性
    字节 2-3 : ai1  Int16  格式变更 scaler (scale_id=0)，缺少缩放属性
    字节 4   : di2  Uint8  数字线 scaler   (scale_id=0)，仅最低 1 位有效

对每个通道输出：
  * 缓冲来源   —— 原始缓冲区索引、字节/位偏移、scale_id
  * 存储宽度   —— scaler 数据类型占用的位数
  * 有效位宽   —— 实际携带信息的位数（数字线 scaler 为 1 位）
  * 原码       —— 从共享缓冲区切出的未缩放原始码
  * 缩放结果   —— 应用该通道自身缩放属性后的结果；
                  缺缩放参数的通道为“未定”，不套用同缓冲区其他通道的参数
"""

from nptdms import TdmsFile
from nptdms.daqmx import DigitalLineScaler
from nptdms.test.util import (
    GeneratedFile, hexlify_value, string_hexlify, segment_objects_metadata,
    root_metadata, group_metadata)


def daqmx_scaler_metadata(scale_id, type_id, byte_offset, raw_buffer_index=0):
    return (
        hexlify_value("<I", type_id) +          # DAQmx 数据类型
        hexlify_value("<I", raw_buffer_index) +  # 原始缓冲区索引
        hexlify_value("<I", byte_offset) +       # 缓冲区内字节偏移
        "00 00 00 00" +                          # sample format bitmap
        hexlify_value("<I", scale_id))           # scale id


def digital_scaler_metadata(scale_id, type_id, bit_offset, raw_buffer_index=0):
    return (
        hexlify_value("<I", type_id) +
        hexlify_value("<I", raw_buffer_index) +
        hexlify_value("<I", bit_offset) +        # 缓冲区内位偏移
        "00" +
        hexlify_value("<I", scale_id))


def daqmx_channel_metadata(channel_name, num_values, raw_data_widths,
                           scaler_metadata, digital_line_scaler=False,
                           properties=None):
    path = "/'Group'/'" + channel_name + "'"
    props_hex = hexlify_value("<I", len(properties or {}))
    for (prop_name, (prop_type, prop_value)) in (properties or {}).items():
        props_hex += hexlify_value("<I", len(prop_name))
        props_hex += string_hexlify(prop_name)
        props_hex += hexlify_value("<I", prop_type)
        props_hex += prop_value
    return (
        hexlify_value("<I", len(path)) +
        string_hexlify(path) +
        ("6A 12 00 00" if digital_line_scaler else "69 12 00 00") +
        hexlify_value("<I", 0xFFFFFFFF) +        # DaqMxRawData
        "01 00 00 00" +                          # dimension = 1
        hexlify_value("<Q", num_values) +        # 每通道采样数
        hexlify_value("<I", len(scaler_metadata)) +
        "".join(scaler_metadata) +
        hexlify_value("<I", len(raw_data_widths)) +
        "".join(hexlify_value("<I", w) for w in raw_data_widths) +
        props_hex)


def string_property(value):
    return hexlify_value("<I", len(value)) + string_hexlify(value)


def build_file():
    # ai0：线性缩放 y = 0.5 * x + 1.0，输入为 scale 0（即 DAQmx scaler 0 的原码）
    ai0_properties = {
        "NI_Number_Of_Scales": (3, hexlify_value("<i", 2)),
        "NI_Scale[1]_Scale_Type": (32, string_property("Linear")),
        "NI_Scale[1]_Linear_Y_Intercept": (10, hexlify_value("<d", 1.0)),
        "NI_Scale[1]_Linear_Slope": (10, hexlify_value("<d", 0.5)),
        "NI_Scale[1]_Linear_Input_Source": (7, hexlify_value("<I", 0)),
    }
    # di2：单级缩放即 DAQmx scaler 原码（NI_Number_Of_Scales=1，无 Scale_Type）
    di2_properties = {
        "NI_Number_Of_Scales": (3, hexlify_value("<i", 1)),
    }
    # ai1：故意不给任何缩放属性

    metadata = segment_objects_metadata(
        root_metadata(),
        group_metadata(),
        daqmx_channel_metadata(
            "ai0", 4, [5], [daqmx_scaler_metadata(0, 3, 0)],
            properties=ai0_properties),
        daqmx_channel_metadata(
            "ai1", 4, [5], [daqmx_scaler_metadata(0, 3, 2)]),
        daqmx_channel_metadata(
            "di2", 4, [5], [digital_scaler_metadata(0, 0, 32)],
            digital_line_scaler=True, properties=di2_properties))

    # 共享缓冲区：每采样点 5 字节 = ai0(2) + ai1(2) + di2(1)
    data = (
        "01 00" "11 00" "00"
        "02 00" "12 00" "01"
        "03 00" "13 00" "00"
        "04 00" "14 00" "01"
    )

    test_file = GeneratedFile()
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList", "kTocDAQmxRawData"),
        metadata, data)
    return test_file


def channel_buffer_source(tdms_file):
    """从解析后的段元数据中提取每个通道的缓冲来源信息"""
    sources = {}
    for segment in tdms_file._reader._segments:
        for obj in segment.ordered_objects:
            daqmx_metadata = getattr(obj, "daqmx_metadata", None)
            if daqmx_metadata is None:
                continue
            for scaler in daqmx_metadata.scalers:
                storage_bits = scaler.data_type.size * 8
                if isinstance(scaler, DigitalLineScaler):
                    offset = "位偏移 %d（字节 %d 第 %d 位）" % (
                        scaler.raw_bit_offset,
                        scaler.raw_bit_offset // 8,
                        scaler.raw_bit_offset % 8)
                    valid_bits = 1
                else:
                    offset = "字节偏移 %d" % scaler.raw_byte_offset
                    valid_bits = storage_bits
                sources[obj.path] = {
                    "raw_buffer_index": scaler.raw_buffer_index,
                    "offset": offset,
                    "scale_id": scaler.scale_id,
                    "storage_bits": storage_bits,
                    "valid_bits": valid_bits,
                }
    return sources


def main():
    tdms_file = build_file().load()
    sources = channel_buffer_source(tdms_file)

    print("DAQmx 段：3 个逻辑通道共享原始缓冲区 0（每采样点 5 字节交错）")
    print("=" * 72)

    results = {}
    for channel in tdms_file["Group"].channels():
        path = channel.path
        source = sources[path]
        raw = [v.item() for v in channel.raw_scaler_data[source["scale_id"]]]
        try:
            scaled = [v.item() for v in channel.data]
            scaled_text = str(scaled)
        except ValueError as exc:
            scaled = None
            scaled_text = "未定（%s）" % exc

        results[channel.name] = (raw, scaled)

        print("通道 %s" % path)
        print("  缓冲来源 : 原始缓冲区 %d，%s，scale_id=%d"
              % (source["raw_buffer_index"], source["offset"], source["scale_id"]))
        print("  位宽     : 有效位宽 %d 位 / 存储宽度 %d 位（%s）"
              % (source["valid_bits"], source["storage_bits"],
                 channel.scaler_data_types[source["scale_id"]].__name__))
        print("  原码     : %s" % str(raw))
        print("  缩放结果 : %s" % scaled_text)
        print("-" * 72)

    # 验证：缩放参数按通道隔离
    ai0_raw, ai0_scaled = results["ai0"]
    ai1_raw, ai1_scaled = results["ai1"]
    di2_raw, di2_scaled = results["di2"]

    assert ai0_raw == [1, 2, 3, 4]
    assert ai0_scaled == [1.5, 2.0, 2.5, 3.0], "ai0 应用自身线性缩放"
    assert ai1_raw == [17, 18, 19, 20]
    assert ai1_scaled is None, "ai1 缺缩放参数，结果必须为未定"
    assert di2_raw == [0, 1, 0, 1] and di2_scaled == [0, 1, 0, 1]

    print("验证通过：")
    print("  * ai0 用自身线性参数缩放：%s" % str(ai0_scaled))
    print("  * ai1 缺缩放参数 -> 缩放结果未定，未套用同缓冲区 ai0 的")
    print("    slope=0.5 / intercept=1.0（否则会得到 [9.5, 10.0, 10.5, 11.0]）")
    print("  * di2 数字线：8 位存储中仅 1 位有效，原码与缩放结果一致")


if __name__ == "__main__":
    main()
