# coding: utf-8

from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Optional

import numpy as np
import numpy.polynomial.polynomial as poly
import re

from nptdms.log import log_manager
import nptdms.thermocouples as thermocouples


log = log_manager.get_logger(__name__)

RAW_DATA_INPUT_SOURCE = 0xFFFFFFFF
VOLTAGE_EXCITATION = 10322
CURRENT_EXCITATION = 10134

#: Property that records the engineering units of a TDMS channel
UNIT_STRING_PROPERTY = "unit_string"

#: Properties that record the version of the parameters used by a scaling chain
PARAMETER_VERSION_PROPERTIES = (
    "NI_Parameter_Version",
    "NI_Scaling_Parameter_Version",
)


class ScalingError(Exception):
    """Base class for errors relating to TDMS scaling chains"""


class MissingScalingDependencyError(ScalingError):
    """Raised when a scale references another scale that is not defined.

    The raw, unscaled channel data can still be read when this error is
    raised while applying scaling.
    """


class CircularScalingReferenceError(ScalingError):
    """Raised when the input source references between scales form a cycle.

    The error message includes the cycle path. The raw, unscaled channel
    data can still be read.
    """


@dataclass
class ScaleInfo:
    """Metadata describing a single scale within a scaling chain.

    :ivar index: Index of the scale in the ``NI_Scale`` list.
    :ivar scale_type: Name of the scale type, for example ``"Linear"``,
        or ``"DaqMxScaler"`` for a scale backed by DAQmx raw scaler data.
    :ivar input_sources: Indices of the scales providing input data.
        A value of :data:`RAW_DATA_INPUT_SOURCE` means the channel's raw
        data is used directly. DAQmx scaler scales have no input sources.
    :ivar input_unit: Engineering units of the scale input, if recorded.
    :ivar output_unit: Engineering units of the scale output, if recorded.
    :ivar parameters: Parameters used to construct the scale, keyed by the
        property suffix, excluding structural properties such as the scale
        type, input sources, units and parameter version.
    :ivar parameter_version: Version identifier for the scale parameters,
        or None if no version was recorded.
    :ivar property_scope: Where the scaling properties came from, one of
        ``"channel"``, ``"group"`` or ``"file"``.
    """

    index: int
    scale_type: str
    input_sources: tuple
    input_unit: Optional[str] = None
    output_unit: Optional[str] = None
    parameters: dict = field(default_factory=dict)
    parameter_version: Optional[Any] = None
    property_scope: Optional[str] = None


@dataclass
class ScalingChainInfo:
    """Metadata describing the full scaling chain of a channel.

    :ivar status: Raw value of the ``NI_Scaling_Status`` property,
        defaulting to ``"unscaled"``.
    :ivar already_scaled: True when the data was written with scaling
        already applied, so no scales are applied when reading.
    :ivar scales: :class:`ScaleInfo` for each scale index. References to
        indices outside this list, or None entries, are reported by
        :meth:`missing_dependencies`.
    :ivar final_scale_index: Index of the scale that produces the final
        channel data.
    :ivar property_scope: Where the scaling properties came from, one of
        ``"channel"``, ``"group"`` or ``"file"``.
    :ivar parameter_version: Version identifier for the scaling chain
        parameters, or None if no version was recorded.
    :ivar channel_unit: Engineering units recorded against the channel
        itself in the ``unit_string`` property, if present.
    """

    status: str
    scales: list
    final_scale_index: int
    property_scope: str
    already_scaled: bool = False
    parameter_version: Optional[Any] = None
    channel_unit: Optional[str] = None

    @property
    def output_unit(self):
        """Engineering units of the final scale, if recorded"""
        final_scale = self._final_scale()
        if final_scale is not None and final_scale.output_unit is not None:
            return final_scale.output_unit
        return self.channel_unit

    def missing_dependencies(self):
        """Return ``(scale_index, missing_source)`` pairs for broken references"""
        missing = []
        for scale_info in self.scales:
            if scale_info is None:
                continue
            for source in scale_info.input_sources:
                if source == RAW_DATA_INPUT_SOURCE:
                    continue
                if source < 0 or source >= len(self.scales) or self.scales[source] is None:
                    missing.append((scale_info.index, source))
        return missing

    def cycles(self):
        """Return a list of cycles, each as a list of scale indices.

        The first and last index of each cycle are the same.
        """
        found_cycles = []
        path = []
        visited = set()

        def visit(index):
            if index == RAW_DATA_INPUT_SOURCE or index in visited:
                return
            if index in path:
                cycle_start = path.index(index)
                found_cycles.append(path[cycle_start:] + [index])
                return
            if index < 0 or index >= len(self.scales) or self.scales[index] is None:
                return
            path.append(index)
            for source in self.scales[index].input_sources:
                visit(source)
            path.remove(index)
            visited.add(index)

        visit(self.final_scale_index)
        return found_cycles

    def is_valid(self):
        """Return True if the chain has no missing dependencies or cycles"""
        return not self.missing_dependencies() and not self.cycles()

    def ordered_scale_indices(self):
        """Return reachable scale indices in dependency order"""
        ordered = []
        visited = set()

        def visit(index):
            if (index == RAW_DATA_INPUT_SOURCE or index in visited or
                    index < 0 or index >= len(self.scales) or
                    self.scales[index] is None):
                return
            visited.add(index)
            for source in self.scales[index].input_sources:
                visit(source)
            ordered.append(index)

        visit(self.final_scale_index)
        return ordered

    def chain_path(self):
        """Return a human readable description of the scaling chain.

        For example ``"raw -> 0(Linear) -> 1(Polynomial)"``. Scales with
        multiple inputs show their inputs joined by commas.
        """
        rendered = set()

        def render(index):
            if index == RAW_DATA_INPUT_SOURCE:
                return "raw"
            if index < 0 or index >= len(self.scales) or self.scales[index] is None:
                return "missing scale %d" % index
            scale_info = self.scales[index]
            label = "%d(%s)" % (index, scale_info.scale_type)
            if index in rendered or not scale_info.input_sources:
                return label
            rendered.add(index)
            inputs = ", ".join(render(source) for source in scale_info.input_sources)
            return "%s -> %s" % (inputs, label)

        return render(self.final_scale_index)

    def _final_scale(self):
        if 0 <= self.final_scale_index < len(self.scales):
            return self.scales[self.final_scale_index]
        return None


