"""
==========
Metabolism
==========

Encodes molecular simulation of microbial metabolism using flux-balance analysis.

This process demonstrates how metabolites are taken up from the environment
and converted into other metabolites for use in other processes.

NOTE:
- In wcEcoli, metabolism only runs after all other processes have completed
and internal states have been updated (deriver-like, no partitioning necessary)
"""

from typing import Tuple

import numpy as np
from scipy.sparse import csr_matrix
from six.moves import zip
from vivarium.core.process import Step

from ecoli.processes.registries import topology_registry
from ecoli.library.schema import (
    numpy_schema, bulk_name_to_idx, counts, listener_schema)
from wholecell.utils import units
from wholecell.utils.random import stochasticRound
from wholecell.utils.modular_fba import FluxBalanceAnalysis


# Register default topology for this process, associating it with process name
NAME = 'ecoli-metabolism'
TOPOLOGY = {
    "bulk": ("bulk",),
    # Non-partitioned counts
    "bulk_total": ("bulk",),
    "listeners": ("listeners",),
    "environment": {
        "_path": ("environment",),
        "exchange": ("exchange",),
    },
    "polypeptide_elongation": ("process_state", "polypeptide_elongation"),
    "evolvers_ran": ('evolvers_ran',),
    "first_update": ("deriver_skips", "metabolism",)
    }
topology_registry.register(NAME, TOPOLOGY)

COUNTS_UNITS = units.mmol
VOLUME_UNITS = units.L
MASS_UNITS = units.g
TIME_UNITS = units.s
CONC_UNITS = COUNTS_UNITS / VOLUME_UNITS
CONVERSION_UNITS = MASS_UNITS * TIME_UNITS / VOLUME_UNITS
GDCW_BASIS = units.mmol / units.g / units.h

USE_KINETICS = True


