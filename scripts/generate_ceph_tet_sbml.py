'''Generate an SBML File for Bioscrape to Simulate

The SBML file defines a chemical reaction network (CRN) with following
reactions:

* Antibiotic influx via diffusion through porins.
* Antibiotic hydrolysis
* Antibiotic export by efflux pumps

The CRN can then be simulated by configuring
:py:class:`ecoli.processes.antibiotics.exchange_aware_bioscrape.ExchangeAwareBioscrape`
with the path to the generated SBML file.
'''

import os

from biocrnpyler import (
    ChemicalReactionNetwork,
    ParameterEntry,
    ProportionalHillPositive,
    GeneralPropensity,
    Reaction,
    Species,
)


DATA_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), '..', 'data'))
# Calculated by dividing V_max reported in (Nagano & Nikaido, 2009) by the model's initial pump concentration of
# 20.179269875115253 counts / micron^2
CEPH_PUMP_KCAT = 0.0956090147363198  # / units.sec
# Reported in (Nagano & Nikaido, 2009)
CEPH_PUMP_KM = 4.95e-3  # * units.millimolar  # TODO: Placeholder
# Reported in (Galleni et al., 1988)
CEPH_BETA_LACTAMASE_KCAT = 130  # / units.sec
# Reported in (Galleni et al., 1988)
CEPH_BETA_LACTAMASE_KM = 170  # * units.micromolar


# A ProportionalHillPositive propensity generates a propensity:
#
#   p = k*d*s^n/(s^n + K)
#
# Michaelis-Menten kinetics has a rate of the form:
#
#   r = kcat*[E]*[S] \ ([S] + Km)
#
# Therefore, we can model a Michaelis-Menten process using a
# ProportionalHillPositive propensity where:
#
# * k=kcat
# * d=[E]
# * s=[S]
# * n=1
# * K=Km


def main() -> None:
    # Define species.
    cephaloridine_in = Species('cephaloridine_periplasm')
    cephaloridine_out = Species('cephaloridine_environment')
    cephaloridine_hydrolyzed = Species('cephaloridine_hydrolyzed')

    pump = Species('AcrAB')
    beta_lactamase = Species('beta_lactamase')

    species = [
        cephaloridine_in,
        cephaloridine_out,
        cephaloridine_hydrolyzed,
        pump,
        beta_lactamase,
    ]

    # Define reactions.
    export_propensity = ProportionalHillPositive(
        k=ParameterEntry('export_kcat', CEPH_PUMP_KCAT),  # Hz
        s1=cephaloridine_in,
        K=ParameterEntry('export_km', 4.95e-3),  # mM  TODO(Matt): What to put here?
        d=pump,
        n=1.75,
    )
    export = Reaction(
        inputs=[nitrocefin_in],
        outputs=[nitrocefin_out],
        propensity_type=export_propensity,
    )

    hydrolysis_propensity = ProportionalHillPositive(
        k=ParameterEntry('hydrolysis_kcat', 490),  # Hz
        s1=nitrocefin_in,
        K=ParameterEntry('hydrolysis_km', 0.5),  # mM
        d=beta_lactamase,
        n=1,
    )
    hydrolysis = Reaction(
        inputs=[nitrocefin_in],
        outputs=[nitrocefin_hydrolyzed],
        propensity_type=hydrolysis_propensity,
    )

    area_mass_ratio = ParameterEntry('x_am', 132)  # cm^2/mg
    permeability = ParameterEntry('perm', 2e-7)  # cm/sec
    mass = ParameterEntry('mass', 1170e-12)  # mg
    volume = ParameterEntry('volume', 0.32e-12)  # mL
    influx_propensity = GeneralPropensity(
        (
            f'x_am * perm * ({nitrocefin_out} - {nitrocefin_in}) '
            '* mass / (volume)'
        ),
        propensity_species=[nitrocefin_in, nitrocefin_out],
        propensity_parameters=[
            area_mass_ratio, permeability, mass, volume],
    )
    influx = Reaction(
        inputs=[nitrocefin_out],
        outputs=[nitrocefin_in],
        propensity_type=influx_propensity
    )

    initial_concentrations = {
        nitrocefin_in: 0,
        nitrocefin_out: 0.1239,
        nitrocefin_hydrolyzed: 0,
        pump: 0.0004525,
        beta_lactamase: 0.000525,
    }

    crn = ChemicalReactionNetwork(
        species=species,
        reactions=[export, hydrolysis, influx],
        initial_concentration_dict=initial_concentrations,
    )

    path = os.path.join(DATA_DIR, 'bioscrape_sbml.xml')
    print(f'Writing the following CRN to {path}:')
    print(crn.pretty_print(show_rates=True))
    crn.write_sbml_file(path)


if __name__ == '__main__':
    main()