class NoOpScaling(object):
    """ Does not apply any scaling
    """
    scale_type = "AdvancedAPI"

    def __init__(self, input_source):
        self.input_source = input_source

    @staticmethod
    def from_properties(properties, scale_index, scale_name):
        try:
            input_source = properties[
                "NI_Scale[%d]_%s_Input_Source" % (scale_index, scale_name)]
        except KeyError:
            input_source = RAW_DATA_INPUT_SOURCE
        return NoOpScaling(input_source)

    def scale(self, data):
        return data


class LinearScaling(object):
    """ Linear scaling with slope and intercept
    """
    scale_type = "Linear"

    def __init__(self, intercept, slope, input_source):
        self.intercept = intercept
        self.slope = slope
        self.input_source = input_source

    @staticmethod
    def from_properties(properties, scale_index):
        try:
            input_source = properties[
                "NI_Scale[%d]_Linear_Input_Source" % scale_index]
        except KeyError:
            input_source = RAW_DATA_INPUT_SOURCE
        return LinearScaling(
            properties["NI_Scale[%d]_Linear_Y_Intercept" % scale_index],
            properties["NI_Scale[%d]_Linear_Slope" % scale_index],
            input_source)

    def scale(self, data):
        return data * self.slope + self.intercept


class PolynomialScaling(object):
    """ Polynomial scaling with an arbitrary number of coefficients
    """
    scale_type = "Polynomial"

    def __init__(self, coefficients, input_source):
        self.coefficients = coefficients
        self.input_source = input_source

    @staticmethod
    def from_properties(properties, scale_index):
        try:
            number_of_coefficients = properties[
                'NI_Scale[%d]_Polynomial_Coefficients_Size' % scale_index]
        except KeyError:
            number_of_coefficients = 4
        try:
            input_source = properties[
                "NI_Scale[%d]_Polynomial_Input_Source" % scale_index]
        except KeyError:
            input_source = RAW_DATA_INPUT_SOURCE
        coefficients = [
            properties[
                'NI_Scale[%d]_Polynomial_Coefficients[%d]' % (scale_index, i)]
            for i in range(number_of_coefficients)]
        return PolynomialScaling(coefficients, input_source)

    def scale(self, data):
        if len(self.coefficients) == 0:
            return np.zeros(len(data), dtype=np.dtype('float64'))

        # Ensure data is double type before scaling
        data = data.astype(np.dtype('float64'), copy=False)
        return np.polynomial.polynomial.polyval(data, self.coefficients)


