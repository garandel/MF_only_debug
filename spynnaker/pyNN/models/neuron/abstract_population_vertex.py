# Copyright (c) 2017-2019 The University of Manchester
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

import logging
import os
import math
import numpy
from scipy import special  # @UnresolvedImport

from spinn_utilities.overrides import overrides
from spinn_utilities.progress_bar import ProgressBar
from pacman.model.constraints.key_allocator_constraints import (
    ContiguousKeyRangeContraint)
from pacman.executor.injection_decorator import inject_items
from pacman.model.resources import (
    ConstantSDRAM, CPUCyclesPerTickResource, DTCMResource, ResourceContainer)
from spinn_front_end_common.abstract_models import (
    AbstractChangableAfterRun, AbstractProvidesOutgoingPartitionConstraints,
    AbstractCanReset, AbstractRewritesDataSpecification)
from spinn_front_end_common.abstract_models.impl import (
    ProvidesKeyToAtomMappingImpl, TDMAAwareApplicationVertex)
from spinn_front_end_common.utilities import (
    helpful_functions, globals_variables)
from spinn_front_end_common.utilities.constants import (
    BYTES_PER_WORD, SYSTEM_BYTES_REQUIREMENT, MICRO_TO_SECOND_CONVERSION)
from spinn_front_end_common.interface.profiling import profile_utils
from spynnaker.pyNN.models.common import (
    AbstractSpikeRecordable, AbstractNeuronRecordable, NeuronRecorder)
from spynnaker.pyNN.utilities import bit_field_utilities
from spynnaker.pyNN.models.abstract_models import (
    AbstractPopulationInitializable, AbstractAcceptsIncomingSynapses,
    AbstractPopulationSettable, AbstractContainsUnits, AbstractMaxSpikes)
from spynnaker.pyNN.exceptions import InvalidParameterType
from spynnaker.pyNN.utilities.ranged import (
    SpynnakerRangeDictionary, SpynnakerRangedList)
from spynnaker.pyNN.utilities.constants import POSSION_SIGMA_SUMMATION_LIMIT
from spynnaker.pyNN.utilities.running_stats import RunningStats
from spynnaker.pyNN.models.neuron.synapse_dynamics import (
    AbstractSynapseDynamics, AbstractSynapseDynamicsStructural)
from .population_machine_vertex import PopulationMachineVertex
from .synapse_io import get_maximum_delay_supported_in_ms, get_max_row_info
from .master_pop_table import MasterPopTableAsBinarySearch
from .generator_data import GeneratorData
from .synaptic_matrices import SYNAPSES_BASE_GENERATOR_SDRAM_USAGE_IN_BYTES

logger = logging.getLogger(__name__)

# TODO: Make sure these values are correct (particularly CPU cycles)
_NEURON_BASE_DTCM_USAGE_IN_BYTES = 9 * BYTES_PER_WORD
_NEURON_BASE_N_CPU_CYCLES_PER_NEURON = 22
_NEURON_BASE_N_CPU_CYCLES = 10

# 1 for drop late packets.
_SYNAPSES_BASE_SDRAM_USAGE_IN_BYTES = 1 * BYTES_PER_WORD