class Metabolism(Step):
    """ Metabolism Process """

    name = NAME
    topology = TOPOLOGY
    defaults = {
        'get_import_constraints': lambda u, c, p: (u, c, []),
        'nutrientToDoublingTime': {},
        'use_trna_charging': False,
        'include_ppgpp': False,
        'aa_names': [],
        'current_timeline': None,
        'media_id': 'minimal',
        'condition': 'basal',
        'nutrients': [],
        'metabolism': {},
        'non_growth_associated_maintenance': 8.39 * units.mmol / (
            units.g * units.h),
        'avogadro': 6.02214076e+23 / units.mol,
        'cell_density': 1100 * units.g / units.L,
        'dark_atp': 33.565052868380675 * units.mmol / units.g,
        'cell_dry_mass_fraction': 0.3,
        'get_biomass_as_concentrations': lambda doubling_time: {},
        'ppgpp_id': 'ppgpp',
        'get_ppGpp_conc': lambda media: 0.0,
        'exchange_data_from_media': lambda media: [],
        'get_masses': lambda exchanges: [],
        'doubling_time': 44.0 * units.min,
        'amino_acid_ids': {},
        'linked_metabolites': None,
        'seed': 0,
        # TODO: For testing, remove later (perhaps after modifying sim data)
        'reduce_murein_objective': False
    }

    def __init__(self, parameters=None):
        super().__init__(parameters)

        # Use information from the environment and sim
        self.get_import_constraints = self.parameters['get_import_constraints']
        self.nutrientToDoublingTime = self.parameters['nutrientToDoublingTime']
        self.use_trna_charging = self.parameters['use_trna_charging']
        self.include_ppgpp = self.parameters['include_ppgpp']
        self.current_timeline = self.parameters['current_timeline']
        self.media_id = self.parameters['media_id']
        self.ngam = self.parameters['non_growth_associated_maintenance']
        self.gam = parameters['dark_atp'] * parameters['cell_dry_mass_fraction']

        # Create model to use to solve metabolism updates
        self.model = FluxBalanceAnalysisModel(
            self.parameters,
            timeline=self.current_timeline,
            include_ppgpp=self.include_ppgpp)

        # Save constants
        self.nAvogadro = self.parameters['avogadro']
        self.cellDensity = self.parameters['cell_density']

        # Track updated AA concentration targets with tRNA charging
        self.aa_targets = {}
        self.aa_targets_not_updated = {'L-SELENOCYSTEINE[c]'}
        self.aa_names = self.parameters['aa_names']

        # Molecules with concentration updates for listener
        self.linked_metabolites = self.parameters['linked_metabolites']
        doubling_time = self.nutrientToDoublingTime.get(
            self.media_id,
            self.nutrientToDoublingTime[self.media_id],)
        update_molecules = list(self.model.getBiomassAsConcentrations(
            doubling_time).keys())
        if self.use_trna_charging:
            update_molecules += [aa for aa in self.aa_names
                if aa not in self.aa_targets_not_updated]
            update_molecules += list(self.linked_metabolites.keys())
        if self.include_ppgpp:
            update_molecules += [self.model.ppgpp_id]
        self.conc_update_molecules = sorted(update_molecules)

        self.seed = self.parameters['seed']
        self.random_state = np.random.RandomState(seed=self.seed)

        # TODO: For testing, remove later (perhaps after modifying sim data)
        self.reduce_murein_objective = self.parameters[
            'reduce_murein_objective']

        # Helper indices for Numpy indexing
        self.metabolite_idx = None


    def __getstate__(self):
        return self.parameters

    def __setstate__(self, state):
        self.__init__(state)

    def ports_schema(self):
        ports = {
            'bulk': numpy_schema('bulk'),
            'bulk_total': numpy_schema('bulk', partition=False),

            'environment': {
                'media_id': {
                    '_default': '',
                    '_updater': 'set'},
                'exchange': {
                    str(element): {'_default': 0}
                    for element in self.model.fba.getExternalMoleculeIDs()},
                'exchange_data': {
                    'unconstrained': {'_default': []},
                    'constrained': {'_default': []}}}, # this is only GLC[p].

            'listeners': {
                'mass': listener_schema({
                    'cell_mass': 0.0,
                    'dry_mass': 0.0}),

                'fba_results': listener_schema({
                    'media_id': '',
                    'conc_updates': [],
                    'catalyst_counts': [],
                    'translation_gtp': 0,
                    'coefficient': 0.0,
                    'unconstrained_molecules': [],
                    'constrained_molecules': [],
                    # 'uptake_constraints': [],
                    'delta_metabolites': [],
                    'reaction_fluxes': [],
                    'external_exchange_fluxes': [],
                    'objective_value': [],
                    'shadow_prices': [],
                    'reduced_costs': [],
                    'target_concentrations': [],
                    'homeostatic_objective_values': [],
                    'kinetic_objective_values': [],
                    # 'estimated_fluxes': {},
                    # 'estimated_homeostatic_dmdt': {},
                    # 'target_homeostatic_dmdt': {},
                    # 'estimated_exchange_dmdt': {},
                    # 'target_kinetic_fluxes': {},
                    # 'target_kinetic_bounds': {},
                    # 'target_maintenance_flux': 0
                }),

                'enzyme_kinetics': listener_schema({
                    'metabolite_counts_init': 0,
                    'metabolite_counts_final': 0,
                    'enzyme_counts_init': 0,
                    'counts_to_molar': 1.0,
                    'actual_fluxes': [],
                    'target_fluxes': [],
                    'target_fluxes_upper': [],
                    'target_fluxes_lower': []})
            },

            'polypeptide_elongation': {
                'aa_count_diff': {
                    '_default': {},
                    '_emit': True,
                    '_divider': 'empty_dict'},
                'gtp_to_hydrolyze': {
                    '_default': 0,
                    '_emit': True,
                    '_divider': 'zero',
                },
            },
            'evolvers_ran': {'_default': True},
            'first_update': {
                '_default': True,
                '_updater': 'set',
                '_divider': {'divider': 'set_value',
                    'config': {'value': True}},
            }
        }

        return ports

    def update_condition(self, timestep, states):
        return states['evolvers_ran']

    def next_update(self, timestep, states):
        # Skip t=0
        if states['first_update']:
            return {'first_update': False}
        
        if self.metabolite_idx is None:
            self.metabolite_idx = bulk_name_to_idx(
                self.model.metaboliteNamesFromNutrients, states['bulk']['id'])
            self.catalyst_idx = bulk_name_to_idx(
                self.model.catalyst_ids, states['bulk']['id'])
            self.kinetics_enzymes_idx = bulk_name_to_idx(
                self.model.kinetic_constraint_enzymes, states['bulk']['id'])
            self.kinetics_substrates_idx = bulk_name_to_idx(
                self.model.kinetic_constraint_substrates, states['bulk']['id'])
            self.aa_idx = bulk_name_to_idx(self.aa_names, states['bulk']['id'])

        timestep = self.parameters['time_step']

        # Load current state of the sim
        # Get internal state variables
        metabolite_counts_init = counts(
            states['bulk'], self.metabolite_idx)
        catalyst_counts = counts(states['bulk'], self.catalyst_idx)
        kinetic_enzyme_counts = counts(
            states['bulk'], self.kinetics_enzymes_idx)
        kinetic_substrate_counts = counts(
            states['bulk'], self.kinetics_substrates_idx)

        translation_gtp = states['polypeptide_elongation']['gtp_to_hydrolyze']
        cell_mass = states['listeners']['mass']['cell_mass'] * units.fg
        dry_mass = states['listeners']['mass']['dry_mass'] * units.fg

        # Get environment updates
        current_media_id = states['environment']['media_id']
        unconstrained = states['environment']['exchange_data']['unconstrained']
        constrained = states['environment']['exchange_data']['constrained']

        # Calculate state values
        cellVolume = cell_mass / self.cellDensity
        counts_to_molar = (1 / (self.nAvogadro * cellVolume)
                           ).asUnit(CONC_UNITS)

        # Coefficient to convert between flux (mol/g DCW/hr) basis and
        # concentration (M) basis
        coefficient = (dry_mass / cell_mass * self.cellDensity
            * timestep * units.s)

        ## Determine updates to concentrations depending on the current state
        doubling_time = self.nutrientToDoublingTime.get(
            current_media_id, self.nutrientToDoublingTime[self.media_id])
        conc_updates = self.model.getBiomassAsConcentrations(doubling_time)
        if self.use_trna_charging:
            conc_updates.update(self.update_amino_acid_targets(
                counts_to_molar,
                states['polypeptide_elongation']['aa_count_diff'],
                states['amino_acids_total'],
            ))
        if self.include_ppgpp:
            conc_updates[self.model.ppgpp_id] = self.model.getppGppConc(
                doubling_time).asUnit(CONC_UNITS)
        # Converted from units to make reproduction from listener data
        # accurate to model results (otherwise can have floating point diffs)
        conc_updates = {
            met: conc.asNumber(CONC_UNITS)
            for met, conc in conc_updates.items()}

        if self.parameters['reduce_murein_objective']:
            conc_updates['CPD-12261[p]'] /= 2.27

        # Update FBA problem based on current state
        # Set molecule availability (internal and external)
        self.model.set_molecule_levels(metabolite_counts_init, counts_to_molar,
            coefficient, current_media_id, unconstrained, constrained,
            conc_updates)

        # Set reaction limits for maintenance and catalysts present
        self.model.set_reaction_bounds(catalyst_counts, counts_to_molar,
            coefficient, translation_gtp)

        # Constrain reactions based on targets
        targets, upper_targets, lower_targets = \
            self.model.set_reaction_targets(kinetic_enzyme_counts,
                kinetic_substrate_counts, counts_to_molar, timestep * units.s)

        # Solve FBA problem and update states
        n_retries = 3
        fba = self.model.fba
        fba.solve(n_retries)

        # Internal molecule changes
        delta_metabolites = (1 / counts_to_molar) * (CONC_UNITS *
            fba.getOutputMoleculeLevelsChange())
        metabolite_counts_final = np.fmax(stochasticRound(
            self.random_state,
            metabolite_counts_init + delta_metabolites.asNumber()
            ), 0).astype(np.int64)
        delta_metabolites_final = metabolite_counts_final - \
            metabolite_counts_init

        # Environmental changes
        exchange_fluxes = CONC_UNITS * fba.getExternalExchangeFluxes()
        converted_exchange_fluxes = (
            exchange_fluxes / coefficient).asNumber(GDCW_BASIS)
        delta_nutrients = ((1 / counts_to_molar) *
                           exchange_fluxes).asNumber().astype(int)

        # Write outputs to listeners
        unconstrained, constrained, uptake_constraints = \
            self.get_import_constraints(unconstrained, constrained,
            GDCW_BASIS)

        # below is used for comparing target and estimate between GD-FBA
        # and LP-FBA, no effect on model
        # maintenance_ngam = ((self.ngam * coefficient) /
        #     (counts_to_molar*timestep)).asNumber()
        # # TODO (Cyrus) Add change in mass when implementing,
        # # currently counts/mass.
        # maintenance_gam = (self.gam).asNumber()
        # maintenance_gam_active = translation_gtp/timestep
        # maintenance_target = maintenance_ngam + maintenance_gam \
        #     + maintenance_gam_active


        # objective_counts = {str(key): int((self.model.homeostatic_objective[
        #     key] / counts_to_molar).asNumber()) - int(states['bulk']['count'][
        #         states['bulk']['id'] == key])
        #     for key in fba.getHomeostaticTargetMolecules()}

        # denom = counts_to_molar*timestep
        # kinetic_targets = {str(self.model.kinetics_constrained_reactions[i]):
        #     int((targets[i] / denom).asNumber())
        #     for i in range(len(targets))}

        # target_kinetic_bounds = {
        #     str(self.model.kinetics_constrained_reactions[i]):
        #         (int((lower_targets[i] / denom).asNumber()),
        #         int((upper_targets[i] / denom).asNumber()))
        #     for i in range(len(targets))}

        # fluxes = fba.getReactionFluxes() / timestep
        # names = fba.getReactionIDs()

        # flux_dict = {str(names[i]): int((fluxes[i] / denom).asNumber())
        #     for i in range(len(names))}

        update = {
            'bulk': [(self.metabolite_idx, delta_metabolites_final)],

            'environment': {
                'exchange': {
                    str(molecule): delta_nutrients[index]
                    for index, molecule in enumerate(
                        fba.getExternalMoleculeIDs())}},

            'listeners': {
                'fba_results': {
                    'media_id': current_media_id,
                    'conc_updates': [conc_updates[m]
                        for m in self.conc_update_molecules],
                    'catalyst_counts': catalyst_counts,
                    'translation_gtp': translation_gtp,
                    'coefficient': coefficient.asNumber(CONVERSION_UNITS),
                    'unconstrained_molecules': unconstrained,
                    'constrained_molecules': constrained,
                    # 'uptake_constraints': uptake_constraints,
                    'delta_metabolites': delta_metabolites_final,
                    # 104 KB, quite large, comment out to reduce emit size
                    'reaction_fluxes': fba.getReactionFluxes() / timestep,
                    'external_exchange_fluxes': converted_exchange_fluxes,
                    'objective_value': fba.getObjectiveValue(),
                    'shadow_prices': fba.getShadowPrices(
                        self.model.metaboliteNamesFromNutrients),
                    # 104 KB, quite large, comment out to reduce emit size
                    'reduced_costs': fba.getReducedCosts(fba.getReactionIDs()),
                    'target_concentrations': [
                        self.model.homeostatic_objective[mol]
                        for mol in fba.getHomeostaticTargetMolecules()],
                    'homeostatic_objective_values': \
                        fba.getHomeostaticObjectiveValues(),
                    'kinetic_objective_values': \
                        fba.getKineticObjectiveValues(),
                    
                    # Quite large, comment out to reduce emit size
                    # 'estimated_fluxes': flux_dict ,
                    # 'estimated_homeostatic_dmdt': {
                    #     metabolite: delta_metabolites_final[index]
                    #     for index, metabolite in enumerate(
                    #         self.model.metaboliteNamesFromNutrients)},
                    # 'target_homeostatic_dmdt': objective_counts,
                    # 'estimated_exchange_dmdt': {
                    #     molecule: delta_nutrients[index]
                    #     for index, molecule in enumerate(
                    #         fba.getExternalMoleculeIDs())},
                    # 'target_kinetic_fluxes': kinetic_targets,
                    # 'target_kinetic_bounds': target_kinetic_bounds,
                    # 'target_maintenance_flux': maintenance_target,
                },

                'enzyme_kinetics': {
                    'metabolite_counts_init': metabolite_counts_init,
                    'metabolite_counts_final': metabolite_counts_init + \
                        delta_metabolites_final,
                    'enzyme_counts_init': kinetic_enzyme_counts,
                    'counts_to_molar': counts_to_molar.asNumber(CONC_UNITS),
                    'actual_fluxes': fba.getReactionFluxes(
                        self.model.kinetics_constrained_reactions) / timestep,
                    'target_fluxes': targets / timestep,
                    'target_fluxes_upper': upper_targets / timestep,
                    'target_fluxes_lower': lower_targets / timestep}}}

        return update

    def update_amino_acid_targets(self, counts_to_molar, count_diff,
        amino_acid_counts):
        """
        Finds new amino acid concentration targets based on difference in
        supply and number of amino acids used in polypeptide_elongation

        Args:
            counts_to_molar (float with mol/volume units): conversion from
                counts to molar for the current state of the cell

        Returns:
            dict {AA name (str): AA conc (float with mol/volume units)}:
                new concentration targets for each amino acid

        Skips updates to molecules defined in self.aa_targets_not_updated:
        - L-SELENOCYSTEINE: rare AA that led to high variability when updated
        """

        if len(self.aa_targets):
            for aa, diff in count_diff.items():
                if aa in self.aa_targets_not_updated:
                    continue
                self.aa_targets[aa] += diff

        # First time step of a simulation so set target to current counts to
        # prevent concentration jumps between generations
        else:
            for aa, counts in amino_acid_counts.items():
                if aa in self.aa_targets_not_updated:
                    continue
                self.aa_targets[aa] = counts

        conc_updates = {aa: counts * counts_to_molar for aa,
                        counts in self.aa_targets.items()}

        # Update linked metabolites that will follow an amino acid
        for met, link in self.linked_metabolites.items():
            conc_updates[met] = conc_updates.get(
                link['lead'], 0 * counts_to_molar) * link['ratio']

        return conc_updates