class RtdScaling(object):
    """ Converts a signal from a resistance temperature detector into
        degrees Celsius using the Callendar-Van Dusen equation
    """
    scale_type = "RTD"

    def __init__(
            self, current_excitation, r0_nominal_resistance,
            a, b, c,
            lead_wire_resistance, resistance_configuration, input_source):
        self.current_excitation = current_excitation
        self.r0_nominal_resistance = r0_nominal_resistance
        self.a = a
        self.b = b
        self.c = c
        self.lead_wire_resistance = lead_wire_resistance
        self.resistance_configuration = resistance_configuration
        self.input_source = input_source

    @staticmethod
    def from_properties(properties, scale_index):
        prefix = "NI_Scale[%d]" % scale_index
        current_excitation = properties[
            "%s_RTD_Current_Excitation" % prefix]
        r0_nominal_resistance = properties[
            "%s_RTD_R0_Nominal_Resistance" % prefix]
        a = properties["%s_RTD_A" % prefix]
        b = properties["%s_RTD_B" % prefix]
        c = properties["%s_RTD_C" % prefix]
        lead_wire_resistance = properties[
            "%s_RTD_Lead_Wire_Resistance" % prefix]
        resistance_configuration = properties[
            "%s_RTD_Resistance_Configuration" % prefix]
        input_source = properties[
            "%s_RTD_Input_Source" % prefix]
        return RtdScaling(
            current_excitation, r0_nominal_resistance, a, b, c,
            lead_wire_resistance, resistance_configuration, input_source)

    def scale(self, data):
        """ Convert voltage data to temperature
        """
        (a, b, r_0) = (self.a, self.b, self.r0_nominal_resistance)

        # R(T) = R(0)[1 + A*T + B*T^2 + (T - 100)*C*T^3]
        # R(T) = V/I

        r_t = data / self.current_excitation
        r_t = _adjust_for_lead_resistance(
            r_t, CURRENT_EXCITATION, self.resistance_configuration, self.lead_wire_resistance)

        positive_temperature = r_t >= r_0
        # First solve for positive temperatures using the quadratic form
        temperature = (-a + np.sqrt(a ** 2 - 4.0 * b * (1.0 - r_t / r_0), where=positive_temperature)) / (2.0 * b)
        if not np.all(positive_temperature):
            # Use full quartic for any negative temperatures
            for i in np.where(np.logical_not(positive_temperature))[0]:
                temperature[i] = self._solve_quartic_form(r_t[i])
        return temperature

    def _solve_quartic_form(self, r_t):
        (a, b, c, r_0) = (self.a, self.b, self.c, self.r0_nominal_resistance)
        poly_coefficients = [r_0 - r_t, r_0 * a, r_0 * b, -100.0 * r_0 * c, r_0 * c]
        roots = poly.polyroots(poly_coefficients)
        return RtdScaling._get_negative_real_root(roots)

    @staticmethod
    def _get_negative_real_root(roots):
        filtered = [r for r in roots if not np.iscomplex(r) and r.real < 0.0]
        if len(filtered) != 1:
            raise ValueError("Expected single real valued negative root for RTD equation")
        return filtered[0].real


