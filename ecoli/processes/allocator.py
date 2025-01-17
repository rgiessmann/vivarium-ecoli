"""
=========
Allocator
=========

Reads requests from PartionedProcesses, and allocates molecules according to
process priorities.
"""
import numpy as np
from vivarium.core.process import Deriver

from ecoli.processes.registries import topology_registry
from ecoli.library.schema import (counts, numpy_schema, bulk_name_to_idx,
    listener_schema)

# Register default topology for this process, associating it with process name
NAME = 'allocator'
TOPOLOGY = {
    'request': ('request',),
    'allocate': ('allocate',),
    'bulk': ('bulk',),
    'evolvers_ran': ('evolvers_ran',),
    'listeners': ('listeners',)
}
topology_registry.register(NAME, TOPOLOGY)

ASSERT_POSITIVE_COUNTS = True

class NegativeCountsError(Exception):
	pass

class Allocator(Deriver):
    """ Allocator Deriver """
    name = NAME
    topology = TOPOLOGY

    defaults = {}

    processes = {}

    # Constructor
    def __init__(self, parameters=None):
        super().__init__(parameters)
        self.moleculeNames = self.parameters['molecule_names']
        self.n_molecules = len(self.moleculeNames)
        self.mol_name_to_idx = {name: idx
            for idx, name in enumerate(self.moleculeNames)}
        self.mol_idx_to_name = {idx: name
            for idx, name in enumerate(self.moleculeNames)}
        self.processNames = self.parameters['process_names']
        self.n_processes = len(self.processNames)
        self.proc_name_to_idx = {name: idx
            for idx, name in enumerate(self.processNames)}
        self.proc_idx_to_name = {idx: name
            for idx, name in enumerate(self.processNames)}
        self.processPriorities = np.zeros(len(self.processNames))
        for process, custom_priority in self.parameters['custom_priorities'].items():
            if process not in self.proc_name_to_idx.keys():
                continue
            self.processPriorities[self.proc_name_to_idx[process]] = custom_priority
        self.seed = self.parameters['seed']
        self.random_state = np.random.RandomState(seed = self.seed)

        # Helper indices for Numpy indexing
        self.molecule_idx = None

    def ports_schema(self):
        ports = {
            'bulk': numpy_schema('bulk'),
            'request': {
                process: {
                    'bulk': {'_default': [], '_emit': False,
                        '_divider': 'null', '_updater': 'set'}}
                for process in self.processNames},
            'allocate': {
                process: {
                    'bulk': {'_default': [], '_emit': False,
                        '_divider': 'null', '_updater': 'set'}
                }
                for process in self.processNames},
            'evolvers_ran': {
                '_default': True,
            },
            'listeners': listener_schema({
                # Requests and initial allocations are one timestep "behind"
                # Example: counts at t=4 go towards update at t=6
                'atp_requested': [],
                'atp_allocated_initial': [],
                # Use blame functionality to get ATP consumed per process
                # 'atp_allocated_final': []
            })
        }
        return ports

    def update_condition(self, timestep, states):
        return states['evolvers_ran']

    def next_update(self, timestep, states):
        if self.molecule_idx is None:
            self.molecule_idx = bulk_name_to_idx(self.moleculeNames,
                states['bulk']['id'])
            self.atp_idx = bulk_name_to_idx('ATP[c]', states['bulk']['id'])
        total_counts = counts(states['bulk'], self.molecule_idx)
        original_totals = total_counts.copy()
        counts_requested = np.zeros((self.n_molecules, self.n_processes),
            dtype=int)
        for process in states['request']:
            proc_idx = self.proc_name_to_idx[process]
            for req_idx, req in states['request'][process]['bulk']:
                counts_requested[req_idx, proc_idx] = req

        if ASSERT_POSITIVE_COUNTS and np.any(counts_requested < 0):
            raise NegativeCountsError(
                "Negative value(s) in counts_requested:\n"
                + "\n".join(
                    "{} in {} ({})".format(
                        self.mol_idx_to_name[molIndex],
                        self.proc_idx_to_name[processIndex],
                        counts_requested[molIndex, processIndex]
                        )
                    for molIndex, processIndex in zip(
                        *np.where(counts_requested < 0))
                    )
                )

        # Calculate partition
        partitioned_counts = calculatePartition(
            self.processPriorities,
            counts_requested,
            total_counts,
            self.random_state
            )

        partitioned_counts.astype(int, copy=False)

        if ASSERT_POSITIVE_COUNTS and np.any(partitioned_counts < 0):
            raise NegativeCountsError(
                    "Negative value(s) in partitioned_counts:\n"
                    + "\n".join(
                    "{} in {} ({})".format(
                        self.mol_idx_to_name[molIndex],
                        self.proc_idx_to_name[processIndex],
                        counts_requested[molIndex, processIndex]
                        )
                    for molIndex, processIndex in zip(*np.where(
                        partitioned_counts < 0))
                    )
                )

        # Ensure we are not overdrafting any molecules
        counts_unallocated = original_totals - np.sum(
            partitioned_counts, axis=-1)

        if ASSERT_POSITIVE_COUNTS and np.any(counts_unallocated < 0):
            raise NegativeCountsError(
                    "Negative value(s) in counts_unallocated:\n"
                    + "\n".join(
                    "{} ({})".format(
                        self.mol_idx_to_name[molIndex],
                        counts_unallocated[molIndex]
                        )
                    for molIndex in np.where(counts_unallocated < 0)[0]
                    )
                )


        update = {
            'request': {
                process: {
                    'bulk': []}
                for process in states['request']},
            'allocate': {
                process: {
                    'bulk': partitioned_counts[
                        :, self.proc_name_to_idx[process]]}
                for process in states['request']
            },
            'evolvers_ran': False,
            'listeners': {
                'atp_requested': counts_requested[self.atp_idx, :],
                'atp_allocated_initial': partitioned_counts[self.atp_idx, :]
            }
        }

        return update

