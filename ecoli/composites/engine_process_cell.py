import copy
import sys

from vivarium.core.composer import Composer
from vivarium.core.engine import Engine
from vivarium.library.topology import get_in, assoc_path
from vivarium.library.dict_utils import deep_merge_check

from ecoli.experiments.ecoli_master_sim import (
    EcoliSim,
    SimConfig,
    get_git_revision_hash,
    get_git_status,
)
from ecoli.library.sim_data import RAND_MAX
from ecoli.processes.engine_process import EngineProcess
from ecoli.processes.listeners.mass_listener import MassListener
from ecoli.composites.environment.lattice import Lattice


class EngineProcessCell(Composer):

    defaults = {
        'agent_id': '0',
        'initial_cell_state': {},
        'seed': 0,
        'initial_tunnel_states': {},
        'tunnel_out_schemas': {},
        'tunnel_in_schemas': {},
        'parallel': False,
        'ecoli_sim_config': {},
        'divide': False,
        'division_threshold': 0,
        'division_variable': tuple(),
    }

    def generate_processes(self, config):
        agent_id = config['agent_id']
        self.ecoli_sim = EcoliSim({
            **config['ecoli_sim_config'],
            'seed': config['seed'],
            'agent_id': agent_id,
            'divide': False,  # Division is handled by EngineProcess.
            'spatial_environment': False,
        })
        self.ecoli_sim.build_ecoli()
        initial_inner_state = (
            config['initial_cell_state']
            or self.ecoli_sim.initial_state)
        cell_process = EngineProcess({
            'agent_id': agent_id,
            'composer': self,
            'composite': self.ecoli_sim.ecoli,
            'initial_inner_state': initial_inner_state,
            'tunnels_in': {
                'mass_tunnel': (
                    ('listeners', 'mass'),
                    MassListener({
                        'submass_indices': {
                            key: None
                            for key in [
                                'rna', 'rRna', 'tRna', 'mRna', 'dna',
                                'protein', 'smallMolecule']
                        }
                    }).ports_schema()['listeners']['mass'],
                ),
                'boundary_tunnel': (
                    ('boundary',),
                    config['tunnel_in_schemas']['boundary_tunnel'],
                ),
            },
            'tunnel_out_schemas': config['tunnel_out_schemas'],
            'seed': (config['seed'] + 1) % RAND_MAX,
            'divide': config['divide'],
            'division_threshold': config['division_threshold'],
            'division_variable': config['division_variable'],
            '_parallel': config['parallel'],
        })
        return {
            'cell_process': cell_process,
        }

    def generate_topology(self, config):
        return {
            'cell_process': {
                'mass_tunnel': ('listeners', 'mass'),
                'agents': ('..', '..'),
                'fields_tunnel': ('..', '..', 'fields'),
                'boundary_tunnel': ('boundary',),
                'dimensions_tunnel': ('..', '..', 'dimensions'),
            },
        }

    def initial_state(self, config):
        merged_config = copy.deepcopy(self.config)
        merged_config.update(config)

        mass_listener_path = ('listeners', 'mass')

        mass_listener_state = merged_config['initial_tunnel_states'].get(
            'mass_tunnel',
            get_in(
                self.ecoli_sim.initial_state,
                mass_listener_path
            ),
        )
        initial_state = assoc_path(
            {}, mass_listener_path, mass_listener_state)
        return initial_state


def run_simulation():
    config = SimConfig()
    config.update_from_cli()

    tunnel_out_schemas = {}
    tunnel_in_schemas = {}
    if config['spatial_environment']:
        # Generate environment composite.
        environment_composer = Lattice(
            config['spatial_environment_config'])
        environment_composite = environment_composer.generate()
        diffusion_schema = environment_composite.processes[
            'diffusion'].get_schema()
        tunnel_out_schemas['fields_tunnel'] = diffusion_schema['fields']
        tunnel_out_schemas['dimensions_tunnel'] = diffusion_schema[
            'dimensions']
        tunnel_in_schemas['boundary_tunnel'] = diffusion_schema[
            'agents']['*']['boundary']

    composer = EngineProcessCell({
        'agent_id': config['agent_id'],
        'parallel': config['parallel'],
        'ecoli_sim_config': config.to_dict(),
        'divide': config['divide'],
        'division_threshold': config['division']['threshold'],
        'division_variable': ('listeners', 'mass', 'cell_mass'),
        'tunnel_in_schemas': tunnel_in_schemas,
    })
    composite = composer.generate(path=('agents', config['agent_id']))
    initial_state = {
        'agents': {
            config['agent_id']: composer.initial_state({}),
        },
    }

    if config['spatial_environment']:
        # Merge a lattice composite for the spatial environment.
        initial_environment = environment_composite.initial_state()
        composite.merge(environment_composite)
        initial_state = deep_merge_check(initial_state, initial_environment)

    metadata = config.to_dict()
    metadata.pop('initial_state', None)
    metadata['git_hash'] = get_git_revision_hash()
    metadata['git_status'] = get_git_status()

    emitter_config = {'type': config['emitter']}
    for key, value in config['emitter_arg']:
        emitter_config[key] = value
    engine = Engine(
        processes=composite.processes,
        topology=composite.topology,
        initial_state=initial_state,
        emitter=emitter_config,
        progress_bar=config['progress_bar'],
        metadata=metadata,
        profile=config['profile'],
    )
    engine.update(config['total_time']),
    engine.end()

    if config['profile']:
        report_profiling(engine.stats)

if __name__ == '__main__':
    run_simulation()