class StrainScaling(object):
    """ Converts a voltage measurement from a strain gauge bridge to strain
    """
    scale_type = "Strain"

    def __init__(
            self, configuration, poisson_ratio, gage_resistance, lead_wire_resistance, initial_bridge_voltage,
            gage_factor, gain_adjustment, voltage_excitation, input_source):
        self.configuration = configuration
        self.poisson_ratio = poisson_ratio
        self.gage_resistance = gage_resistance
        self.lead_wire_resistance = lead_wire_resistance
        self.initial_bridge_voltage = initial_bridge_voltage
        self.gage_factor = gage_factor
        self.gain_adjustment = gain_adjustment
        self.voltage_excitation = voltage_excitation
        self.input_source = input_source

    @staticmethod
    def from_properties(properties, scale_index):
        prefix = "NI_Scale[%d]_Strain" % scale_index
        configuration = properties["%s_Configuration" % prefix]
        poisson_ratio = properties["%s_Poisson_Ratio" % prefix]
        gage_resistance = properties["%s_Gage_Resistance" % prefix]
        lead_wire_resistance = properties["%s_Lead_Wire_Resistance" % prefix]
        initial_bridge_voltage = properties["%s_Initial_Bridge_Voltage" % prefix]
        gage_factor = properties["%s_Gage_Factor" % prefix]
        gain_adjustment = properties["%s_Bridge_Shunt_Calibration_Gain_Adjustment" % prefix]
        voltage_excitation = properties["%s_Voltage_Excitation" % prefix]
        input_source = properties["%s_Input_Source" % prefix]
        return StrainScaling(
            configuration, poisson_ratio, gage_resistance, lead_wire_resistance, initial_bridge_voltage, gage_factor,
            gain_adjustment, voltage_excitation, input_source)

    def scale(self, data):
        """ Convert voltage data to strain
        """
        # For a Wheatstone bridge, Vo = [R3 / (R3 + R4) - R2 / (R1 + R2)] Vex
        # The input data for this scaling is the output voltage (Vo) and we
        # want to compute the strain (ε)
        voltage_out = data.astype(np.double)
        if self.initial_bridge_voltage != 0.0:
            voltage_out -= self.initial_bridge_voltage
        if self.configuration == StrainScaling.FULL_BRIDGE_1:
            # In the full bridge type I configuration:
            # R1 = R3 = R0 (1 - ε G)
            # R2 = R4 = R0 (1 + ε G)
            # where G is the gauge factor and R0 is the base gague resistance.
            # This gives Vo = -ε G Vex
            strain = voltage_out
            strain *= (-self.gain_adjustment / (self.voltage_excitation * self.gage_factor))
            return strain
        elif self.configuration == StrainScaling.FULL_BRIDGE_2:
            # In the full bridge type II configuration:
            # R1 = R0 (1 - ε ν G)
            # R2 = R0 (1 + ε ν G)
            # R3 = R0 (1 - ε G)
            # R4 = R0 (1 + ε G)
            # This gives Vo = - (1/2) ε G Vex (1 + ν)
            strain = voltage_out
            strain *= (-self.gain_adjustment * 2.0 / (
                self.voltage_excitation * self.gage_factor * (1.0 + self.poisson_ratio)))
            return strain
        elif self.configuration == StrainScaling.FULL_BRIDGE_3:
            # In the full bridge type III configuration:
            # R1 = R3 = R0 (1 - ε ν G)
            # R2 = R4 = R0 (1 + ε G)
            # This gives Vo = - ε G (1 + ν) Vex / (2 + ε G (1 - ν))
            # Rearranging for strain:
            # ε = - 2 Vo / (G Vex (1 + ν + (Vo/Vex) (1 - ν)))
            common_factor = -0.5 / self.gain_adjustment
            temp = voltage_out.copy()
            temp *= common_factor * (1.0 - self.poisson_ratio) * self.gage_factor
            temp += common_factor * self.voltage_excitation * self.gage_factor * (1.0 + self.poisson_ratio)
            strain = voltage_out
            strain /= temp
            return strain
        elif self.configuration == StrainScaling.HALF_BRIDGE_1:
            # In the half bridge type I configuration:
            # R1 = R2 = R0
            # R3 = R0 (1 - ε ν G)
            # R4 = R0 (1 + ε G)
            # Which gives Vo = [(1 - ε ν G) / (2 + ε G - ε ν G) - 1/2] Vex
            # Rearranging for strain:
            # ε = -4 (Vo / Vex) / [G (1 + ν + 2 (Vo / Vex) (1 - ν))]
            lead_adjustment = 1.0 / (1.0 + self.lead_wire_resistance / self.gage_resistance)
            common_factor = -self.gage_factor * self.voltage_excitation * lead_adjustment / (
                    4.0 * self.gain_adjustment)
            temp = voltage_out.copy()
            temp *= common_factor * 2.0 * (1.0 - self.poisson_ratio) / self.voltage_excitation
            temp += common_factor * (1.0 + self.poisson_ratio)
            strain = voltage_out
            strain /= temp
            return strain
        elif self.configuration == StrainScaling.HALF_BRIDGE_2:
            # In the half bridge type II configuration:
            # R1 = R2 = R0
            # R3 = R0 (1 - ε G)
            # R4 = R0 (1 + ε G)
            # This gives Vo = - ε G Vex / 2
            # Rearrange for strain:
            # ε = -2 Vo / (Vex G)
            lead_adjustment = 1.0 / (1.0 + self.lead_wire_resistance / self.gage_resistance)
            strain = voltage_out
            strain *= -2.0 * self.gain_adjustment / (
                    self.gage_factor * self.voltage_excitation * lead_adjustment)
            return strain
        elif self.configuration in (StrainScaling.QUARTER_BRIDGE_1, StrainScaling.QUARTER_BRIDGE_2):
            # In the quarter bridge configuration:
            # R1 = R2 = R3 = R0
            # R4 = R0 (1 + ε G)
            # Which gives Vo = [1 / (2 + ε G) - (1 / 2)] Vex
            # Rearranging for strain:
            # ε = -2 / G [1 - 1 / (1 + 2 Vo / Vex)]
            lead_adjustment = 1.0 / (1.0 + self.lead_wire_resistance / self.gage_resistance)
            strain = voltage_out
            strain *= 2.0 / self.voltage_excitation
            strain += 1.0
            np.reciprocal(strain, out=strain)
            strain -= 1.0
            strain *= 2.0 * self.gain_adjustment / (self.gage_factor * lead_adjustment)
            return strain

        raise Exception("Strain gauge configuration %d is not supported" % self.configuration)

    FULL_BRIDGE_1 = 10183
    FULL_BRIDGE_2 = 10184
    FULL_BRIDGE_3 = 10185
    HALF_BRIDGE_1 = 10188
    HALF_BRIDGE_2 = 10189
    QUARTER_BRIDGE_1 = 10271
    QUARTER_BRIDGE_2 = 10272


