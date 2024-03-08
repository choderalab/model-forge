import pytest


@pytest.fixture
def setup_methane():
    import torch

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    coordinates = torch.tensor(
        [
            [
                [0.03192167, 0.00638559, 0.01301679],
                [-0.83140486, 0.39370209, -0.26395324],
                [-0.66518241, -0.84461308, 0.20759389],
                [0.45554739, 0.54289633, 0.81170881],
                [0.66091919, -0.16799635, -0.91037834],
            ]
        ],
        requires_grad=True,
        device=device,
    )
    # In periodic table, C = 6 and H = 1
    species = torch.tensor([[1, 0, 0, 0, 0]], device=device)
    atomic_subsystem_indices = torch.tensor(
        [0, 0, 0, 0, 0], dtype=torch.int32, device=device
    )
    mf_input = {
        "atomic_numbers": species,
        "positions": coordinates.squeeze() / 10,
        "atomic_subsystem_indices": atomic_subsystem_indices,
    }

    return species, coordinates, device, mf_input


@pytest.fixture
def setup_two_methanes():
    import torch

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    coordinates = torch.tensor(
        [
            [
                [0.03192167, 0.00638559, 0.01301679],
                [-0.83140486, 0.39370209, -0.26395324],
                [-0.66518241, -0.84461308, 0.20759389],
                [0.45554739, 0.54289633, 0.81170881],
                [0.66091919, -0.16799635, -0.91037834],
            ],
            [
                [0.03192167, 0.00638559, 0.01301679],
                [-0.83140486, 0.39370209, -0.26395324],
                [-0.66518241, -0.84461308, 0.20759389],
                [0.45554739, 0.54289633, 0.81170881],
                [0.66091919, -0.16799635, -0.91037834],
            ],
        ],
        requires_grad=True,
        device=device,
    )
    # In periodic table, C = 6 and H = 1
    species = torch.tensor([[1, 0, 0, 0, 0], [1, 0, 0, 0, 0]], device=device)
    atomic_subsystem_indices = torch.tensor(
        [0, 0, 0, 0, 0, 1, 1, 1, 1, 1], dtype=torch.int32, device=device
    )
    mf_input = {
        "atomic_numbers": torch.cat((species[0], species[1]), dim=0),
        "positions": torch.cat((coordinates[0], coordinates[1]), dim=0) / 10,
        "atomic_subsystem_indices": atomic_subsystem_indices,
    }

    return species, coordinates, device, mf_input


def test_torchani_ani(setup_two_methanes):
    import torch
    import torchani

    species, coordinates, device, _ = setup_two_methanes
    model = torchani.models.ANI2x(periodic_table_index=False).to(device)

    energy = model((species, coordinates)).energies
    derivative = torch.autograd.grad(energy.sum(), coordinates)[0]
    force = -derivative


def test_modelforge_ani(setup_two_methanes):
    from modelforge.potential.ani import ANI2x as mf_ANI2x

    _, _, _, mf_input = setup_two_methanes
    model = mf_ANI2x()
    model(mf_input)


def test_compare_radial_symmetry_features():
    # Compare the ANI radial symmetry function
    # agsint the output of the Modelforge radial symmetry function
    import torch
    from modelforge.potential.utils import RadialSymmetryFunction, CosineCutoff
    from openff.units import unit

    # generate a random list of distances, all < 5
    d_ij = (
        torch.rand(
            5,
        )
        * 5
    )

    # ANI constants
    radial_cutoff = 5.1  # radial_cutoff
    radial_start = 0.8
    radial_dist_divisions = 8
    EtaR = torch.tensor([19.7])  # radial eta
    ShfR = torch.linspace(radial_start, radial_cutoff, radial_dist_divisions + 1)[:-1]

    # NOTE: we pass in Angstrom to ANI and in nanometer to mf
    rsf = RadialSymmetryFunction(
        radial_dist_divisions,
        radial_cutoff * unit.angstrom,
        radial_start * unit.angstrom,
        ani_style=True,
    )
    r_mf = rsf(d_ij / 10)  # torch.Size([5, 8]) # NOTE: nanometer
    cutoff_module = CosineCutoff(5 * unit.angstrom)
    from torchani.aev import radial_terms

    d_cutoff = cutoff_module(d_ij/10)  # torch.Size([5]) # NOTE: nanometer

    r_mf = (r_mf.T * d_cutoff).T
    r_ani = radial_terms(5, EtaR, ShfR, d_ij)  # torch.Size([5,8]) # NOTE: Angstrom
    assert torch.allclose(r_mf, r_ani)