class FluxBalanceAnalysisModel(object):
    """
    Metabolism model that solves an FBA problem with modular_fba.
    """

    def __init__(self, parameters, timeline=None, include_ppgpp=True):
        """
        Args:
            sim_data: simulation data
            timeline: timeline for nutrient changes during simulation
                (time of change, media ID), if None, nutrients for the saved
                condition are set at time 0 (eg. [(0.0, 'minimal')])
            include_ppgpp: if True, ppGpp is included as a concentration target
        """

        if timeline is None:
            nutrients = parameters['nutrients']
            timeline = [(0., nutrients)]
        else:
            nutrients = timeline[0][1]

        # Local sim_data references
        metabolism = parameters['metabolism']
        self.stoichiometry = metabolism.reaction_stoich
        self.maintenance_reaction = metabolism.maintenance_reaction

        # Load constants
        self.ngam = parameters['non_growth_associated_maintenance']
        gam = parameters['dark_atp'] * parameters['cell_dry_mass_fraction']

        self.exchange_constraints = metabolism.exchange_constraints

        self._biomass_concentrations = {}  # type: dict
        self._getBiomassAsConcentrations = parameters[
            'get_biomass_as_concentrations']

        # Include ppGpp concentration target in objective if not handled
        # kinetically in other processes
        self.ppgpp_id = parameters['ppgpp_id']
        self.getppGppConc = parameters['get_ppGpp_conc']

        # go through all media in the timeline and add to metaboliteNames
        metaboliteNamesFromNutrients = set()
        exchange_molecules = set()
        conc_from_nutrients = (metabolism.concentration_updates.
            concentrations_based_on_nutrients)
        if include_ppgpp:
            metaboliteNamesFromNutrients.add(self.ppgpp_id)
        for time, media_id in timeline:
            metaboliteNamesFromNutrients.update(conc_from_nutrients(media_id))
            exchanges = parameters['exchange_data_from_media'](media_id)
            exchange_molecules.update(exchanges['externalExchangeMolecules'])
        self.metaboliteNamesFromNutrients = list(
            sorted(metaboliteNamesFromNutrients))
        exchange_molecules = list(sorted(exchange_molecules))
        molecule_masses = dict(zip(exchange_molecules, 
            parameters['get_masses'](exchange_molecules).asNumber(
                MASS_UNITS / COUNTS_UNITS)))

        # Setup homeostatic objective concentration targets
        # Determine concentrations based on starting environment
        conc_dict = conc_from_nutrients(nutrients)
        doubling_time = parameters['doubling_time']
        conc_dict.update(self.getBiomassAsConcentrations(doubling_time))
        if include_ppgpp:
            conc_dict[self.ppgpp_id] = self.getppGppConc(doubling_time)
        self.homeostatic_objective = dict(
            (key, conc_dict[key].asNumber(CONC_UNITS)) for key in conc_dict)

        # TODO: For testing, remove later (perhaps after modifying sim data)
        if parameters["reduce_murein_objective"]:
            self.homeostatic_objective['CPD-12261[p]'] /= 2.27

        # Include all concentrations that will be present in a sim for constant
        # length listeners
        for met in self.metaboliteNamesFromNutrients:
            if met not in self.homeostatic_objective:
                self.homeostatic_objective[met] = 0.

        # Data structures to compute reaction bounds based on enzyme
        # presence/absence
        self.catalyst_ids = metabolism.catalyst_ids
        self.reactions_with_catalyst = metabolism.reactions_with_catalyst

        i = metabolism.catalysis_matrix_I
        j = metabolism.catalysis_matrix_J
        v = metabolism.catalysis_matrix_V
        shape = (i.max() + 1, j.max() + 1)
        self.catalysis_matrix = csr_matrix((v, (i, j)), shape=shape)

        # Function to compute reaction targets based on kinetic parameters and
        # molecule concentrations
        self.get_kinetic_constraints = metabolism.get_kinetic_constraints

        # Remove disabled reactions so they don't get included in the FBA
        # problem setup
        kinetic_constraint_reactions = metabolism.kinetic_constraint_reactions
        constraintsToDisable = metabolism.constraints_to_disable
        self.active_constraints_mask = np.array(
            [(rxn not in constraintsToDisable)
             for rxn in kinetic_constraint_reactions])
        self.kinetics_constrained_reactions = list(
            np.array(kinetic_constraint_reactions)[
                self.active_constraints_mask])

        self.kinetic_constraint_enzymes = metabolism.kinetic_constraint_enzymes
        self.kinetic_constraint_substrates = \
            metabolism.kinetic_constraint_substrates

        # Set solver and kinetic objective weight (lambda)
        solver = metabolism.solver
        kinetic_objective_weight = metabolism.kinetic_objective_weight
        kinetic_objective_weight_in_range = \
            metabolism.kinetic_objective_weight_in_range

        # Disable kinetics completely if weight is 0 or specified in file above
        if not USE_KINETICS or kinetic_objective_weight == 0:
            objective_type = 'homeostatic'
            self.use_kinetics = False
            kinetic_objective_weight = 0
        else:
            objective_type = 'homeostatic_kinetics_mixed'
            self.use_kinetics = True

        # Set up FBA solver
        # reactionRateTargets value is just for initialization, it gets reset
        # each timestep during evolveState
        fba_options = {
            "reactionStoich": metabolism.reaction_stoich,
            "externalExchangedMolecules": exchange_molecules,
            "objective": self.homeostatic_objective,
            "objectiveType": objective_type,
            "objectiveParameters": {
                "kineticObjectiveWeight": kinetic_objective_weight,
                'kinetic_objective_weight_in_range': \
                    kinetic_objective_weight_in_range,
                "reactionRateTargets": {reaction: 1 for reaction
                    in self.kinetics_constrained_reactions},
                "oneSidedReactionTargets": [],
            },
            "moleculeMasses": molecule_masses,
            # The "inconvenient constant"--limit secretion (e.g., of CO2)
            "secretionPenaltyCoeff": metabolism.secretion_penalty_coeff,
            "solver": solver,
            "maintenanceCostGAM": gam.asNumber(COUNTS_UNITS / MASS_UNITS),
            "maintenanceReaction": metabolism.maintenance_reaction,
        }
        self.fba = FluxBalanceAnalysis(**fba_options)

        self.metabolite_names = {met: i for i, met in enumerate(
            self.fba.getOutputMoleculeIDs())}
        self.aa_names_no_location = [x[:-3]
                                     for x in parameters['amino_acid_ids']]

    def getBiomassAsConcentrations(self, doubling_time):
        """
        Caches the result of the sim_data function to improve performance since
        function requires computation but won't change for a given doubling_time.

        Args:
            doubling_time (float with time units): doubling time of the cell to
                get the metabolite concentrations for

        Returns:
            dict {str : float with concentration units}: dictionary with metabolite
                IDs as keys and concentrations as values
        """

        minutes = doubling_time.asNumber(units.min)  # hashable
        if minutes not in self._biomass_concentrations:
            self._biomass_concentrations[minutes] = \
                self._getBiomassAsConcentrations(doubling_time)

        return self._biomass_concentrations[minutes]

    def update_external_molecule_levels(self, objective,
        metabolite_concentrations, external_molecule_levels):
        """
        Limit amino acid uptake to what is needed to meet concentration
        objective to prevent use as carbon source, otherwise could be used
        as an infinite nutrient source.

        Args:
            objective (Dict[str, Unum]): homeostatic objective for internal
                molecules (molecule ID: concentration in counts/volume units)
            metabolite_concentrations (Unum[float]): concentration for each
                molecule in metabolite_names
            external_molecule_levels (np.ndarray[float]): current limits on
                external molecule availability

        Returns:
            external_molecule_levels (np.ndarray[float]): updated limits on
                external molecule availability

        TODO(wcEcoli):
            determine rate of uptake so that some amino acid uptake can
            be used as a carbon/nitrogen source
        """

        external_exchange_molecule_ids = self.fba.getExternalMoleculeIDs()
        for aa in self.aa_names_no_location:
            if aa + "[p]" in external_exchange_molecule_ids:
                idx = external_exchange_molecule_ids.index(aa + "[p]")
            elif aa + "[c]" in external_exchange_molecule_ids:
                idx = external_exchange_molecule_ids.index(aa + "[c]")
            else:
                continue

            conc_diff = objective[aa + "[c]"] - \
                metabolite_concentrations[self.metabolite_names[aa +
                    "[c]"]].asNumber(CONC_UNITS)
            if conc_diff < 0:
                conc_diff = 0

            if external_molecule_levels[idx] > conc_diff:
                external_molecule_levels[idx] = conc_diff

        return external_molecule_levels

    def set_molecule_levels(self, metabolite_counts, counts_to_molar,
        coefficient, current_media_id, unconstrained, constrained, conc_updates):
        """
        Set internal and external molecule levels available to the FBA solver.

        Args:
            metabolite_counts (np.ndarray[int]): counts for each metabolite
                with a concentration target
            counts_to_molar (Unum): conversion from counts to molar
                (counts/volume units)
            coefficient (Unum): coefficient to convert from mmol/g DCW/hr
                to mM basis (mass.time/volume units)
            current_media_id (str): ID of current media
            unconstrained (Set[str]): molecules that have unconstrained import
            constrained (Dict[str, units.Unum]): molecules (keys) and their
                limited max uptake rates (values in mol / mass / time units)
            conc_updates (Dict[str, Unum]): updates to concentrations targets
                for molecules (molecule ID: concentration in counts/volume
                units)
        """

        # Update objective from media exchanges
        external_molecule_levels, objective = self.exchange_constraints(
            self.fba.getExternalMoleculeIDs(), coefficient, CONC_UNITS,
            current_media_id, unconstrained, constrained, conc_updates,
        )
        self.fba.update_homeostatic_targets(objective)

        # Internal concentrations
        metabolite_conc = counts_to_molar * metabolite_counts
        self.fba.setInternalMoleculeLevels(
            metabolite_conc.asNumber(CONC_UNITS))

        # External concentrations
        external_molecule_levels = self.update_external_molecule_levels(
            objective, metabolite_conc, external_molecule_levels)
        self.fba.setExternalMoleculeLevels(external_molecule_levels)

    def set_reaction_bounds(self, catalyst_counts, counts_to_molar,
        coefficient, gtp_to_hydrolyze):
        """
        Set reaction bounds for constrained reactions in the FBA object.

        Args:
            catalyst_counts (np.ndarray[int]): counts of enzyme catalysts
            counts_to_molar (Unum): conversion from counts to molar
                (counts/volume units)
            coefficient (Unum): coefficient to convert from mmol/g DCW/hr
                to mM basis (mass.time/volume units)
            gtp_to_hydrolyze (float): number of GTP molecules to hydrolyze to
                account for consumption in translation
        """

        # Maintenance reactions
        # Calculate new NGAM
        flux = (self.ngam * coefficient).asNumber(CONC_UNITS)
        self.fba.setReactionFluxBounds(
            self.fba._reactionID_NGAM,
            lowerBounds=flux, upperBounds=flux,
        )

        # Calculate GTP usage based on how much was needed in polypeptide
        # elongation in previous step.
        flux = (counts_to_molar * gtp_to_hydrolyze).asNumber(CONC_UNITS)
        self.fba.setReactionFluxBounds(
            self.fba._reactionID_polypeptideElongationEnergy,
            lowerBounds=flux, upperBounds=flux,
        )

        # Set hard upper bounds constraints based on enzyme presence
        # (infinite upper bound) or absence (upper bound of zero)
        reaction_bounds = np.inf * np.ones(len(self.reactions_with_catalyst))
        no_rxn_mask = self.catalysis_matrix.dot(catalyst_counts) == 0
        reaction_bounds[no_rxn_mask] = 0
        self.fba.setReactionFluxBounds(self.reactions_with_catalyst,
            upperBounds=reaction_bounds, raiseForReversible=False)

    def set_reaction_targets(self, kinetic_enzyme_counts,
        kinetic_substrate_counts, counts_to_molar, time_step):
        # type: (np.ndarray, np.ndarray, units.Unum, units.Unum) -> Tuple[np.ndarray, np.ndarray, np.ndarray]
        """
        Set reaction targets for constrained reactions in the FBA object.

        Args:
            kinetic_enzyme_counts (np.ndarray[int]): counts of enzymes used in
                kinetic constraints
            kinetic_substrate_counts (np.ndarray[int]): counts of substrates
                used in kinetic constraints
            counts_to_molar: conversion from counts to molar
                (float with counts/volume units)
            time_step: current time step (float with time units)

        Returns:
            mean_targets (np.ndarray[float]): mean target for each constrained
                reaction
            upper_targets (np.ndarray[float]): upper target limit for each
                constrained reaction
            lower_targets (np.ndarray[float]): lower target limit for each
                constrained reaction
        """

        if self.use_kinetics:
            enzyme_conc = counts_to_molar * kinetic_enzyme_counts
            substrate_conc = counts_to_molar * kinetic_substrate_counts

            # Set target fluxes for reactions based on their most relaxed
            # constraint
            reaction_targets = self.get_kinetic_constraints(
                enzyme_conc, substrate_conc)

            # Calculate reaction flux target for current time step
            targets = (time_step * reaction_targets).asNumber(CONC_UNITS)[
                self.active_constraints_mask, :]
            lower_targets = targets[:, 0]
            mean_targets = targets[:, 1]
            upper_targets = targets[:, 2]

            # Set kinetic targets only if kinetics is enabled
            self.fba.set_scaled_kinetic_objective(time_step.asNumber(units.s))
            self.fba.setKineticTarget(
                self.kinetics_constrained_reactions, mean_targets,
                lower_targets=lower_targets, upper_targets=upper_targets)
        else:
            lower_targets = np.zeros(len(self.kinetics_constrained_reactions))
            mean_targets = np.zeros(len(self.kinetics_constrained_reactions))
            upper_targets = np.zeros(len(self.kinetics_constrained_reactions))

        return mean_targets, upper_targets, lower_targets


def test_metabolism_listener():
    from ecoli.experiments.ecoli_master_sim import EcoliSim
    sim = EcoliSim.from_file()
    sim.total_time = 2
    sim.raw_output = False
    sim.build_ecoli()
    sim.run()
    data = sim.query()
    reaction_fluxes = data['listeners']['fba_results']['reaction_fluxes']
    assert(type(reaction_fluxes[0]) == list)
    assert(type(reaction_fluxes[1]) == list)


if __name__ == '__main__':
    test_metabolism_listener()