class TableScaling(object):
    """ Scales data using a map from input to output values with
        linear interpolation for points in between inputs.
    """
    scale_type = "Table"

    def __init__(
            self, pre_scaled_values, scaled_values, input_source):

        # Ensure pre-scaled values (X axis) are monotonically increasing for interpolation to work
        if not np.all(np.diff(pre_scaled_values) > 0):
            pre_scaled_values = np.flip(pre_scaled_values)
            scaled_values = np.flip(scaled_values)
        if not np.all(np.diff(pre_scaled_values) > 0):
            # Reversing didn't help
            raise ValueError(
                "Table scaled values must be monotonically "
                "increasing or decreasing")

        self.input_values = pre_scaled_values
        self.output_values = scaled_values
        self.input_source = input_source

    @staticmethod
    def from_properties(properties, scale_index):
        prefix = "NI_Scale[%d]_Table_" % scale_index
        try:
            input_source = properties[prefix + "Input_Source"]
        except KeyError:
            input_source = RAW_DATA_INPUT_SOURCE
        num_pre_scaled_values = properties[
            prefix + "Pre_Scaled_Values_Size"]
        num_scaled_values = properties[
            prefix + "Scaled_Values_Size"]
        if num_pre_scaled_values != num_scaled_values:
            raise ValueError(
                "Number of pre-scaled values does not match "
                "number of scaled values")
        pre_scaled_values = np.array([
            properties[prefix + "Pre_Scaled_Values[%d]" % i]
            for i in range(num_pre_scaled_values)])
        scaled_values = np.array([
            properties[prefix + "Scaled_Values[%d]" % i]
            for i in range(num_scaled_values)])
        return TableScaling(pre_scaled_values, scaled_values, input_source)

    def scale(self, data):
        """ Calculate scaled data
        """
        return np.interp(data, self.input_values, self.output_values)


class ThermistorScaling(object):
    """ Converts a voltage measurement from a Thermistor into temperature in Kelvin
    """
    scale_type = "Thermistor"

    def __init__(
            self,
            excitation_type,
            excitation_value,
            resistance_configuration,
            r1_reference_resistance,
            lead_wire_resistance,
            a, b, c,
            temperature_offset,
            input_source):
        self.excitation_type = excitation_type
        self.excitation_value = excitation_value
        self.resistance_configuration = resistance_configuration
        self.r1_reference_resistance = r1_reference_resistance
        self.lead_wire_resistance = lead_wire_resistance
        self.a = a
        self.b = b
        self.c = c
        self.temperature_offset = temperature_offset
        self.input_source = input_source

    @staticmethod
    def from_properties(properties, scale_index):
        prefix = "NI_Scale[%d]_Thermistor" % scale_index
        excitation_type = properties["%s_Excitation_Type" % prefix]
        excitation_value = properties["%s_Excitation_Value" % prefix]
        resistance_configuration = properties["%s_Resistance_Configuration" % prefix]
        r1_reference_resistance = properties["%s_R1_Reference_Resistance" % prefix]
        lead_wire_resistance = properties["%s_Lead_Wire_Resistance" % prefix]
        a = properties["%s_A" % prefix]
        b = properties["%s_B" % prefix]
        c = properties["%s_C" % prefix]
        temperature_offset = properties["%s_Temperature_Offset" % prefix]
        input_source = properties["%s_Input_Source" % prefix]
        return ThermistorScaling(
            excitation_type, excitation_value,
            resistance_configuration, r1_reference_resistance, lead_wire_resistance,
            a, b, c, temperature_offset, input_source)

    def scale(self, data):
        """ Convert voltage data to temperature in Kelvin
        """
        # Ensure data is double precision
        data = data.astype(np.dtype('float64'), copy=False)
        if self.excitation_type == CURRENT_EXCITATION:
            r_t = data / self.excitation_value
        elif self.excitation_type == VOLTAGE_EXCITATION:
            # Calculate resistance based on voltage divider circuit
            # R_t = R1 / ((V_excitation / V_out) - 1)
            r_t = self.r1_reference_resistance * np.reciprocal(self.excitation_value * np.reciprocal(data) - 1.0)
        else:
            raise ValueError("Invalid excitation type: %s" % self.excitation_type)

        r_t = _adjust_for_lead_resistance(
            r_t, self.excitation_type, self.resistance_configuration, self.lead_wire_resistance)

        coefficients = [self.a, self.b, 0.0, self.c]
        return np.reciprocal(
            np.polynomial.polynomial.polyval(np.log(r_t), coefficients)) - self.temperature_offset