def calculatePartition(process_priorities, counts_requested, total_counts,
    random_state
):
    priorityLevels = np.sort(np.unique(process_priorities))[::-1]

    partitioned_counts = np.zeros_like(counts_requested)

    for priorityLevel in priorityLevels:
        processHasPriority = (priorityLevel == process_priorities)

        requests = counts_requested[:, processHasPriority].copy()

        total_requested = requests.sum(axis=1)
        excess_request_mask = (total_requested > total_counts) & (
            total_requested > 0)

        # Get fractional request for molecules that have excess request
        # compared to available counts
        fractional_requests = (
            requests[excess_request_mask, :] * total_counts[
                excess_request_mask, np.newaxis]
            / total_requested[excess_request_mask, np.newaxis]
            )

        # requests_counts_product = requests[excess_request_mask, :] * total_counts[excess_request_mask, np.newaxis]
        # requests_with_mask = total_requested[excess_request_mask, np.newaxis]
        # test = np.true_divide(requests_counts_product, requests_with_mask,
        #                  out=np.zeros_like(requests_counts_product), where=requests_with_mask != [0], casting='unsafe')
        # TODO(Matt): Incorporate this fix into the line of code above. Commented code a start, but only returns ints.
        # TODO: This doesn't seem necessary. Monitor to ensure nothing breaks.
        # for lst_index in range(len(fractional_requests)):
        #     for value_index in range(len(fractional_requests[lst_index])):
        #         if np.isnan(fractional_requests[lst_index][value_index]):
        #             fractional_requests[lst_index][value_index] = 0

        # Distribute fractional counts to ensure full allocation of excess
        # request molecules
        remainders = fractional_requests % 1
        options = np.arange(remainders.shape[1])
        for idx, remainder in enumerate(remainders):
            total_remainder = remainder.sum()
            count = int(np.round(total_remainder))
            if count > 0:
                allocated_indices = random_state.choice(options, size=count,
                    p=remainder/total_remainder, replace=False)
                fractional_requests[idx, allocated_indices] += 1
        requests[excess_request_mask, :] = fractional_requests

        allocations = requests.astype(np.int64)
        partitioned_counts[:, processHasPriority] = allocations
        total_counts -= allocations.sum(axis=1)
    return partitioned_counts
