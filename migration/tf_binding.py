"""
tests that vivarium-ecoli tf_binding process update is the same as saved wcEcoli updates
"""
import pytest

from ecoli.processes.tf_binding import TfBinding
from migration.migration_utils import run_and_compare

@pytest.mark.master
def test_tf_binding_migration():
    times = [0, 2104]
    for initial_time in times:
        run_and_compare(initial_time, TfBinding)

if __name__ == "__main__":
    test_tf_binding_migration()