class ThermocoupleScaling(object):
    """ Convert between voltages in uV and degrees Celsius for a Thermocouple.
        Can convert in either direction depending on the scaling direction
        parameter.
    """
    scale_type = "Thermocouple"

    def __init__(self, type_code, scaling_direction, input_source):
        # Thermocouple types from
        # http://zone.ni.com/reference/en-XX/help/371361R-01/glang/tdms_create_scalinginfo/#instance2
        thermocouple_types = {
            10047: thermocouples.type_b,
            10055: thermocouples.type_e,
            10072: thermocouples.type_j,
            10073: thermocouples.type_k,
            10077: thermocouples.type_n,
            10082: thermocouples.type_r,
            10085: thermocouples.type_s,
            10086: thermocouples.type_t,
        }
        self.thermocouple = thermocouple_types[type_code]

        self.scaling_direction = scaling_direction
        self.input_source = input_source

    @staticmethod
    def from_properties(properties, scale_index):
        prefix = "NI_Scale[%d]_Thermocouple" % scale_index
        input_source = properties.get(
            "%s_Input_Source" % prefix, RAW_DATA_INPUT_SOURCE)
        type_code = properties.get(
            "%s_Thermocouple_Type" % prefix, 10072)
        scaling_direction = properties.get(
            "%s_Scaling_Direction" % prefix, 0)
        return ThermocoupleScaling(type_code, scaling_direction, input_source)

    def scale(self, data):
        """ Apply thermocouple scaling
        """
        # Note that the thermocouple conversions use mV for voltages, but TDMS uses uV.
        if self.scaling_direction == 1:
            return 1000.0 * self.thermocouple.celsius_to_mv(data)
        else:
            milli_volts = data / 1000.0
            return self.thermocouple.mv_to_celsius(milli_volts)


class AddScaling(object):
    """ Adds two scalings
    """
    scale_type = "Add"

    def __init__(self, left_input_source, right_input_source):
        self.left_input_source = left_input_source
        self.right_input_source = right_input_source

    @staticmethod
    def from_properties(properties, scale_index):
        left_input_source = properties[
            "NI_Scale[%d]_Add_Left_Operand_Input_Source" % scale_index]
        right_input_source = properties[
            "NI_Scale[%d]_Add_Right_Operand_Input_Source" % scale_index]
        return AddScaling(left_input_source, right_input_source)

    def scale(self, left_data, right_data):
        return left_data + right_data


class SubtractScaling(object):
    """ Subtracts one scaling from another
    """
    scale_type = "Subtract"

    def __init__(self, left_input_source, right_input_source):
        self.left_input_source = left_input_source
        self.right_input_source = right_input_source

    @staticmethod
    def from_properties(properties, scale_index):
        left_input_source = properties[
            "NI_Scale[%d]_Subtract_Left_Operand_Input_Source" % scale_index]
        right_input_source = properties[
            "NI_Scale[%d]_Subtract_Right_Operand_Input_Source" % scale_index]
        return SubtractScaling(left_input_source, right_input_source)

    def scale(self, left_data, right_data):
        """ Calculate scaled data
        """

        # Subtracting the left operand from the right doesn't make much sense,
        # but this does match the Excel TDMS plugin behaviour.
        return right_data - left_data


class DaqMxScalerScaling(object):
    """ Reads scaler from DAQmx data
    """
    scale_type = "DaqMxScaler"

    def __init__(self, scale_id):
        self.scale_id = scale_id

    def scale_daqmx(self, scaler_data):
        return scaler_data[self.scale_id]