def test_radial_with_diagonal_batching(setup_two_methanes):
    import torch
    from modelforge.potential.utils import RadialSymmetryFunction
    from openff.units import unit
    from modelforge.potential.models import Pairlist

    ani_species, ani_coordinates, _, mf_input = setup_two_methanes
    pairlist = Pairlist()
    pairs = pairlist(
        mf_input["positions"],
        mf_input["atomic_subsystem_indices"],
        only_unique_pairs=True,
    )
    d_ij = pairs["d_ij"].squeeze()

    # ANI constants
    radial_cutoff = 5.1  # radial_cutoff
    radial_start = 0.8
    radial_dist_divisions = 16

    radial_symmetry_function = RadialSymmetryFunction(
        radial_dist_divisions,
        radial_cutoff * unit.angstrom,
        radial_start * unit.angstrom,
        ani_style=True,
    )

    radial_symmetry_feature_vector_mf = radial_symmetry_function(d_ij)
    # ------------ ANI ----------#
    from torchani.aev import radial_terms
    from torchani.aev import neighbor_pairs_nopbc

    EtaR = torch.tensor([19.7])  # radial eta
    ShfR = torch.linspace(radial_start, radial_cutoff, radial_dist_divisions + 1)[:-1]

    ani_coordinates_ = ani_coordinates
    ani_coordinates = ani_coordinates_.flatten(0, 1)

    species = ani_species
    mask = species == -1
    atom_index12 = neighbor_pairs_nopbc(species == -1, ani_coordinates_, radial_cutoff)
    selected_coordinates = ani_coordinates.index_select(0, atom_index12.view(-1)).view(
        2, -1, 3
    )
    vec = selected_coordinates[0] - selected_coordinates[1]
    distances = vec.norm(2, -1)

    radial_symmetry_feature_vector_ani = radial_terms(1, EtaR, ShfR, distances)
    # test that both ANI and MF obtain the same radial symmetry outpu
    assert torch.allclose(
        radial_symmetry_feature_vector_mf, radial_symmetry_feature_vector_ani
    )

    assert radial_symmetry_feature_vector_mf.shape == torch.Size(
        [20, radial_dist_divisions]
    )

    # postprocessing

    num_molecules = 2
    num_atoms = 5
    num_species = 7
    radial_terms_ = radial_symmetry_feature_vector_mf
    radial_sublength = radial_symmetry_function.radial_sublength
    # The length of full radial aev
    radial_length = num_species * radial_sublength

    # radial_sublength = 16
    # radial_length = 112
    radial_aev = radial_terms_.new_zeros(
        (
            num_molecules * num_atoms * num_species,
            radial_sublength,
        )
    )
    atom_index12 = pairs["pair_indices"]
    species = mf_input["atomic_numbers"]
    species12 = species[atom_index12]

    index12 = atom_index12 * num_species + species12.flip(0)
    radial_aev.index_add_(0, index12[0], radial_terms_)
    radial_aev.index_add_(0, index12[1], radial_terms_)
    manual_result = radial_aev

    # radial_aev = radial_aev.reshape(num_molecules, num_atoms, radial_length)


def test_compare_angular_symmetry_features(setup_methane):
    # Compare the Modelforge angular symmetry function
    # against the original torchani implementation

    import torch
    from modelforge.potential.utils import AngularSymmetryFunction, triple_by_molecule
    from openff.units import unit
    from modelforge.potential.models import Pairlist
    import math

    # set up relevant system properties
    species, r, _, _ = setup_methane
    pairlist = Pairlist()
    pairs = pairlist(r[0], torch.tensor([0, 0, 0, 0, 0]), only_unique_pairs=True)
    d_ij = pairs["d_ij"].squeeze(1)
    r_ij = pairs["r_ij"].squeeze(1)

    # reformat for input
    species = species.flatten()
    atom_index12 = pairs["pair_indices"]
    species12 = species[atom_index12]
    # ANI constants
    # for angular features
    angular_cutoff = Rca = 3.5  # angular_cutoff
    angular_start = 0.8
    EtaA = angular_eta = 19.7
    angular_dist_divisions = 8
    ShfA = torch.linspace(angular_start, angular_cutoff, angular_dist_divisions + 1)[
        :-1
    ]
    angle_sections = 4

    angle_start = math.pi / (2 * angle_sections)
    ShfZ = (torch.linspace(0, math.pi, angle_sections + 1) + angle_start)[:-1]

    # other constants
    Zeta = 32.0

    # get index in right order
    even_closer_indices = (d_ij <= Rca).nonzero().flatten()
    atom_index12 = atom_index12.index_select(1, even_closer_indices)
    species12 = species12.index_select(1, even_closer_indices)
    r_ij = r_ij.index_select(0, even_closer_indices)
    central_atom_index, pair_index12, sign12 = triple_by_molecule(atom_index12)
    species12_small = species12[:, pair_index12]
    vec12 = r_ij.index_select(0, pair_index12.view(-1)).view(
        2, -1, 3
    ) * sign12.unsqueeze(-1)
    species12_ = torch.where(sign12 == 1, species12_small[1], species12_small[0])

    # now use formated indices and inputs to calculate the
    # angular terms, both with the modelforge AngularSymmetryFunction
    # and with its implementation in torchani
    from torchani.aev import angular_terms

    # First with ANI
    angular_feature_vector_ani = angular_terms(
        Rca, ShfZ.unsqueeze(0).unsqueeze(0), EtaA, Zeta, ShfA.unsqueeze(1), vec12
    )

    # set up modelforge angular features
    asf = AngularSymmetryFunction(
        angular_cutoff * unit.angstrom,
        angular_start * unit.angstrom,
        angular_dist_divisions,
        angle_sections,
    )
    # NOTE: ANI works with Angstrom, modelforge with nanometer
    vec12 = vec12 / 10
    # NOTE: ANI operates on a [nr_of_molecules, nr_of_atoms, 3] tensor
    angular_feature_vector_mf = asf(vec12)
    # make sure that the output is the same
    assert angular_feature_vector_ani.size() == angular_feature_vector_mf.size()
    assert torch.allclose(angular_feature_vector_ani, angular_feature_vector_mf)
