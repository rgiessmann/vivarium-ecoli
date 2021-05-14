"""
PolypeptideElongation

Translation elongation sub-model.

TODO:
- see the initiation process for more TODOs
"""

import numpy as np
import logging as log

from vivarium.core.process import Process
from vivarium.library.dict_utils import deep_merge
from vivarium.core.composition import simulate_process
from vivarium.plots.simulation_output import plot_variables

from ecoli.library.schema import bulk_schema, listener_schema, arrays_from, array_from
from ecoli.models.polypeptide_elongation_models import BaseElongationModel, MICROMOLAR_UNITS

from wholecell.utils.polymerize import buildSequences, polymerize, computeMassIncrease
from wholecell.utils.random import stochasticRound
from wholecell.utils import units



class PolypeptideElongation(Process):
    name = 'ecoli-polypeptide-elongation'

    defaults = {
        'max_time_step': 2.0,
        'n_avogadro': 6.02214076e+23 / units.mol,
        'proteinIds': np.array([]),
        'proteinLengths': np.array([]),
        'proteinSequences': np.array([[]]),
        'aaWeightsIncorporated': np.array([]),
        'endWeight': np.array([2.99146113e-08]),
        'variable_elongation': False,
        'make_elongation_rates': lambda random, rate, timestep, variable: np.array([]),
        'ribosomeElongationRate': 17.388824902723737,
        'translation_aa_supply': {'minimal': np.array([])},
        'import_threshold': 1e-05,
        'aa_from_trna': np.array([[]]),
        'gtpPerElongation': 4.2,
        'ppgpp_regulation': False,
        'trna_charging': False,
        'translation_supply': False,
        'ribosome30S': 'ribosome30S',
        'ribosome50S': 'ribosome50S',
        'amino_acids': [],

        'basal_elongation_rate': 22.0,
        'ribosomeElongationRateDict': {},
        'uncharged_trna_names': np.array([]),
        'aaNames': [],
        'proton': 'PROTON',
        'water': 'H2O',
        'cellDensity': 1100 * units.g / units.L,
        'elongation_max': 22 * units.aa / units.s,
        'aa_from_synthetase': np.array([[]]),
        'charging_stoich_matrix': np.array([[]]),
        'charged_trna_names': [],
        'charging_molecule_names': [],
        'synthetase_names': [],
        'ppgpp_reaction_names': [],
        'ppgpp_reaction_metabolites': [],
        'ppgpp_reaction_stoich': np.array([[]]),
        'ppgpp_synthesis_reaction': 'GDPPYPHOSKIN-RXN',
        'ppgpp_degradation_reaction': 'PPGPPSYN-RXN',
        'rela': 'RELA',
        'spot': 'SPOT',
        'ppgpp': 'ppGpp',
        'kS': 100.0,
        'KMtf': 1.0,
        'KMaa': 100.0,
        'krta': 1.0,
        'krtf': 500.0,
        'KD_RelA': 0.26,
        'k_RelA': 75.0,
        'k_SpoT_syn': 2.6,
        'k_SpoT_deg': 0.23,
        'KI_SpoT': 20.0,
        'aa_supply_scaling': lambda aa_conc, aa_in_media: 0,
        'seed': 0}

    def __init__(self, initial_parameters):
        super().__init__(initial_parameters)

        self.max_time_step = self.parameters['max_time_step']

        # Load parameters
        self.n_avogadro = self.parameters['n_avogadro']
        self.proteinIds = self.parameters['proteinIds']
        self.protein_lengths = self.parameters['proteinLengths']
        self.proteinSequences = self.parameters['proteinSequences']
        self.aaWeightsIncorporated = self.parameters['aaWeightsIncorporated']
        self.endWeight = self.parameters['endWeight']
        self.variable_elongation = self.parameters['variable_elongation']
        self.make_elongation_rates = self.parameters['make_elongation_rates']
        self.ribosome30S = self.parameters['ribosome30S']
        self.ribosome50S = self.parameters['ribosome50S']
        self.amino_acids = self.parameters['amino_acids']
        self.aaNames = self.parameters['aaNames']

        self.ribosomeElongationRate = self.parameters['ribosomeElongationRate']

        # Amino acid supply calculations
        self.translation_aa_supply = self.parameters['translation_aa_supply']
        self.import_threshold = self.parameters['import_threshold']

        # Used for figure in publication
        self.trpAIndex = np.where(self.proteinIds == "TRYPSYN-APROTEIN[c]")[0][0]

        self.elngRateFactor = 1.

        # Data structures for charging
        self.aa_from_trna = self.parameters['aa_from_trna']

        # Set modeling method
        # if self.parameters['trna_charging']:
        #     self.elongation_model = SteadyStateElongationModel(self.parameters, self)
        # elif self.parameters['translation_supply']:
        #     self.elongation_model = TranslationSupplyElongationModel(self.parameters, self)
        # else:
        #     self.elongation_model = BaseElongationModel(self.parameters, self)
        self.elongation_model = BaseElongationModel(self.parameters, self)
        self.ppgpp_regulation = self.parameters['ppgpp_regulation']

        # Growth associated maintenance energy requirements for elongations
        self.gtpPerElongation = self.parameters['gtpPerElongation']
        ## Need to account for ATP hydrolysis for charging that has been
        ## removed from measured GAM (ATP -> AMP is 2 hydrolysis reactions)
        ## if charging reactions are not explicitly modeled
        if not self.parameters['trna_charging']:
            self.gtpPerElongation += 2
        ## Variable for metabolism to read to consume required energy
        self.gtp_to_hydrolyze = 0

        # basic molecule names
        self.proton = self.parameters['proton']
        self.water = self.parameters['water']
        self.rela = self.parameters['rela']
        self.spot = self.parameters['spot']
        self.ppgpp = self.parameters['ppgpp']

        # Names of molecules associated with tRNA charging
        self.ppgpp_reaction_metabolites = self.parameters['ppgpp_reaction_metabolites']
        self.uncharged_trna_names = self.parameters['uncharged_trna_names']
        self.charged_trna_names = self.parameters['charged_trna_names']
        self.charging_molecule_names = self.parameters['charging_molecule_names']
        self.synthetase_names = self.parameters['synthetase_names']

        self.seed = self.parameters['seed']
        self.random_state = np.random.RandomState(seed = self.seed)

    def ports_schema(self):
        return {
            'environment': {
                'media_id': {
                    '_default': '',
                    '_updater': 'set'},
                'amino_acids': bulk_schema([aa[:-3] for aa in self.aaNames])},

            'listeners': {
                'mass': {
                    'cell_mass': {'_default': 0.0},
                    'dry_mass': {'_default': 0.0}},

                'growth_limits': listener_schema({
                    'fraction_trna_charged': 0,
                    'aa_pool_size': 0,
                    'aa_request_size': 0,
                    'aa_allocated': 0,
                    'active_ribosomes_allocated': 0,
                    'net_charged': 0,
                    'aasUsed': 0}),

                'ribosome_data': listener_schema({
                    'translation_supply': 0,
                    'effective_elongation_rate': 0,
                    'aaCountInSequence': 0,
                    'aaCounts': 0,
                    'actualElongations': 0,
                    'actualElongationHist': 0, 
                    'elongationsNonTerminatingHist': 0,
                    'didTerminate': 0,
                    'terminationLoss': 0,
                    'numTrpATerminated': 0,
                    'processElongationRate': 0})},

            'molecules': bulk_schema([
                self.proton,
                self.water,
                self.rela,
                self.spot,
                self.ppgpp]),

            'monomers': bulk_schema(self.proteinIds),
            'amino_acids': bulk_schema(self.amino_acids),
            'ppgpp_reaction_metabolites': bulk_schema(self.ppgpp_reaction_metabolites),
            'uncharged_trna': bulk_schema(self.uncharged_trna_names),
            'charged_trna': bulk_schema(self.charged_trna_names),
            'charging_molecules': bulk_schema(self.charging_molecule_names),
            'synthetases': bulk_schema(self.synthetase_names),

            'active_ribosome': {
                '*': {
                    'unique_index': {'_default': 0, '_updater': 'set'},
                    'protein_index': {'_default': 0, '_updater': 'set'},
                    'peptide_length': {'_default': 0, '_updater': 'set', '_emit': True},
                    'pos_on_mRNA': {'_default': 0, '_updater': 'set', '_emit': True},
                    'submass': {
                        'protein': {'_default': 0, '_emit': True}}}},

            'subunits': {
                self.ribosome30S: {
                    '_default': 0,
                    '_emit': True},
                self.ribosome50S: {
                    '_default': 0,
                    '_emit': True}},

            'polypeptide_elongation': {
                'aa_count_diff': {
                    '_default': {},
                    '_updater': 'set',
                    '_emit': True},
                'gtp_to_hydrolyze': {
                    '_default': 0,
                    '_updater': 'set',
                    '_emit': True}}}

    def next_update(self, timestep, states):
        # Set ribosome elongation rate based on simulation medium environment and elongation rate factor
        # which is used to create single-cell variability in growth rate
        # The maximum number of amino acids that can be elongated in a single timestep is set to 22 intentionally as the minimum number of padding values
        # on the protein sequence matrix is set to 22. If timesteps longer than 1.0s are used, this feature will lead to errors in the effective ribosome
        # elongation rate.

        update = {
            'molecules': {
                self.water: 0,
            },
            'listeners': {
                'ribosome_data': {},
                'growth_limits': {}}}

        current_media_id = states['environment']['media_id']

        # MODEL SPECIFIC: get ribosome elongation rate
        self.ribosomeElongationRate = self.elongation_model.elongation_rate(current_media_id)

        # If there are no active ribosomes, return immediately
        if len(states['active_ribosome']) == 0:
            return {}

        # Build sequences to request appropriate amount of amino acids to
        # polymerize for next timestep
        protein_indexes, peptide_lengths, positions_on_mRNA = arrays_from(
            states['active_ribosome'].values(),
            ['protein_index', 'peptide_length', 'pos_on_mRNA'])

        self.elongation_rates = self.make_elongation_rates(
            self.random_state,
            self.ribosomeElongationRate,
            timestep,
            self.variable_elongation)

        # import ipdb; ipdb.set_trace()

        sequences = buildSequences(
            self.proteinSequences,
            protein_indexes,
            peptide_lengths,
            self.elongation_rates)

        sequenceHasAA = (sequences != polymerize.PAD_VALUE)
        aasInSequences = np.bincount(sequences[sequenceHasAA], minlength=21)

        # Calculate AA supply for expected doubling of protein
        dryMass = states['listeners']['mass']['dry_mass'] * units.fg
        self.cell_mass = states['listeners']['mass']['cell_mass'] * units.fg
        translation_supply_rate = self.translation_aa_supply[current_media_id] * self.elngRateFactor
        mol_aas_supplied = translation_supply_rate * dryMass * timestep * units.s
        self.aa_supply = units.strip_empty_units(mol_aas_supplied * self.n_avogadro)
        update['listeners']['ribosome_data']['translation_supply'] = translation_supply_rate.asNumber()

        assert not any(i<0 for i in states['uncharged_trna'].values())

        # MODEL SPECIFIC to self.elongation_model: Calculate AA request
        fraction_charged, aa_counts_for_translation, requests = self.elongation_model.request(
            timestep, states, aasInSequences)

        # Write to listeners
        update['listeners']['growth_limits']['fraction_trna_charged'] = np.dot(fraction_charged, self.aa_from_trna)
        update['listeners']['growth_limits']['aa_pool_size'] = array_from(states['amino_acids'])
        update['listeners']['growth_limits']['aa_request_size'] = aa_counts_for_translation


        ## Begin wcEcoli evolveState()
        # Set value to 0 for metabolism in case of early return
        self.gtp_to_hydrolyze = 0

        # Write allocation data to listener
        # update['listeners']['growth_limits']['aa_allocated'] = aa_counts_for_translation

        # Get number of active ribosomes
        n_active_ribosomes = len(states['active_ribosome'])
        update['listeners']['growth_limits']['active_ribosomes_allocated'] = n_active_ribosomes

        if n_active_ribosomes == 0:
            return update

        if sequences.size == 0:
            return update

        # Calculate elongation resource capacity
        aaCountInSequence = np.bincount(sequences[(sequences != polymerize.PAD_VALUE)])
        # total_aa_counts = array_from(states['amino_acids'])
        # total_aa_counts = self.aas.counts()

        # MODEL SPECIFIC: Get amino acid counts
        aa_counts_for_translation = self.elongation_model.final_amino_acids(aa_counts_for_translation)
        aa_counts_for_translation = aa_counts_for_translation.astype(int)

        # Using polymerization algorithm elongate each ribosome up to the limits
        # of amino acids, sequence, and GTP
        result = polymerize(
            sequences,
            aa_counts_for_translation,
            10000000, # Set to a large number, the limit is now taken care of in metabolism
            self.random_state,
            self.elongation_rates[protein_indexes])

        sequence_elongations = result.sequenceElongation
        aas_used = result.monomerUsages
        nElongations = result.nReactions

        # Update masses of ribosomes attached to polymerizing polypeptides
        added_protein_mass = computeMassIncrease(
            sequences,
            sequence_elongations,
            self.aaWeightsIncorporated)

        updated_lengths = peptide_lengths + sequence_elongations
        updated_positions_on_mRNA = positions_on_mRNA + 3*sequence_elongations

        didInitialize = (
            (sequence_elongations > 0) &
            (peptide_lengths == 0))

        added_protein_mass[didInitialize] += self.endWeight

        # Write current average elongation to listener
        currElongRate = (sequence_elongations.sum() / n_active_ribosomes) / timestep
        update['listeners']['ribosome_data']['effective_elongation_rate'] = currElongRate

        # Update active ribosomes, terminating if necessary
        # self.active_ribosomes.attrIs(
        #     peptide_length=updated_lengths,
        #     pos_on_mRNA=updated_positions_on_mRNA)
        # self.active_ribosomes.add_submass_by_name("protein", added_protein_mass)

        # Ribosomes that reach the end of their sequences are terminated and
        # dissociated into 30S and 50S subunits. The polypeptide that they are polymerizing
        # is converted into a protein in BulkMolecules
        terminalLengths = self.protein_lengths[protein_indexes]

        didTerminate = (updated_lengths == terminalLengths)

        terminatedProteins = np.bincount(
            protein_indexes[didTerminate],
            minlength = self.proteinSequences.shape[0])

        # self.active_ribosomes.delByIndexes(termination)

        update['active_ribosome'] = {'_delete': []}
        for index, ribosome in enumerate(states['active_ribosome'].values()):
            if didTerminate[index]:
                update['active_ribosome']['_delete'].append((ribosome['unique_index'],))
            else:
                update['active_ribosome'][ribosome['unique_index']] = {
                    'peptide_length': updated_lengths[index],
                    'pos_on_mRNA': updated_positions_on_mRNA[index],
                    'submass': {
                        'protein': added_protein_mass[index]}}

        update['monomers'] = {}
        for index, count in enumerate(terminatedProteins):
            update['monomers'][self.proteinIds[index]] = count

        # self.bulkMonomers.countsInc(terminatedProteins)

        nTerminated = didTerminate.sum()
        nInitialized = didInitialize.sum()

        update['subunits'] = {}
        update['subunits'][self.ribosome30S] = nTerminated
        update['subunits'][self.ribosome50S] = nTerminated
        # self.ribosome30S.countInc(nTerminated)
        # self.ribosome50S.countInc(nTerminated)

        # MODEL SPECIFIC: evolve
        # TODO: use something other than a class attribute to pass aa diff to metabolism
        net_charged, aa_count_diff, evolve_update = self.elongation_model.evolve(
            timestep,
            states,
            requests,
            aa_counts_for_translation,
            aas_used,
            nElongations,
            nInitialized)

        update = deep_merge(update, evolve_update)

        # GTP hydrolysis is carried out in Metabolism process for growth
        # associated maintenance. This is set here for metabolism to use.
        self.gtp_to_hydrolyze = self.gtpPerElongation * nElongations

        update['polypeptide_elongation'] = {}
        update['polypeptide_elongation']['aa_count_diff'] = aa_count_diff
        update['polypeptide_elongation']['gtp_to_hydrolyze'] = self.gtp_to_hydrolyze

        # Write data to listeners
        update['listeners']['growth_limits']['net_charged'] = net_charged

        update['listeners']['ribosome_data']['effective_elongation_rate'] = currElongRate
        update['listeners']['ribosome_data']['aaCountInSequence'] = aaCountInSequence
        update['listeners']['ribosome_data']['aaCounts'] = aa_counts_for_translation
        update['listeners']['ribosome_data']['actualElongations'] = sequence_elongations.sum()
        update['listeners']['ribosome_data']['actualElongationHist'] = np.histogram(
            sequence_elongations, bins = np.arange(0,23))[0]
        update['listeners']['ribosome_data']['elongationsNonTerminatingHist'] = np.histogram(
            sequence_elongations[~didTerminate], bins=np.arange(0,23))[0]
        update['listeners']['ribosome_data']['didTerminate'] = didTerminate.sum()
        update['listeners']['ribosome_data']['terminationLoss'] = (terminalLengths - peptide_lengths)[
            didTerminate].sum()
        update['listeners']['ribosome_data']['numTrpATerminated'] = terminatedProteins[self.trpAIndex]
        update['listeners']['ribosome_data']['processElongationRate'] = self.ribosomeElongationRate / timestep

        log.info('polypeptide elongation terminated: {}'.format(nTerminated))

        return update

    def isTimeStepShortEnough(self, inputTimeStep, timeStepSafetyFraction):
        return inputTimeStep <= self.max_time_step