class MultiScaling(object):
    """ Computes scaled data from multiple scalings
    """
    def __init__(self, scalings, chain_info=None):
        self.scalings = scalings
        self.chain_info = chain_info

    @property
    def final_scale_index(self):
        return len(self.scalings) - 1

    def scale(self, raw_channel_data):
        self._validate_chain()
        return self._compute_scaled_data(self.final_scale_index, raw_channel_data)

    def get_dtype(self, raw_data_type, scaler_data_types):
        """ Get the numpy dtype for scaled data
        """
        self._validate_chain()
        return self._compute_scale_dtype(
            self.final_scale_index, raw_data_type, scaler_data_types)

    def _validate_chain(self):
        """ Check that all input source references resolve and there are no
            cycles, raising a descriptive error otherwise.
        """
        path = []

        def visit(scale_index):
            if scale_index == RAW_DATA_INPUT_SOURCE:
                return
            if scale_index in path:
                cycle = path[path.index(scale_index):] + [scale_index]
                raise CircularScalingReferenceError(
                    "Circular scaling reference detected: %s" %
                    self._format_scale_path(cycle))
            if (scale_index < 0 or scale_index >= len(self.scalings) or
                    self.scalings[scale_index] is None):
                raise MissingScalingDependencyError(
                    "Scale %s references a missing scale %d. Scaling chain: %s" %
                    (path[-1] if path else self.final_scale_index,
                     scale_index, self._describe_chain()))
            path.append(scale_index)
            for source in self._input_sources(self.scalings[scale_index]):
                visit(source)
            path.pop()

        visit(self.final_scale_index)

    def _describe_chain(self):
        if self.chain_info is not None:
            return self.chain_info.chain_path()
        return self._format_scale_path(
            [self.final_scale_index])

    def _format_scale_path(self, indices):
        return " -> ".join(
            "raw" if index == RAW_DATA_INPUT_SOURCE else
            self._scale_label(index) for index in indices)

    def _scale_label(self, scale_index):
        if (scale_index < 0 or scale_index >= len(self.scalings) or
                self.scalings[scale_index] is None):
            return "missing scale %d" % scale_index
        scaling = self.scalings[scale_index]
        return "%d(%s)" % (scale_index, getattr(scaling, "scale_type", type(scaling).__name__))

    @staticmethod
    def _input_sources(scaling):
        if isinstance(scaling, DaqMxScalerScaling):
            return ()
        if hasattr(scaling, 'input_source'):
            return (scaling.input_source,)
        if hasattr(scaling, 'left_input_source') and hasattr(scaling, 'right_input_source'):
            return (scaling.left_input_source, scaling.right_input_source)
        return ()

    def _compute_scale_dtype(self, scale_index, raw_data_type, scaler_data_types):
        if scale_index == RAW_DATA_INPUT_SOURCE:
            return raw_data_type.nptype
        scaling = self.scalings[scale_index]
        if isinstance(scaling, DaqMxScalerScaling):
            return scaler_data_types[scaling.scale_id].nptype
        elif isinstance(scaling, AddScaling) or isinstance(scaling, SubtractScaling):
            return np.result_type(
                self._compute_scale_dtype(scaling.left_input_source, raw_data_type, scaler_data_types),
                self._compute_scale_dtype(scaling.right_input_source, raw_data_type, scaler_data_types))
        elif isinstance(scaling, NoOpScaling):
            return raw_data_type.nptype
        else:
            # Any other scaling type should produce double data
            return np.dtype('float64')

    def _compute_scaled_data(self, scale_index, raw_channel_data):
        """ Compute output data from a single scale in the set of all scalings,
            computing any required input scales recursively.
        """
        if scale_index == RAW_DATA_INPUT_SOURCE:
            if raw_channel_data.data is None:
                raise Exception("Invalid scaling input source for DAQmx data")
            return raw_channel_data.data

        scaling = self.scalings[scale_index]
        if scaling is None:
            raise MissingScalingDependencyError(
                "Cannot compute data for missing scale %d. Scaling chain: %s" %
                (scale_index, self._describe_chain()))

        if isinstance(scaling, DaqMxScalerScaling):
            return scaling.scale_daqmx(raw_channel_data.scaler_data)
        elif hasattr(scaling, 'input_source'):
            input_data = self._compute_scaled_data(
                scaling.input_source, raw_channel_data)
            return scaling.scale(input_data)
        elif (hasattr(scaling, 'left_input_source') and
              hasattr(scaling, 'right_input_source')):
            left_input_data = self._compute_scaled_data(
                scaling.left_input_source, raw_channel_data)
            right_input_data = self._compute_scaled_data(
                scaling.right_input_source, raw_channel_data)
            return scaling.scale(left_input_data, right_input_data)
        else:
            raise ValueError("Cannot compute scaled data for %r" % scaling)


def get_scaling(channel_properties, group_properties, file_properties):
    """ Get scaling for a channel from either the channel itself,
        its group, or the whole TDMS file
    """
    multi_scaling, _ = _build_scalings(
        channel_properties, group_properties, file_properties)
    return multi_scaling


def get_scaling_chain_info(channel_properties, group_properties, file_properties):
    """ Get metadata describing the scaling chain of a channel.

    Returns a :class:`ScalingChainInfo` if the channel (or its group or
    file properties) defines any scales, otherwise None. The chain info is
    returned even when the data was already written in scaled form, in which
    case :attr:`ScalingChainInfo.already_scaled` is True and no scales are
    applied when reading data.
    """
    multi_scaling, fallback_info = _build_scalings(
        channel_properties, group_properties, file_properties)
    if multi_scaling is not None:
        return multi_scaling.chain_info
    return fallback_info


def _build_scalings(channel_properties, group_properties, file_properties):
    """ Build scaling objects and chain info from channel, group and file
        properties, returning ``(multi_scaling, fallback_chain_info)``.
    """
    fallback_info = None
    for scope, properties in (
            ("channel", channel_properties),
            ("group", group_properties),
            ("file", file_properties)):
        multi_scaling, chain_info = _build_channel_scaling(properties, scope)
        if chain_info is not None and fallback_info is None:
            fallback_info = chain_info
        if multi_scaling is not None:
            if chain_info is not None:
                chain_info.channel_unit = channel_properties.get(UNIT_STRING_PROPERTY)
            return multi_scaling, fallback_info
    if fallback_info is not None:
        fallback_info.channel_unit = channel_properties.get(UNIT_STRING_PROPERTY)
    return None, fallback_info