class AbstractPopulationVertex(
        TDMAAwareApplicationVertex, AbstractContainsUnits,
        AbstractSpikeRecordable, AbstractNeuronRecordable,
        AbstractProvidesOutgoingPartitionConstraints,
        AbstractPopulationInitializable, AbstractPopulationSettable,
        AbstractChangableAfterRun, AbstractAcceptsIncomingSynapses,
        ProvidesKeyToAtomMappingImpl, AbstractCanReset):
    """ Underlying vertex model for Neural Populations.
        Not actually abstract.
    """

    __slots__ = [
        "__all_single_syn_sz",
        "__change_requires_mapping",
        "__change_requires_data_generation",
        "__incoming_spike_buffer_size",
        "__n_atoms",
        "__n_profile_samples",
        "__neuron_impl",
        "__neuron_recorder",
        "_parameters",  # See AbstractPyNNModel
        "__pynn_model",
        "_state_variables",  # See AbstractPyNNModel
        "__time_between_requests",
        "__units",
        "__n_subvertices",
        "__n_data_specs",
        "__initial_state_variables",
        "__has_reset_last",
        "__updated_state_variables",
        "__ring_buffer_sigma",
        "__spikes_per_second",
        "__drop_late_spikes",
        "__incoming_projections",
        "__synapse_dynamics"]

    #: recording region IDs
    _SPIKE_RECORDING_REGION = 0

    #: the size of the runtime SDP port data region
    _RUNTIME_SDP_PORT_SIZE = BYTES_PER_WORD

    #: The Buffer traffic type
    _TRAFFIC_IDENTIFIER = "BufferTraffic"

    # 5 elements before the start of global parameters
    # 1. has key, 2. key, 3. n atoms,
    # 4. n synapse types, 5. incoming spike buffer size.
    BYTES_TILL_START_OF_GLOBAL_PARAMETERS = 5 * BYTES_PER_WORD

    def __init__(
            self, n_neurons, label, constraints, max_atoms_per_core,
            spikes_per_second, ring_buffer_sigma, incoming_spike_buffer_size,
            neuron_impl, pynn_model, drop_late_spikes):
        """
        :param int n_neurons: The number of neurons in the population
        :param str label: The label on the population
        :param list(~pacman.model.constraints.AbstractConstraint) constraints:
            Constraints on where a population's vertices may be placed.
        :param int max_atoms_per_core:
            The maximum number of atoms (neurons) per SpiNNaker core.
        :param spikes_per_second: Expected spike rate
        :type spikes_per_second: float or None
        :param ring_buffer_sigma:
            How many SD above the mean to go for upper bound of ring buffer \
            size; a good starting choice is 5.0. Given length of simulation \
            we can set this for approximate number of saturation events.
        :type ring_buffer_sigma: float or None
        :param incoming_spike_buffer_size:
        :type incoming_spike_buffer_size: int or None
        :param bool drop_late_spikes: control flag for dropping late packets.
        :param AbstractNeuronImpl neuron_impl:
            The (Python side of the) implementation of the neurons themselves.
        :param AbstractPyNNNeuronModel pynn_model:
            The PyNN neuron model that this vertex is working on behalf of.
        """

        # pylint: disable=too-many-arguments, too-many-locals
        TDMAAwareApplicationVertex.__init__(
            self, label, constraints, max_atoms_per_core)

        self.__n_atoms = n_neurons
        self.__n_subvertices = 0
        self.__n_data_specs = 0

        # buffer data
        self.__incoming_spike_buffer_size = incoming_spike_buffer_size

        # get config from simulator
        config = globals_variables.get_simulator().config

        if incoming_spike_buffer_size is None:
            self.__incoming_spike_buffer_size = config.getint(
                "Simulation", "incoming_spike_buffer_size")

        # Limit the DTCM used by one-to-one connections
        self.__all_single_syn_sz = config.getint(
            "Simulation", "one_to_one_connection_dtcm_max_bytes")

        self.__ring_buffer_sigma = ring_buffer_sigma
        if self.__ring_buffer_sigma is None:
            self.__ring_buffer_sigma = config.getfloat(
                "Simulation", "ring_buffer_sigma")

        self.__spikes_per_second = spikes_per_second
        if self.__spikes_per_second is None:
            self.__spikes_per_second = config.getfloat(
                "Simulation", "spikes_per_second")

        self.__drop_late_spikes = drop_late_spikes
        if self.__drop_late_spikes is None:
            self.__drop_late_spikes = config.getboolean(
                "Simulation", "drop_late_spikes")

        self.__neuron_impl = neuron_impl
        self.__pynn_model = pynn_model
        self._parameters = SpynnakerRangeDictionary(n_neurons)
        self._state_variables = SpynnakerRangeDictionary(n_neurons)
        self.__neuron_impl.add_parameters(self._parameters)
        self.__neuron_impl.add_state_variables(self._state_variables)
        self.__initial_state_variables = None
        self.__updated_state_variables = set()

        # Set up for recording
        recordable_variables = list(
            self.__neuron_impl.get_recordable_variables())
        record_data_types = dict(
            self.__neuron_impl.get_recordable_data_types())
        self.__neuron_recorder = NeuronRecorder(
            recordable_variables, record_data_types, [NeuronRecorder.SPIKES],
            n_neurons, [NeuronRecorder.PACKETS],
            {NeuronRecorder.PACKETS: NeuronRecorder.PACKETS_TYPE})

        # bool for if state has changed.
        self.__change_requires_mapping = True
        self.__change_requires_data_generation = False
        self.__has_reset_last = True

        # Set up for profiling
        self.__n_profile_samples = helpful_functions.read_config_int(
            config, "Reports", "n_profile_samples")

        # Set up for incoming
        self.__incoming_projections = list()
        self.__ring_buffer_shifts = None
        self.__weight_scales = None

        # Prepare for dealing with STDP - there can only be one (non-static)
        # synapse dynamics per vertex at present
        self.__synapse_dynamics = None

    @property
    def synapse_dynamics(self):
        """ The synapse dynamics used by the synapses e.g. plastic or static.
            Settable.

        :rtype: AbstractSynapseDynamics or None
        """
        return self.__synapse_dynamics

    @synapse_dynamics.setter
    def synapse_dynamics(self, synapse_dynamics):
        """ Set the synapse dynamics.  Note that after setting, the dynamics
            might not be the type set as it can be combined with the existing
            dynamics in exciting ways.
        """
        if self.__synapse_dynamics is None:
            self.__synapse_dynamics = synapse_dynamics
        else:
            self.__synapse_dynamics = self.__synapse_dynamics.merge(
                synapse_dynamics)

    def add_incoming_projection(self, projection):
        """ Add a projection incoming to this vertex
        """
        # Reset the ring buffer shifts as a projection has been added
        self.__change_requires_mapping = True
        self.__ring_buffer_shifts = None
        self.__weight_scales = None
        self.__incoming_projections.append(projection)

    @property
    @overrides(TDMAAwareApplicationVertex.n_atoms)
    def n_atoms(self):
        return self.__n_atoms

    @property
    def size(self):
        return self.__n_atoms

    @property
    def all_single_syn_size(self):
        """ The maximum amount of DTCM to use for single synapses

        :rtype: int
        """
        return self.__all_single_syn_sz

    @property
    def incoming_spike_buffer_size(self):
        return self.__incoming_spike_buffer_size

    @property
    def parameters(self):
        return self._parameters

    @property
    def state_variables(self):
        return self._state_variables

    @property
    def neuron_impl(self):
        return self.__neuron_impl

    @property
    def n_profile_samples(self):
        return self.__n_profile_samples

    @property
    def neuron_recorder(self):  # for testing only
        return self.__neuron_recorder

    @property
    def drop_late_spikes(self):
        return self.__drop_late_spikes

    def update_state_variables(self):
        """ processes any changes since init

        :rtype: None
        """

        # If resetting
        if self.__has_reset_last:
            # reset any state variables that need to be reset
            if self.__initial_state_variables is not None:
                self._state_variables = self.__copy_ranged_dict(
                    self.__initial_state_variables, self._state_variables,
                    self.__updated_state_variables)
                self.__initial_state_variables = None
            else:
                # If no initial state variables, copy them now
                self.__initial_state_variables = self.__copy_ranged_dict(
                    self._state_variables)

        # Reset things that need resetting
        self.__has_reset_last = False
        self.__updated_state_variables.clear()

    @inject_items({
        "graph": "MemoryApplicationGraph"
    })
    @overrides(
        TDMAAwareApplicationVertex.get_resources_used_by_atoms,
        additional_arguments={"graph"}
    )
    def get_resources_used_by_atoms(self, vertex_slice, graph):
        # pylint: disable=arguments-differ

        variableSDRAM = self.__neuron_recorder.get_variable_sdram_usage(
            vertex_slice)
        constantSDRAM = ConstantSDRAM(
            self._get_sdram_usage_for_atoms(vertex_slice, graph))

        # set resources required from this object
        container = ResourceContainer(
            sdram=variableSDRAM + constantSDRAM,
            dtcm=DTCMResource(self.get_dtcm_usage_for_atoms(vertex_slice)),
            cpu_cycles=CPUCyclesPerTickResource(
                self.get_cpu_usage_for_atoms(vertex_slice)))

        # return the total resources.
        return container

    @property
    @overrides(AbstractChangableAfterRun.requires_mapping)
    def requires_mapping(self):
        return self.__change_requires_mapping

    @property
    @overrides(AbstractChangableAfterRun.requires_data_generation)
    def requires_data_generation(self):
        return self.__change_requires_data_generation

    @overrides(AbstractChangableAfterRun.mark_no_changes)
    def mark_no_changes(self):
        self.__change_requires_mapping = False
        self.__change_requires_data_generation = False

    @overrides(TDMAAwareApplicationVertex.create_machine_vertex)
    def create_machine_vertex(
            self, vertex_slice, resources_required, label=None,
            constraints=None):
        self.__n_subvertices += 1
        return PopulationMachineVertex(
            resources_required,
            self.__neuron_recorder.recorded_ids_by_slice(vertex_slice),
            label, constraints, self, vertex_slice,
            self._get_binary_file_name())

    def get_cpu_usage_for_atoms(self, vertex_slice):
        """
        :param ~pacman.model.graphs.common.Slice vertex_slice:
        """
        # TODO: Add CPU cycles for processing synapses?
        return (
            _NEURON_BASE_N_CPU_CYCLES +
            (_NEURON_BASE_N_CPU_CYCLES_PER_NEURON * vertex_slice.n_atoms) +
            self.__neuron_recorder.get_n_cpu_cycles(vertex_slice.n_atoms) +
            self.__neuron_impl.get_n_cpu_cycles(vertex_slice.n_atoms))

    def get_dtcm_usage_for_atoms(self, vertex_slice):
        """
        :param ~pacman.model.graphs.common.Slice vertex_slice:
        """
        # TODO: Add DTCM for synapses?
        return (
            _NEURON_BASE_DTCM_USAGE_IN_BYTES +
            self.__neuron_impl.get_dtcm_usage_in_bytes(vertex_slice.n_atoms) +
            self.__neuron_recorder.get_dtcm_usage_in_bytes(vertex_slice))

    def get_sdram_usage_for_neuron_params(self, vertex_slice):
        """ Calculate the SDRAM usage for just the neuron parameters region.

        :param ~pacman.model.graphs.common.Slice vertex_slice:
            the slice of atoms.
        :return: The SDRAM required for the neuron region
        """
        return (
            self.BYTES_TILL_START_OF_GLOBAL_PARAMETERS +
            self.tdma_sdram_size_in_bytes +
            self.__neuron_impl.get_sdram_usage_in_bytes(vertex_slice.n_atoms))

    def _get_sdram_usage_for_atoms(self, vertex_slice, graph):
        sdram_requirement = (
            SYSTEM_BYTES_REQUIREMENT +
            self.get_sdram_usage_for_neuron_params(vertex_slice) +
            self.neuron_recorder.get_static_sdram_usage(vertex_slice) +
            PopulationMachineVertex.get_provenance_data_size(
                len(PopulationMachineVertex.EXTRA_PROVENANCE_DATA_ENTRIES)) +
            self.get_synapse_dynamics_size(vertex_slice) +
            self.get_structural_dynamics_size(vertex_slice) +
            self.get_synapses_size(vertex_slice) +
            self.get_pop_table_size() +
            self.get_synapse_expander_size() +
            profile_utils.get_profile_region_size(
                self.__n_profile_samples) +
            bit_field_utilities.get_estimated_sdram_for_bit_field_region(
                graph, self) +
            bit_field_utilities.get_estimated_sdram_for_key_region(
                graph, self) +
            bit_field_utilities.exact_sdram_for_bit_field_builder_region())
        return sdram_requirement

    @staticmethod
    def __copy_ranged_dict(source, merge=None, merge_keys=None):
        target = SpynnakerRangeDictionary(len(source))
        for key in source.keys():
            copy_list = SpynnakerRangedList(len(source))
            if merge_keys is None or key not in merge_keys:
                init_list = source.get_list(key)
            else:
                init_list = merge.get_list(key)
            for start, stop, value in init_list.iter_ranges():
                is_list = (hasattr(value, '__iter__') and
                           not isinstance(value, str))
                copy_list.set_value_by_slice(start, stop, value, is_list)
            target[key] = copy_list
        return target

    @overrides(AbstractSpikeRecordable.is_recording_spikes)
    def is_recording_spikes(self):
        return self.__neuron_recorder.is_recording(NeuronRecorder.SPIKES)

    @overrides(AbstractSpikeRecordable.set_recording_spikes)
    def set_recording_spikes(
            self, new_state=True, sampling_interval=None, indexes=None):
        self.set_recording(
            NeuronRecorder.SPIKES, new_state, sampling_interval, indexes)

    @overrides(AbstractSpikeRecordable.get_spikes)
    def get_spikes(
            self, placements, buffer_manager, machine_time_step):
        return self.__neuron_recorder.get_spikes(
            self.label, buffer_manager, placements, self,
            NeuronRecorder.SPIKES, machine_time_step)

    @overrides(AbstractNeuronRecordable.get_recordable_variables)
    def get_recordable_variables(self):
        return self.__neuron_recorder.get_recordable_variables()

    @overrides(AbstractNeuronRecordable.is_recording)
    def is_recording(self, variable):
        return self.__neuron_recorder.is_recording(variable)

    @overrides(AbstractNeuronRecordable.set_recording)
    def set_recording(self, variable, new_state=True, sampling_interval=None,
                      indexes=None):
        self.__change_requires_mapping = not self.is_recording(variable)
        self.__neuron_recorder.set_recording(
            variable, new_state, sampling_interval, indexes)

    @overrides(AbstractNeuronRecordable.get_data)
    def get_data(self, variable, n_machine_time_steps, placements,
                 buffer_manager, machine_time_step):
        # pylint: disable=too-many-arguments
        return self.__neuron_recorder.get_matrix_data(
            self.label, buffer_manager, placements, self, variable,
            n_machine_time_steps)

    @overrides(AbstractNeuronRecordable.get_neuron_sampling_interval)
    def get_neuron_sampling_interval(self, variable):
        return self.__neuron_recorder.get_neuron_sampling_interval(variable)

    @overrides(AbstractSpikeRecordable.get_spikes_sampling_interval)
    def get_spikes_sampling_interval(self):
        return self.__neuron_recorder.get_neuron_sampling_interval("spikes")

    @overrides(AbstractPopulationInitializable.initialize)
    def initialize(self, variable, value):
        if not self.__has_reset_last:
            raise Exception(
                "initialize can only be called before the first call to run, "
                "or before the first call to run after a reset")
        if variable not in self._state_variables:
            raise KeyError(
                "Vertex does not support initialisation of"
                " parameter {}".format(variable))
        self._state_variables.set_value(variable, value)
        self.__updated_state_variables.add(variable)
        for vertex in self.machine_vertices:
            if isinstance(vertex, AbstractRewritesDataSpecification):
                vertex.set_reload_required(True)

    @property
    def initialize_parameters(self):
        """ The names of parameters that have default initial values.

        :rtype: iterable(str)
        """
        return self.__pynn_model.default_initial_values.keys()

    def _get_parameter(self, variable):
        if variable.endswith("_init"):
            # method called with "V_init"
            key = variable[:-5]
            if variable in self._state_variables:
                # variable is v and parameter is v_init
                return variable
            elif key in self._state_variables:
                # Oops neuron defines v and not v_init
                return key
        else:
            # method called with "v"
            if variable + "_init" in self._state_variables:
                # variable is v and parameter is v_init
                return variable + "_init"
            if variable in self._state_variables:
                # Oops neuron defines v and not v_init
                return variable

        # parameter not found for this variable
        raise KeyError("No variable {} found in {}".format(
            variable, self.__neuron_impl.model_name))

    def _get_binary_file_name(self):

        # Split binary name into title and extension
        binary_title, binary_extension = os.path.splitext(
            self.__neuron_impl.binary_name)

        suffix = ""
        if self.__synapse_dynamics is not None:
            suffix = self.__synapse_dynamics.get_vertex_executable_suffix()

        # Reunite title and extension and return
        return (binary_title + suffix + binary_extension)

    @overrides(AbstractPopulationInitializable.get_initial_value)
    def get_initial_value(self, variable, selector=None):
        parameter = self._get_parameter(variable)

        ranged_list = self._state_variables[parameter]
        if selector is None:
            return ranged_list
        return ranged_list.get_values(selector)

    @overrides(AbstractPopulationInitializable.set_initial_value)
    def set_initial_value(self, variable, value, selector=None):
        if variable not in self._state_variables:
            raise KeyError(
                "Vertex does not support initialisation of"
                " parameter {}".format(variable))

        parameter = self._get_parameter(variable)
        ranged_list = self._state_variables[parameter]
        ranged_list.set_value_by_selector(selector, value)
        for vertex in self.machine_vertices:
            if isinstance(vertex, AbstractRewritesDataSpecification):
                vertex.set_reload_required(True)

    @property
    def conductance_based(self):
        """
        :rtype: bool
        """
        return self.__neuron_impl.is_conductance_based

    @overrides(AbstractPopulationSettable.get_value)
    def get_value(self, key):
        """ Get a property of the overall model.
        """
        if key not in self._parameters:
            raise InvalidParameterType(
                "Population {} does not have parameter {}".format(
                    self.__neuron_impl.model_name, key))
        return self._parameters[key]

    @overrides(AbstractPopulationSettable.set_value)
    def set_value(self, key, value):
        """ Set a property of the overall model.
        """
        if key not in self._parameters:
            raise InvalidParameterType(
                "Population {} does not have parameter {}".format(
                    self.__neuron_impl.model_name, key))
        self._parameters.set_value(key, value)
        for vertex in self.machine_vertices:
            if isinstance(vertex, AbstractRewritesDataSpecification):
                vertex.set_reload_required(True)

    @property
    def weight_scale(self):
        """
        :rtype: float
        """
        return self.__neuron_impl.get_global_weight_scale()

    @property
    def ring_buffer_sigma(self):
        return self.__ring_buffer_sigma

    @ring_buffer_sigma.setter
    def ring_buffer_sigma(self, ring_buffer_sigma):
        self.__ring_buffer_sigma = ring_buffer_sigma

    @property
    def spikes_per_second(self):
        return self.__spikes_per_second

    @spikes_per_second.setter
    def spikes_per_second(self, spikes_per_second):
        self.__spikes_per_second = spikes_per_second

    def set_synapse_dynamics(self, synapse_dynamics):
        """
        :param AbstractSynapseDynamics synapse_dynamics:
        """
        self.synapse_dynamics = synapse_dynamics

    def clear_connection_cache(self):
        """ Flush the cache of connection information; needed for a second run
        """
        for post_vertex in self.machine_vertices:
            post_vertex.clear_connection_cache()

    @overrides(AbstractAcceptsIncomingSynapses
               .get_maximum_delay_supported_in_ms)
    def get_maximum_delay_supported_in_ms(self, machine_time_step):
        return get_maximum_delay_supported_in_ms(
            machine_time_step)

    @overrides(AbstractProvidesOutgoingPartitionConstraints.
               get_outgoing_partition_constraints)
    def get_outgoing_partition_constraints(self, partition):
        """ Gets the constraints for partitions going out of this vertex.

        :param partition: the partition that leaves this vertex
        :return: list of constraints
        """
        return [ContiguousKeyRangeContraint()]

    @overrides(
        AbstractNeuronRecordable.clear_recording)
    def clear_recording(self, variable, buffer_manager, placements):
        if variable == NeuronRecorder.SPIKES:
            index = len(self.__neuron_impl.get_recordable_variables())
        else:
            index = (
                self.__neuron_impl.get_recordable_variable_index(variable))
        self._clear_recording_region(buffer_manager, placements, index)

    @overrides(AbstractSpikeRecordable.clear_spike_recording)
    def clear_spike_recording(self, buffer_manager, placements):
        self._clear_recording_region(
            buffer_manager, placements,
            len(self.__neuron_impl.get_recordable_variables()))

    def _clear_recording_region(
            self, buffer_manager, placements, recording_region_id):
        """ Clear a recorded data region from the buffer manager.

        :param buffer_manager: the buffer manager object
        :param placements: the placements object
        :param recording_region_id: the recorded region ID for clearing
        :rtype: None
        """
        for machine_vertex in self.machine_vertices:
            placement = placements.get_placement_of_vertex(machine_vertex)
            buffer_manager.clear_recorded_data(
                placement.x, placement.y, placement.p, recording_region_id)

    @overrides(AbstractContainsUnits.get_units)
    def get_units(self, variable):
        if variable == NeuronRecorder.SPIKES:
            return NeuronRecorder.SPIKES
        if variable == NeuronRecorder.PACKETS:
            return "count"
        if self.__neuron_impl.is_recordable(variable):
            return self.__neuron_impl.get_recordable_units(variable)
        if variable not in self._parameters:
            raise Exception("Population {} does not have parameter {}".format(
                self.__neuron_impl.model_name, variable))
        return self.__neuron_impl.get_units(variable)

    def describe(self):
        """ Get a human-readable description of the cell or synapse type.

        The output may be customised by specifying a different template\
        together with an associated template engine\
        (see :py:mod:`pyNN.descriptions`).

        If template is None, then a dictionary containing the template context\
        will be returned.

        :rtype: dict(str, ...)
        """
        parameters = dict()
        for parameter_name in self.__pynn_model.default_parameters:
            parameters[parameter_name] = self.get_value(parameter_name)

        context = {
            "name": self.__neuron_impl.model_name,
            "default_parameters": self.__pynn_model.default_parameters,
            "default_initial_values": self.__pynn_model.default_parameters,
            "parameters": parameters,
        }
        return context

    def get_synapse_id_by_target(self, target):
        return self.__neuron_impl.get_synapse_id_by_target(target)

    def __str__(self):
        return "{} with {} atoms".format(self.label, self.n_atoms)

    def __repr__(self):
        return self.__str__()

    @overrides(AbstractCanReset.reset_to_first_timestep)
    def reset_to_first_timestep(self):
        # Mark that reset has been done, and reload state variables
        self.__has_reset_last = True
        for vertex in self.machine_vertices:
            if isinstance(vertex, AbstractRewritesDataSpecification):
                vertex.set_reload_required(True)

        # If synapses change during the run,
        if (self.__synapse_dynamics is not None and
                self.__synapse_dynamics.changes_during_run):
            self.__change_requires_data_generation = True
            for vertex in self.machine_vertices:
                if isinstance(vertex, AbstractRewritesDataSpecification):
                    vertex.set_reload_required(False)

    @staticmethod
    def _ring_buffer_expected_upper_bound(
            weight_mean, weight_std_dev, spikes_per_second,
            machine_timestep, n_synapses_in, sigma):
        """ Provides expected upper bound on accumulated values in a ring\
            buffer element.

        Requires an assessment of maximum Poisson input rate.

        Assumes knowledge of mean and SD of weight distribution, fan-in\
        and timestep.

        All arguments should be assumed real values except n_synapses_in\
        which will be an integer.

        :param float weight_mean: Mean of weight distribution (in either nA or\
            microSiemens as required)
        :param float weight_std_dev: SD of weight distribution
        :param float spikes_per_second: Maximum expected Poisson rate in Hz
        :param int machine_timestep: in us
        :param int n_synapses_in: No of connected synapses
        :param float sigma: How many SD above the mean to go for upper bound;\
            a good starting choice is 5.0. Given length of simulation we can\
            set this for approximate number of saturation events.
        :rtype: float
        """
        # E[ number of spikes ] in a timestep
        steps_per_second = MICRO_TO_SECOND_CONVERSION / machine_timestep
        average_spikes_per_timestep = (
            float(n_synapses_in * spikes_per_second) / steps_per_second)

        # Exact variance contribution from inherent Poisson variation
        poisson_variance = average_spikes_per_timestep * (weight_mean ** 2)

        # Upper end of range for Poisson summation required below
        # upper_bound needs to be an integer
        upper_bound = int(round(average_spikes_per_timestep +
                                POSSION_SIGMA_SUMMATION_LIMIT *
                                math.sqrt(average_spikes_per_timestep)))

        # Closed-form exact solution for summation that gives the variance
        # contributed by weight distribution variation when modulated by
        # Poisson PDF.  Requires scipy.special for gamma and incomplete gamma
        # functions. Beware: incomplete gamma doesn't work the same as
        # Mathematica because (1) it's regularised and needs a further
        # multiplication and (2) it's actually the complement that is needed
        # i.e. 'gammaincc']

        weight_variance = 0.0

        if weight_std_dev > 0:
            # pylint: disable=no-member
            lngamma = special.gammaln(1 + upper_bound)
            gammai = special.gammaincc(
                1 + upper_bound, average_spikes_per_timestep)

            big_ratio = (math.log(average_spikes_per_timestep) * upper_bound -
                         lngamma)

            if -701.0 < big_ratio < 701.0 and big_ratio != 0.0:
                log_weight_variance = (
                    -average_spikes_per_timestep +
                    math.log(average_spikes_per_timestep) +
                    2.0 * math.log(weight_std_dev) +
                    math.log(math.exp(average_spikes_per_timestep) * gammai -
                             math.exp(big_ratio)))
                weight_variance = math.exp(log_weight_variance)

        # upper bound calculation -> mean + n * SD
        return ((average_spikes_per_timestep * weight_mean) +
                (sigma * math.sqrt(poisson_variance + weight_variance)))

    def _get_ring_buffer_to_input_left_shifts(self, machine_timestep):
        """ Get the scaling of the ring buffer to provide as much accuracy as\
            possible without too much overflow

        :param .MachineVertex machine_vertex:
        :param .MachineGraph machine_graph:
        :param int machine_timestep:
        :param float weight_scale:
        :rtype: list(int)
        """
        weight_scale = self.__neuron_impl.get_global_weight_scale()
        weight_scale_squared = weight_scale * weight_scale
        n_synapse_types = self.__neuron_impl.get_n_synapse_types()
        running_totals = [RunningStats() for _ in range(n_synapse_types)]
        delay_running_totals = [RunningStats() for _ in range(n_synapse_types)]
        total_weights = numpy.zeros(n_synapse_types)
        biggest_weight = numpy.zeros(n_synapse_types)
        weights_signed = False
        rate_stats = [RunningStats() for _ in range(n_synapse_types)]
        steps_per_second = MICRO_TO_SECOND_CONVERSION / machine_timestep

        for proj in self.__incoming_projections:
            synapse_info = proj._synapse_information
            synapse_type = synapse_info.synapse_type
            synapse_dynamics = synapse_info.synapse_dynamics
            connector = synapse_info.connector

            weight_mean = (
                synapse_dynamics.get_weight_mean(
                    connector, synapse_info) * weight_scale)
            n_connections = \
                connector.get_n_connections_to_post_vertex_maximum(
                    synapse_info)
            weight_variance = synapse_dynamics.get_weight_variance(
                connector, synapse_info.weights) * weight_scale_squared
            running_totals[synapse_type].add_items(
                weight_mean, weight_variance, n_connections)

            delay_variance = synapse_dynamics.get_delay_variance(
                connector, synapse_info.delays)
            delay_running_totals[synapse_type].add_items(
                0.0, delay_variance, n_connections)

            weight_max = (synapse_dynamics.get_weight_maximum(
                connector, synapse_info) * weight_scale)
            biggest_weight[synapse_type] = max(
                biggest_weight[synapse_type], weight_max)

            spikes_per_tick = max(
                1.0, self.__spikes_per_second / steps_per_second)
            spikes_per_second = self.__spikes_per_second
            pre_vertex = proj._projection_edge.pre_vertex
            if isinstance(pre_vertex, AbstractMaxSpikes):
                rate = pre_vertex.max_spikes_per_second()
                if rate != 0:
                    spikes_per_second = rate
                spikes_per_tick = \
                    pre_vertex.max_spikes_per_ts(machine_timestep)
            rate_stats[synapse_type].add_items(
                spikes_per_second, 0, n_connections)
            total_weights[synapse_type] += spikes_per_tick * (
                weight_max * n_connections)

            if synapse_dynamics.are_weights_signed():
                weights_signed = True

        max_weights = numpy.zeros(n_synapse_types)
        for synapse_type in range(n_synapse_types):
            if delay_running_totals[synapse_type].variance == 0.0:
                max_weights[synapse_type] = max(total_weights[synapse_type],
                                                biggest_weight[synapse_type])
            else:
                stats = running_totals[synapse_type]
                rates = rate_stats[synapse_type]
                max_weights[synapse_type] = min(
                    self._ring_buffer_expected_upper_bound(
                        stats.mean, stats.standard_deviation, rates.mean,
                        machine_timestep, stats.n_items,
                        self.__ring_buffer_sigma),
                    total_weights[synapse_type])
                max_weights[synapse_type] = max(
                    max_weights[synapse_type], biggest_weight[synapse_type])

        # Convert these to powers; we could use int.bit_length() for this if
        # they were integers, but they aren't...
        max_weight_powers = (
            0 if w <= 0 else int(math.ceil(max(0, math.log(w, 2))))
            for w in max_weights)

        # If 2^max_weight_power equals the max weight, we have to add another
        # power, as range is 0 - (just under 2^max_weight_power)!
        max_weight_powers = (
            w + 1 if (2 ** w) <= a else w
            for w, a in zip(max_weight_powers, max_weights))

        # If we have synapse dynamics that uses signed weights,
        # Add another bit of shift to prevent overflows
        if weights_signed:
            max_weight_powers = (m + 1 for m in max_weight_powers)

        return list(max_weight_powers)

    def get_ring_buffer_shifts(self, machine_timestep):
        if self.__ring_buffer_shifts is None:
            self.__ring_buffer_shifts = \
                self._get_ring_buffer_to_input_left_shifts(machine_timestep)
        return self.__ring_buffer_shifts

    @staticmethod
    def __get_weight_scale(ring_buffer_to_input_left_shift):
        """ Return the amount to scale the weights by to convert them from \
            floating point values to 16-bit fixed point numbers which can be \
            shifted left by ring_buffer_to_input_left_shift to produce an\
            s1615 fixed point number

        :param int ring_buffer_to_input_left_shift:
        :rtype: float
        """
        return float(math.pow(2, 16 - (ring_buffer_to_input_left_shift + 1)))

    def get_weight_scales(self, machine_timestep):
        if self.__weight_scales is None:
            ring_buffer_shifts = self.get_ring_buffer_shifts(machine_timestep)
            weight_scale = self.__neuron_impl.get_global_weight_scale()
            self.__weight_scales = numpy.array([
                self.__get_weight_scale(r) * weight_scale
                for r in ring_buffer_shifts])
        return self.__weight_scales

    @overrides(AbstractAcceptsIncomingSynapses.get_connections_from_machine)
    def get_connections_from_machine(
            self, transceiver, placements, app_edge, synapse_info):
        """ Read the connections from the machine for a given projection.

        :param ~spinnman.transciever.Transceiver transceiver:
            Used to read the data from the machine
        :param ~pacman.model.placements.Placements placements:
            Where the vertices are on the machine
        :param ProjectionApplicationEdge app_edge:
            The application edge of the projection
        :param SynapseInformation synapse_info:
            The synapse information of the projection
        :return: The connections from the machine, with dtype
            AbstractSynapseDynamics.NUMPY_CONNECTORS_DTYPE
        :rtype: ~numpy.ndarray
        """

        # TODO: Make sure this only contains neuron vertices
        post_vertices = self.machine_vertices

        # Start with something in the list so that concatenate works
        connections = [numpy.zeros(
                0, dtype=AbstractSynapseDynamics.NUMPY_CONNECTORS_DTYPE)]
        progress = ProgressBar(
            len(post_vertices),
            "Getting synaptic data between {} and {}".format(
                app_edge.pre_vertex.label, app_edge.post_vertex.label))
        for post_vertex in progress.over(post_vertices):
            placement = placements.get_placement_of_vertex(post_vertex)
            connections.extend(post_vertex.get_connections_from_machine(
                transceiver, placement, app_edge, synapse_info))
        return numpy.concatenate(connections)

    def get_synapse_params_size(self):
        """
        :rtype: int
        """
        return (_SYNAPSES_BASE_SDRAM_USAGE_IN_BYTES +
                (BYTES_PER_WORD * self.__neuron_impl.get_n_synapse_types()))

    def get_synapse_dynamics_size(self, vertex_slice):
        """ Get the size of the synapse dynamics region

        :param ~pacman.model.graphs.common.Slice vertex_slice:
            The slice of the vertex to get the usage of
        :rtype: int
        """
        if self.__synapse_dynamics is None:
            return 0

        return self.__synapse_dynamics.get_parameters_sdram_usage_in_bytes(
            vertex_slice.n_atoms, self.__neuron_impl.get_n_synapse_types())

    def get_structural_dynamics_size(self, vertex_slice):
        """ Get the size of the structural dynamics region

        :param ~pacman.model.graphs.common.Slice vertex_slice:
            The slice of the vertex to get the usage of
        """
        if self.__synapse_dynamics is None:
            return 0

        if not isinstance(
                self.__synapse_dynamics, AbstractSynapseDynamicsStructural):
            return 0

        return self.__synapse_dynamics\
            .get_structural_parameters_sdram_usage_in_bytes(
                self.__incoming_projections, vertex_slice.n_atoms)

    def get_synapses_size(self, vertex_slice):
        """ Get the maximum SDRAM usage for the synapses on a vertex slice

        :param ~pacman.model.graphs.common.Slice vertex_slice:
            The slice of the vertex to get the usage of
        """
        addr = 2 * BYTES_PER_WORD
        for proj in self.__incoming_projections:
            addr = self.__add_matrix_size(addr, proj, vertex_slice)
        return addr

    @staticmethod
    def __add_matrix_size(addr, projection, vertex_slice):
        """ Add the size of the matrices for the projection to the vertex slice
            to the address

        :param int addr: The address to start from
        :param ~spynnaker.pyNN.models.pynn_projection_common\
            .PyNNProjectionCommon projection: The projection to add
        :param ~pacman.model.graphs.common.Slice vertex_slice:
            The slice projected to
        :rtype: int
        """
        synapse_info = projection._synapse_information
        app_edge = projection._projection_edge

        max_row_info = get_max_row_info(
            synapse_info, vertex_slice, app_edge.n_delay_stages,
            globals_variables.get_simulator().machine_time_step, app_edge)

        vertex = app_edge.pre_vertex
        n_sub_atoms = int(min(vertex.get_max_atoms_per_core(), vertex.n_atoms))
        n_sub_edges = int(math.ceil(vertex.n_atoms / n_sub_atoms))

        if max_row_info.undelayed_max_n_synapses > 0:
            size = n_sub_atoms * max_row_info.undelayed_max_bytes
            for _ in range(n_sub_edges):
                addr = MasterPopTableAsBinarySearch.get_next_allowed_address(
                    addr)
                addr += size
        if max_row_info.delayed_max_n_synapses > 0:
            size = (n_sub_atoms * max_row_info.delayed_max_bytes *
                    app_edge.n_delay_stages)
            for _ in range(n_sub_edges):
                addr = MasterPopTableAsBinarySearch.get_next_allowed_address(
                    addr)
                addr += size
        return addr

    def get_pop_table_size(self):
        """ Get the size of the master population table in bytes

        :rtype: int
        """
        return MasterPopTableAsBinarySearch.get_master_population_table_size(
            self.__incoming_projections)

    def get_synapse_expander_size(self):
        """ Get the size of the synapse expander region in bytes

        :rtype: int
        """
        size = 0
        for proj in self.__incoming_projections:
            synapse_info = proj._synapse_information
            app_edge = proj._projection_edge
            n_sub_edges = len(app_edge.pre_vertex.machine_vertices)
            if not n_sub_edges:
                vertex = app_edge.pre_vertex
                max_atoms = float(min(vertex.get_max_atoms_per_core(),
                                      vertex.n_atoms))
                n_sub_edges = int(math.ceil(vertex.n_atoms / max_atoms))
            size += self.__generator_info_size(synapse_info) * n_sub_edges

        # If anything generates data, also add some base information
        if size:
            size += SYNAPSES_BASE_GENERATOR_SDRAM_USAGE_IN_BYTES
            size += self.__neuron_impl.get_n_synapse_types() * BYTES_PER_WORD
        return size

    @staticmethod
    def __generator_info_size(synapse_info):
        """ The number of bytes required by the generator information

        :rtype: int
        """
        if not synapse_info.may_generate_on_machine():
            return 0

        connector = synapse_info.connector
        dynamics = synapse_info.synapse_dynamics
        gen_size = sum((
            GeneratorData.BASE_SIZE,
            connector.gen_delay_params_size_in_bytes(synapse_info.delays),
            connector.gen_weight_params_size_in_bytes(synapse_info.weights),
            connector.gen_connector_params_size_in_bytes,
            dynamics.gen_matrix_params_size_in_bytes
        ))
        return gen_size
