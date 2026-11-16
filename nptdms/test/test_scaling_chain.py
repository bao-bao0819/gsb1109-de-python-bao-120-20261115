"""Integration tests for scaling chain metadata and broken scaling chains
"""

from collections import OrderedDict

import numpy as np
import pytest

from nptdms.scaling import (
    CircularScalingReferenceError,
    MissingScalingDependencyError,
    RAW_DATA_INPUT_SOURCE,
)
from nptdms.test.util import (
    GeneratedFile,
    channel_metadata,
    hexlify_value,
    segment_objects_metadata,
    string_hexlify,
)


TDS_TYPE_INT32 = 3
TDS_TYPE_UINT32 = 7
TDS_TYPE_DOUBLE = 10
TDS_TYPE_STRING = 0x20


def _string_property(name, value):
    return {
        name: (
            TDS_TYPE_STRING,
            hexlify_value("<I", len(value)) + string_hexlify(value))
    }


def _double_property(name, value):
    return {name: (TDS_TYPE_DOUBLE, hexlify_value("<d", value))}


def _uint_property(name, value):
    return {name: (TDS_TYPE_UINT32, hexlify_value("<I", value))}


def _linear_scale(scale_index, input_source, slope, intercept, extra_properties=None):
    properties = OrderedDict()
    properties["NI_Scale[%d]_Scale_Type" % scale_index] = (
        TDS_TYPE_STRING,
        hexlify_value("<I", len("Linear")) + string_hexlify("Linear"))
    properties["NI_Scale[%d]_Linear_Slope" % scale_index] = (
        TDS_TYPE_DOUBLE, hexlify_value("<d", slope))
    properties["NI_Scale[%d]_Linear_Y_Intercept" % scale_index] = (
        TDS_TYPE_DOUBLE, hexlify_value("<d", intercept))
    properties["NI_Scale[%d]_Linear_Input_Source" % scale_index] = (
        TDS_TYPE_UINT32, hexlify_value("<I", input_source))
    if extra_properties:
        properties.update(extra_properties)
    return properties


def _file_with_scaling(properties):
    data_hex = "".join(hexlify_value("<i", value) for value in (1, 2, 3, 4))
    test_file = GeneratedFile()
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'group'/'channel1'", TDS_TYPE_INT32, 4, properties),
        ),
        data_hex,
    )
    return test_file


def test_channel_exposes_scaling_chain_with_units_and_versions():
    properties = OrderedDict()
    properties.update(_uint_property("NI_Number_Of_Scales", 2))
    properties.update(_string_property("NI_Parameter_Version", "v42"))
    properties.update(_string_property("unit_string", "degC"))
    scale_units = OrderedDict()
    scale_units.update(_string_property("NI_Scale[0]_Linear_Input_Unit", "V"))
    scale_units.update(_string_property("NI_Scale[0]_Linear_Output_Unit", "degC"))
    properties.update(_linear_scale(
        0, RAW_DATA_INPUT_SOURCE, 2.0, 10.0, scale_units))
    properties.update(_linear_scale(1, 0, 1.0, 1.0))

    tdms_file = _file_with_scaling(properties).load()
    channel = tdms_file["group"]["channel1"]
    chain_info = channel.scaling_chain

    assert chain_info is not None
    assert chain_info.property_scope == "channel"
    assert chain_info.parameter_version == "v42"
    assert chain_info.channel_unit == "degC"
    assert chain_info.output_unit == "degC"
    assert [scale.scale_type for scale in chain_info.scales] == ["Linear", "Linear"]
    assert chain_info.scales[0].input_sources == (RAW_DATA_INPUT_SOURCE,)
    assert chain_info.scales[1].input_sources == (0,)
    assert chain_info.scales[0].input_unit == "V"
    assert chain_info.scales[0].output_unit == "degC"
    assert chain_info.scales[0].parameters["Linear_Slope"] == 2.0
    assert chain_info.chain_path() == "raw -> 0(Linear) -> 1(Linear)"

    # Scaling is still applied correctly: ((x * 2 + 10) * 1 + 1)
    np.testing.assert_array_equal(
        channel[:], np.array([13.0, 15.0, 17.0, 19.0]))


def test_raw_data_is_delivered_when_scaling_chain_has_missing_dependency():
    properties = OrderedDict()
    properties.update(_uint_property("NI_Number_Of_Scales", 1))
    properties.update(_linear_scale(0, 9, 2.0, 10.0))

    tdms_file = _file_with_scaling(properties).load()
    channel = tdms_file["group"]["channel1"]
    chain_info = channel.scaling_chain

    assert chain_info.missing_dependencies() == [(0, 9)]
    assert not chain_info.is_valid()

    # Raw data is still readable
    np.testing.assert_array_equal(
        channel.read_data(scaled=False), np.array([1, 2, 3, 4], dtype=np.int32))
    np.testing.assert_array_equal(
        channel.raw_data, np.array([1, 2, 3, 4], dtype=np.int32))

    # Applying the broken scaling raises with the chain path
    with pytest.raises(MissingScalingDependencyError) as exc_info:
        channel.read_data(scaled=True)
    assert "missing scale 9" in str(exc_info.value)


def test_raw_data_is_delivered_when_scaling_chain_is_circular():
    properties = OrderedDict()
    properties.update(_uint_property("NI_Number_Of_Scales", 2))
    properties.update(_linear_scale(0, 1, 1.0, 0.0))
    properties.update(_linear_scale(1, 0, 1.0, 0.0))

    tdms_file = _file_with_scaling(properties).load()
    channel = tdms_file["group"]["channel1"]
    chain_info = channel.scaling_chain

    assert chain_info.cycles() == [[1, 0, 1]]

    np.testing.assert_array_equal(
        channel.read_data(scaled=False), np.array([1, 2, 3, 4], dtype=np.int32))

    with pytest.raises(CircularScalingReferenceError) as exc_info:
        channel.read_data(scaled=True)
    message = str(exc_info.value)
    assert "1(Linear) -> 0(Linear) -> 1(Linear)" in message


def test_already_scaled_data_is_not_scaled_again_but_chain_is_recorded():
    properties = OrderedDict()
    properties.update(_uint_property("NI_Number_Of_Scales", 1))
    properties.update(_string_property("NI_Scaling_Status", "scaled"))
    properties.update(_linear_scale(0, RAW_DATA_INPUT_SOURCE, 2.0, 10.0))

    tdms_file = _file_with_scaling(properties).load()
    channel = tdms_file["group"]["channel1"]

    # Stored data is returned unchanged
    np.testing.assert_array_equal(
        channel[:], np.array([1, 2, 3, 4], dtype=np.int32))
    assert channel.scaling_chain.already_scaled is True
    assert channel.scaling_chain.scales[0].parameters["Linear_Slope"] == 2.0


def test_channel_without_scaling_has_no_chain_info():
    test_file = GeneratedFile()
    data_hex = "".join(hexlify_value("<i", value) for value in (1, 2, 3, 4))
    test_file.add_segment(
        ("kTocMetaData", "kTocRawData", "kTocNewObjList"),
        segment_objects_metadata(
            channel_metadata("/'group'/'channel1'", TDS_TYPE_INT32, 4),
        ),
        data_hex,
    )
    tdms_file = test_file.load()
    channel = tdms_file["group"]["channel1"]

    assert channel.scaling_chain is None
    np.testing.assert_array_equal(
        channel[:], np.array([1, 2, 3, 4], dtype=np.int32))