def _build_channel_scaling(properties, scope):
    num_scalings = _get_number_of_scalings(properties)
    if num_scalings is None or num_scalings == 0:
        return None, None

    scaling_status = properties.get("NI_Scaling_Status", "unscaled")
    chain_info = _build_chain_info(
        properties, num_scalings, scope, scaling_status)

    if scaling_status == "scaled":
        # Data is written with scaling already applied, so don't build
        # scaling objects and never apply scales again.
        return None, chain_info

    scalings = [None] * num_scalings
    for scale_index in range(num_scalings):
        type_property = 'NI_Scale[%d]_Scale_Type' % scale_index
        try:
            scale_type = properties[type_property]
        except KeyError:
            # Scalings are not in properties if they come from DAQmx scalers
            scalings[scale_index] = DaqMxScalerScaling(scale_index)
            continue
        if scale_type == 'Polynomial':
            scalings[scale_index] = PolynomialScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'Linear':
            scalings[scale_index] = LinearScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'RTD':
            scalings[scale_index] = RtdScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'Strain':
            scalings[scale_index] = StrainScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'Table':
            scalings[scale_index] = TableScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'Thermistor':
            scalings[scale_index] = ThermistorScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'Thermocouple':
            scalings[scale_index] = ThermocoupleScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'Add':
            scalings[scale_index] = AddScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'Subtract':
            scalings[scale_index] = SubtractScaling.from_properties(
                properties, scale_index)
        elif scale_type == 'AdvancedAPI':
            scalings[scale_index] = NoOpScaling.from_properties(
                properties, scale_index, 'AdvancedAPI')
        else:
            log.warning("Unsupported scale type: %s", scale_type)
            return None, chain_info

    if not scalings:
        return None, chain_info
    return MultiScaling(scalings, chain_info), chain_info


def _build_chain_info(properties, num_scalings, scope, scaling_status):
    """ Build chain metadata without constructing any scaling objects, so
        metadata inspection never fails on a malformed scaling chain.
    """
    parameter_version = _get_parameter_version(properties)
    scale_infos = []
    for scale_index in range(num_scalings):
        type_property = 'NI_Scale[%d]_Scale_Type' % scale_index
        scale_type = properties.get(type_property, "DaqMxScaler")
        scale_infos.append(
            _build_scale_info(properties, scale_index, scale_type, scope))

    return ScalingChainInfo(
        status=scaling_status,
        scales=scale_infos,
        final_scale_index=num_scalings - 1,
        property_scope=scope,
        already_scaled=(scaling_status == "scaled"),
        parameter_version=parameter_version)


def _build_scale_info(properties, scale_index, scale_type, scope):
    prefix = "NI_Scale[%d]_" % scale_index
    parameters = OrderedDict()
    input_unit = None
    output_unit = None
    parameter_version = None
    input_sources = []

    for key, value in properties.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix):]
        if suffix == "Scale_Type":
            continue
        if suffix.endswith("_Input_Unit"):
            input_unit = value
            continue
        if suffix.endswith("_Output_Unit"):
            output_unit = value
            continue
        if suffix.endswith("_Parameter_Version"):
            parameter_version = value
            continue
        if suffix.endswith("_Left_Operand_Input_Source"):
            input_sources.append(("left", value))
            continue
        if suffix.endswith("_Right_Operand_Input_Source"):
            input_sources.append(("right", value))
            continue
        if suffix.endswith("_Input_Source"):
            input_sources.append(("input", value))
            continue
        parameters[suffix] = value

    ordered_sources = []
    for operand, source in input_sources:
        if operand == "left":
            ordered_sources.insert(0, source)
        elif operand == "right":
            ordered_sources.append(source)
        else:
            ordered_sources.append(source)
    if not ordered_sources and scale_type != "DaqMxScaler":
        ordered_sources = [RAW_DATA_INPUT_SOURCE]

    return ScaleInfo(
        index=scale_index,
        scale_type=scale_type,
        input_sources=tuple(ordered_sources),
        input_unit=input_unit,
        output_unit=output_unit,
        parameters=parameters,
        parameter_version=parameter_version,
        property_scope=scope)


def _get_parameter_version(properties):
    for version_property in PARAMETER_VERSION_PROPERTIES:
        if version_property in properties:
            return properties[version_property]
    return None


_scale_regex = re.compile(r"NI_Scale\[(\d+)\]_Scale_Type")


def _get_number_of_scalings(properties):
    num_scalings_property = "NI_Number_Of_Scales"
    if num_scalings_property in properties:
        return int(properties[num_scalings_property])

    matches = (_scale_regex.match(key) for key in properties.keys())
    try:
        return max(int(m.group(1)) for m in matches if m is not None) + 1
    except ValueError:
        return None


def _adjust_for_lead_resistance(
        measured_resistance, excitation_type, resistance_configuration, lead_wire_resistance):
    """" Adjust a measured resistance to account for lead wire resistance
    """
    if resistance_configuration == 3:
        return measured_resistance - lead_wire_resistance
    if excitation_type == CURRENT_EXCITATION and resistance_configuration == 2:
        return measured_resistance - 2.0 * lead_wire_resistance
    return measured_resistance