def test_polypeptide_elongation():
    def make_elongation_rates(random, base, time_step, variable_elongation=False):
        size = 1
        lengths = time_step * np.full(size, base, dtype=np.int64)
        lengths = stochasticRound(random, lengths) if random else np.round(lengths)
        return lengths.astype(np.int64)

    amino_acids = [
            'L-ALPHA-ALANINE[c]', 'ARG[c]', 'ASN[c]', 'L-ASPARTATE[c]', 'CYS[c]', 'GLT[c]', 'GLN[c]', 'GLY[c]',
            'HIS[c]', 'ILE[c]', 'LEU[c]', 'LYS[c]', 'MET[c]', 'PHE[c]', 'PRO[c]', 'SER[c]', 'THR[c]', 'TRP[c]',
            'TYR[c]', 'L-SELENOCYSTEINE[c]', 'VAL[c]']
    test_config = {
        'time_step': 2,
        'proteinIds': np.array(['TRYPSYN-APROTEIN[c]']),
        'ribosomeElongationRateDict': {'minimal': 17.388824902723737 * units.aa / units.s},
        'ribosome30S': 'CPLX0-3953[c]',
        'ribosome50S': 'CPLX0-3962[c]',
        'amino_acids': amino_acids,
        'make_elongation_rates': make_elongation_rates,
        'proteinSequences': np.array([[12, 15, 1, 1, 20, 0, 16, 9, 16, 10, 2, 14, 0, 18, 3, 10, 20, 7, 13, 4, 14, 5, 9, 5, 1, 7, 5, 20, 2, 10, 20, 11, 16, 16, 7, 10, 8, 0, 0, 7, 11, 7, 9, 2, 20, 0, 11, 20, 10, 11, 3, 10, 7, 9, 3, 20, 16, 20, 7, 7, 13, 10, 7, 11, 3, 2, 6, 3, 7, 13, 6, 6, 10, 13, 15, 5, 10, 7, 9, 0, 2, 1, 13, 6, 20, 20, 6, 7, 1, 16, 1, 9, 2, 20, 11, 10, 16, 5, 11, 3, 7, 5, 20, 16, 3, 13, 2, 13, 15, 7, 13, 5, 20, 16, 14, 0, 3, 17, 5, 1, 13, 20, 16, 3, 15, 10, 15, 17, 10, 7, 6, 13, 3, 12, 20, 4, 20, 15, 7, 15, 10, 14, 15, 7, 20, 15, 14, 5, 0, 13, 16, 3, 17, 12, 16, 1, 10, 1, 15, 6, 4, 14, 4, 9, 9, 13, 3, 15, 15, 1, 5, 0, 10, 20, 0, 7, 10, 11, 0, 0, 14, 17, 10, 20, 11, 14, 2, 1, 1, 5, 10, 5, 9, 17, 0, 7, 1, 11, 10, 14, 5, 12, 11, 3, 20, 9, 5, 0, 0, 8, 0, 10, 1, 5, 6, 7, 9, 0, 8, 20, 20, 9, 15, 10, 7, 0, 5, 7, 0, 10, 17, 20, 2, 0, 15, 7, 5, 17, 9, 0, 11, 14, 14, 15, 20, 3, 20, 20, 15, 16, 20, 7, 0, 7, 3, 15, 12, 20, 7, 7, 10, 9, 18, 7, 10, 10, 12, 1, 5, 15, 15, 5, 8, 16, 10, 1, 10, 0, 16, 0, 20, 0, 0, 10, 0, 20, 15, 6, 15, 2, 20, 7, 9, 16, 3, 1, 14, 6, 10, 0, 0, 12, 12, 0, 1, 20, 3, 10, 6, 14, 13, 2, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1, -1]]).astype(np.int8)
    }

    polypep_elong = PolypeptideElongation(test_config)

    initial_state = {
        'environment': {'media_id': 'minimal'},
        'subunits': {
            'CPLX0-3953[c]': 100,
            'CPLX0-3962[c]': 100
        },
        'monomers': {
            'TRYPSYN-APROTEIN[c]': 0,
        },
        'amino_acids': {
            aa: 100 for aa in amino_acids
        },
        'active_ribosome': {
            '1': {'unique_index': 1, 'protein_index': 1, 'peptide_length': 1, 'pos_on_mRNA': 1, 'submass': {'protein': 0}}
        }
    }

    settings = {
        'total_time': 100,
        'initial_state': initial_state}
    data = simulate_process(polypep_elong, settings)

    return data, test_config



def run_plot(data, config):

    proteins = [('monomers', prot_id) for prot_id in config['proteinIds']]
    aa = [('amino_acids', aa) for aa in config['amino_acids']]
    plot_variables(
        data,
        variables=proteins,
        out_dir='out/processes/polypeptide_elongation',
        filename='variables'
    )




def main():
    data, config = test_polypeptide_elongation()
    run_plot(data, config)

if __name__ == '__main__':
    main()
