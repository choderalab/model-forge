import os

import pytest

from modelforge.potential.painn import PaiNN
from .helper_functions import (
    setup_simple_model,
    SIMPLIFIED_INPUT_DATA,
)


IN_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"


@pytest.mark.parametrize("lightning", [True, False])
def test_PaiNN_init(lightning):
    """Test initialization of the PaiNN neural network potential."""

    painn = setup_simple_model(PaiNN, lightning=lightning)
    assert painn is not None, "PaiNN model should be initialized."


from openff.units import unit


@pytest.mark.parametrize("lightning", [True, False])
@pytest.mark.parametrize("input_data", SIMPLIFIED_INPUT_DATA)
@pytest.mark.parametrize(
    "model_parameter",
    (
        [64, 50, 2, unit.Quantity(5.0, unit.angstrom), 2],
        [32, 60, 10, unit.Quantity(7.0, unit.angstrom), 1],
        [128, 120, 5, unit.Quantity(5.0, unit.angstrom), 3],
    ),
)
def test_painn_forward(lightning, input_data, model_parameter):
    """
    Test the forward pass of the Schnet model.
    """
    print(f"model_parameter: {model_parameter}")
    (
        nr_atom_basis,
        max_atomic_number,
        number_of_gaussians,
        cutoff,
        nr_interaction_blocks,
    ) = model_parameter
    painn = setup_simple_model(
        PaiNN,
        lightning=lightning,
        nr_atom_basis=nr_atom_basis,
        max_atomic_number=max_atomic_number,
        number_of_gaussians=number_of_gaussians,
        cutoff=cutoff,
        nr_interaction_blocks=nr_interaction_blocks,
    )
    energy = painn(input_data)["E_predict"]
    nr_of_mols = input_data["atomic_subsystem_indices"].unique().shape[0]

    assert (
        len(energy) == nr_of_mols
    )  # Assuming energy is calculated per sample in the batch


def test_painn_interaction_equivariance():
    import torch
    from torch import nn
    from .helper_functions import generate_methane_input, setup_simple_model
    from modelforge.potential.painn import PaiNN
    from modelforge.potential.utils import _distance_to_radial_basis

    # define a rotation matrix in 3D that rotates by 90 degrees around the z-axis
    # (clockwise when looking along the z-axis towards the origin)
    rotation_matrix = torch.tensor([[0.0, 1.0, 0.0], [-1.0, 0.0, 0.0], [0.0, 0.0, 1.0]])

    painn = setup_simple_model(PaiNN)
    methane_input = generate_methane_input()
    perturbed_methane_input = methane_input.copy()
    perturbed_methane_input["positions"] = torch.matmul(
        methane_input["positions"], rotation_matrix
    )

    # prepare reference and perturbed inputs
    reference_prepared_input = painn.prepare_inputs(methane_input)
    reference_d_ij = reference_prepared_input["d_ij"]
    reference_r_ij = reference_prepared_input["r_ij"]
    reference_dir_ij = reference_r_ij / reference_d_ij
    reference_f_ij, _ = _distance_to_radial_basis(
        reference_d_ij, painn.radial_symmetry_function_module
    )

    perturbed_prepared_input = painn.prepare_inputs(perturbed_methane_input)
    perturbed_d_ij = perturbed_prepared_input["d_ij"]
    perturbed_r_ij = perturbed_prepared_input["r_ij"]
    perturbed_dir_ij = perturbed_r_ij / perturbed_d_ij
    perturbed_f_ij, _ = _distance_to_radial_basis(
        perturbed_d_ij, painn.radial_symmetry_function_module
    )

    # check that the invariant properties are preserved
    # d_ij is the distance between atom i and j
    # f_ij is the radial basis function of d_ij
    assert torch.allclose(reference_d_ij, perturbed_d_ij)
    assert torch.allclose(reference_f_ij, perturbed_f_ij)

    # what shoudl not be invariant is the direction
    assert not torch.allclose(reference_dir_ij, perturbed_dir_ij)

    # Check for equivariance
    # rotate the reference dir_ij
    rotated_reference_dir_ij = torch.matmul(reference_dir_ij, rotation_matrix)
    # Compare the rotated original dir_ij with the dir_ij from rotated positions
    assert torch.allclose(rotated_reference_dir_ij, perturbed_dir_ij)

    # Test that the interaction block is equivariant
    # First we test the transformed inputs
    reference_tranformed_inputs = painn._generate_representation(
        reference_prepared_input
    )
    perturbed_tranformed_inputs = painn._generate_representation(
        perturbed_prepared_input
    )

    assert torch.allclose(
        reference_tranformed_inputs["q"], perturbed_tranformed_inputs["q"]
    )
    assert torch.allclose(
        reference_tranformed_inputs["mu"], perturbed_tranformed_inputs["mu"]
    )

    painn_interaction = painn.interaction_modules[0]

    reference_r = painn_interaction(
        reference_tranformed_inputs["q"],
        reference_tranformed_inputs["mu"],
        painn.filter_list[0],
        reference_dir_ij,
        reference_prepared_input["pair_indices"],
    )

    perturbed_r = painn_interaction(
        perturbed_tranformed_inputs["q"],
        perturbed_tranformed_inputs["mu"],
        painn.filter_list[0],
        perturbed_dir_ij,
        perturbed_prepared_input["pair_indices"],
    )

    perturbed_q, perturbed_mu = perturbed_r
    reference_q, reference_mu = reference_r

    # mu is different, q is invariant
    assert torch.allclose(reference_q, perturbed_q)
    assert not torch.allclose(reference_mu, perturbed_mu)

    mixed_reference_q, mixed_reference_mu = painn.mixing_modules[0](
        reference_q, reference_mu
    )
    mixed_perturbed_q, mixed_perturbed_mu = painn.mixing_modules[0](
        perturbed_q, perturbed_mu
    )

    # q is a scalar property and invariant
    assert torch.allclose(mixed_reference_q, mixed_perturbed_q, atol=1e-2)
    # mu is a vector property and should not be invariant
    assert not torch.allclose(mixed_reference_mu, mixed_perturbed_mu)
