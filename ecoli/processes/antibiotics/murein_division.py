import numpy as np

from vivarium.core.process import Step

from ecoli.library.schema import bulk_schema
from ecoli.processes.registries import topology_registry

# Register default topology for this process, associating it with process name
NAME = "murein-division"
TOPOLOGY = {
    "total_murein": ("bulk",),
    "murein_state": ("murein_state",),
    "wall_state": ("wall_state",),
    "first_update": ("deriver_skips", "murein_division",)
}
topology_registry.register(NAME, TOPOLOGY)


class MureinDivision(Step):
    """
    Ensures that total murein count in bulk store matches that from division of
    murein_state store before running mass listener
    """
    name = NAME
    topology = TOPOLOGY

    defaults = {
        "murein_name": "CPD-12261[p]",
    }

    def __init__(self, parameters=None):
        super().__init__(parameters)
        self.murein = self.parameters["murein_name"]
        self.first_timestep = True

    def ports_schema(self):
        return {
            "total_murein": bulk_schema([self.parameters["murein_name"]]),
            "murein_state": {
                "incorporated_murein": {
                    "_default": 0,
                    "_updater": "set",
                    "_emit": True,
                },
                "unincorporated_murein": {
                    "_default": 0,
                    "_emit": True,
                },
                "shadow_murein": {
                    "_default": 0,
                    "_emit": True
                },
            },
            "wall_state": {
                "lattice": {
                    "_default": None,
                    "_updater": "set",
                    "_emit": False,
                }
            },
            "first_update": {
                "_default": True,
                "_updater": "set",
                "_divider": {"divider": "set_value", "config": {"value": True}},
            }
        }

    def next_update(self, timestep, states):
        update = {"murein_state": {}, "total_murein": {}}
        # Ensure that lattice is a numpy array so divider works properly.
        # Used when loading from a saved state.
        if ((not isinstance(states["wall_state"]["lattice"], np.ndarray)) and 
            (states["wall_state"]["lattice"] != None)):
            update["wall_state"] = {
                "lattice": np.array(states["wall_state"]["lattice"])
            }
        # Only run right after division (cell has half of mother lattice)
        # TODO: Calculate porosity, hole size/strand length dists
        # Note: This mechanism does not perfectly conserve murein mass between
        # mother and daughter cells (can at most gain the mass of 1 CPD-12261).
        if states["first_update"] and states["wall_state"]["lattice"] is not None:
            accounted_murein_monomers = sum(states["murein_state"].values())
            # When run in an EngineProcess, this Step sets the incorporated
            # murein count before CellWall or PBPBinding run after division
            if states["murein_state"]["incorporated_murein"] == 0:
                incorporated_murein = np.sum(states["wall_state"]["lattice"])
                update["murein_state"][
                    "incorporated_murein"] = incorporated_murein
                accounted_murein_monomers += incorporated_murein
            remainder = accounted_murein_monomers % 4
            if remainder != 0:
                # Bulk murein is a tetramer. Add extra unincorporated murein
                # monomers until divisible by 4
                update["murein_state"]["unincorporated_murein"] = 4 - remainder
                accounted_murein_monomers += 4 - remainder
            accounted_murein = accounted_murein_monomers // 4
            if accounted_murein != states["total_murein"][
                self.parameters["murein_name"]]:
                update["total_murein"][self.parameters["murein_name"]] = (
                    accounted_murein - states["total_murein"][
                        self.parameters["murein_name"]])
        update["first_update"] = False
        return update