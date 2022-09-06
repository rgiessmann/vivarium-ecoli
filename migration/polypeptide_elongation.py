"""
tests that vivarium-ecoli polypeptide_elongation process update is the same as saved wcEcoli updates
"""
import json
import pytest

from ecoli.library.sim_data import LoadSimData
from ecoli.composites.ecoli_nonpartition import SIM_DATA_PATH
from ecoli.processes.polypeptide_elongation import PolypeptideElongation
from migration.migration_utils import (run_custom_partitioned_process, 
                                       recursive_compare)
from ecoli.states.wcecoli_state import get_state_from_file

load_sim_data = LoadSimData(
            sim_data_path=SIM_DATA_PATH,
            seed=0)

TOPOLOGY = PolypeptideElongation.topology

@pytest.mark.master
def test_polypeptide_elongation_migration():
    def test(initial_time):
        # Set time parameters
        total_time = 2
        initial_time = initial_time

        # Create process, experiment, loading in initial state from file.
        config = load_sim_data.get_polypeptide_elongation_config()
        polypeptide_elongation_process = PolypeptideElongation(config)

        initial_state = get_state_from_file(
            path=f'data/migration/wcecoli_t{initial_time}_before_layer_0.json')

        # run the process and get an update
        actual_request, actual_update = run_custom_partitioned_process(
            polypeptide_elongation_process, TOPOLOGY, initial_time = initial_time, 
            initial_state=initial_state, folder_name='polypeptide_elongation')

        with open(
            f"data/migration/polypeptide_elongation/request_t{total_time+initial_time}.json",
            'r'
        ) as f:
            wc_request = json.load(f)
        # Ignore differences in unique IDs
        assert recursive_compare(actual_request, wc_request)
        
        with open(f"data/migration/polypeptide_elongation/update_t{total_time+initial_time}.json") as f:
            wc_update = json.load(f)
        assert recursive_compare(actual_update, wc_update)

    times = [0, 2072]
    for initial_time in times:
        test(initial_time)

if __name__ == "__main__":
    test_polypeptide_elongation_migration()
