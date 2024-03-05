import torch
from torch import nn
from loguru import logger as log
from modelforge.potential.models import BaseNNP
from modelforge.potential.postprocessing import PostprocessingPipeline, NoPostprocess
from typing import Dict
from openff.units import unit


class ANIRepresentation(nn.Module):
    # calculate the atomic environment vectors
    # used for the ANI architecture of NNPs

    def __init__(
        self,
        radial_cutoff: unit.Quantity,
        angular_cutoff: unit.Quantity,
        nr_of_supported_elements: int = 7,
    ):
        # radial symmetry functions

        super().__init__()
        self.radial_cutoff = radial_cutoff
        self.angular_cutoff = angular_cutoff
        self.nr_of_supported_elements = nr_of_supported_elements

        self.radial_symmetry_functions = self._setup_radial_symmetry_functions(
            self.radial_cutoff
        )
        self.angular_symmetry_functions = self._setup_angular_symmetry_functions(
            self.angular_cutoff
        )
        # generate indices
        from modelforge.potential.utils import triple_by_molecule

        self.triple_by_molecule = triple_by_molecule

    def _setup_radial_symmetry_functions(self, radial_cutoff: unit.Quantity):
        from openff.units import unit
        from .utils import RadialSymmetryFunction

        # ANI constants
        radial_start = 0.8 * unit.angstrom
        radial_dist_divisions = 16

        radial_symmetry_function = RadialSymmetryFunction(
            radial_dist_divisions,
            radial_cutoff,
            radial_start,
            ani_style=True,
            dtype=torch.float32,
        )
        return radial_symmetry_function

    def _setup_angular_symmetry_functions(self, angular_cutoff: unit.Quantity):
        from .utils import AngularSymmetryFunction
        from openff.units import unit

        # ANI constants for angular features
        angular_start = 0.8 * unit.angstrom
        angular_dist_divisions = 8
        angle_sections = 4

        # set up modelforge angular features
        return AngularSymmetryFunction(
            angular_cutoff,
            angular_start,
            angular_dist_divisions,
            angle_sections,
            dtype=torch.float32,
        )

    def forward(self, inputs: Dict[str, torch.Tensor]):

        # calculate the atomic environment vectors
        # used for the ANI architecture of NNPs
        radial_feature_vector = self.radial_symmetry_functions(inputs["d_ij"])
        postprocessed_radial_aev_and_additional_data = self._postprocess_radial_aev(
            radial_feature_vector, inputs=inputs
        )

        angular_data = self._preprocess_angular_aev(
            postprocessed_radial_aev_and_additional_data
        )
        angular_feature_vector = self.angular_symmetry_functions(angular_data["r_ij"])
        return [radial_feature_vector, angular_feature_vector]

    def _preprocess_angular_aev(self, data: Dict[str, torch.Tensor]):

        atom_index12 = data["atom_index12"]
        species12 = data["species12"]
        vec = data["vec"]

        # compute angular aev
        central_atom_index, pair_index12, sign12 = self.triple_by_molecule(atom_index12)
        species12_small = species12[:, pair_index12]
        vec12 = vec.index_select(0, pair_index12.view(-1)).view(
            2, -1, 3
        ) * sign12.unsqueeze(-1)
        species12_ = torch.where(sign12 == 1, species12_small[1], species12_small[0])
        return {"r_ij": vec12}

    def _postprocess_radial_aev(
        self,
        radial_feature_vector,
        inputs: Dict[str, torch.Tensor],
    ):

        radial_feature_vector = radial_feature_vector.squeeze(1)
        number_of_atoms_in_batch = inputs["number_of_atoms_in_batch"]
        radial_sublength = self.radial_symmetry_functions.radial_sublength
        radial_length = radial_sublength * self.nr_of_supported_elements
        radial_aev = radial_feature_vector.new_zeros(
            (
                number_of_atoms_in_batch * self.nr_of_supported_elements,
                radial_sublength,
            )
        )
        atom_index12 = inputs["pair_indices"]
        species = inputs["atomic_numbers"]
        species12 = species[atom_index12]

        index12 = atom_index12 * self.nr_of_supported_elements + species12.flip(0)
        radial_aev.index_add_(0, index12[0], radial_feature_vector)
        radial_aev.index_add_(0, index12[1], radial_feature_vector)

        # radial_aev = radial_aev.reshape(number_of_atoms_in_batch, radial_length)

        even_closer_indices = (
            (inputs["d_ij"] <= self.angular_cutoff.to(unit.nanometer).m)
            .nonzero()
            .flatten()
        )
        atom_index12 = atom_index12.index_select(1, even_closer_indices)
        species12 = species12.index_select(1, even_closer_indices)
        vec = inputs["d_ij"].index_select(0, even_closer_indices)

        return {
            "radial_aev": radial_aev,
            "atom_index12": atom_index12,
            "species12": species12,
            "vec": vec,
        }


class ANIInteraction(nn.Module):

    def __init__(self, aev_dim: int):
        super().__init__()
        # define atomic neural network
        atomic_neural_networks = self.intialize_atomic_neural_network(aev_dim)
        self.H_network = atomic_neural_networks["H"]
        self.C_network = atomic_neural_networks["C"]
        self.O_network = atomic_neural_networks["O"]
        self.N_network = atomic_neural_networks["N"]
        # self.S_network = atomic_neural_networks["S"]
        # self.F_network = atomic_neural_networks["F"]
        # self.Cl_network = atomic_neural_networks["Cl"]

    def intialize_atomic_neural_network(self, aev_dim: int) -> Dict[str, nn.Module]:

        H_network = torch.nn.Sequential(
            torch.nn.Linear(aev_dim, 160),
            torch.nn.CELU(0.1),
            torch.nn.Linear(160, 128),
            torch.nn.CELU(0.1),
            torch.nn.Linear(128, 96),
            torch.nn.CELU(0.1),
            torch.nn.Linear(96, 1),
        )

        C_network = torch.nn.Sequential(
            torch.nn.Linear(aev_dim, 144),
            torch.nn.CELU(0.1),
            torch.nn.Linear(144, 112),
            torch.nn.CELU(0.1),
            torch.nn.Linear(112, 96),
            torch.nn.CELU(0.1),
            torch.nn.Linear(96, 1),
        )

        N_network = torch.nn.Sequential(
            torch.nn.Linear(aev_dim, 128),
            torch.nn.CELU(0.1),
            torch.nn.Linear(128, 112),
            torch.nn.CELU(0.1),
            torch.nn.Linear(112, 96),
            torch.nn.CELU(0.1),
            torch.nn.Linear(96, 1),
        )

        O_network = torch.nn.Sequential(
            torch.nn.Linear(aev_dim, 128),
            torch.nn.CELU(0.1),
            torch.nn.Linear(128, 112),
            torch.nn.CELU(0.1),
            torch.nn.Linear(112, 96),
            torch.nn.CELU(0.1),
            torch.nn.Linear(96, 1),
        )

        return {"H": H_network, "C": C_network, "N": N_network, "O": O_network}


class ANI2x(BaseNNP):

    def __init__(
        self,
        postprocessing: PostprocessingPipeline = PostprocessingPipeline(
            [NoPostprocess({})]
        ),
        radial_cutoff: unit.Quantity = 5.3 * unit.angstrom,
        angular_cutoff: unit.Quantity = 3.5 * unit.angstrom,
    ) -> None:
        """
        Initialize the ANi NNP architeture.

        Parameters
        ----------
        """
        # number of elements in ANI2x
        self.num_species = 7
        self.only_unique_pairs = True

        log.debug("Initializing ANI model.")
        super().__init__(
            radial_cutoff=radial_cutoff,
            angular_cutoff=angular_cutoff,
            postprocessing=postprocessing,
        )

        # Initialize representation block
        self.ani_representation_module = ANIRepresentation(
            radial_cutoff, angular_cutoff
        )
        # The length of radial aev
        self.radial_length = (
            self.num_species
            * self.ani_representation_module.radial_symmetry_functions.radial_sublength
        )
        # The length of angular aev
        self.angular_length = (
            (self.num_species * (self.num_species + 1))
            // 2
            * self.ani_representation_module.angular_symmetry_functions.angular_sublength
        )

        # The length of full aev
        self.aev_length = self.radial_length + self.angular_length

        # Intialize interaction blocks
        self.interaction_modules = ANIInteraction(self.aev_length)

    def _readout(self, inputs: Dict[str, torch.Tensor]) -> torch.Tensor:
        # Compute the energy for each system
        return self.readout_module(inputs)

    def _model_specific_input_preparation(
        self, inputs: Dict[str, torch.Tensor]
    ) -> Dict[str, torch.Tensor]:

        return inputs

    def _forward(self, inputs: Dict[str, torch.Tensor]) -> torch.Tensor:
        """
        Calculate the energy for a given input batch.

        Parameters
        ----------
        inputs : Dict[str, torch.Tensor]
        - pairlist:  shape (n_pairs, 2)
        - r_ij:  shape (n_pairs, 1)
        - d_ij:  shape (n_pairs, 3)
        - positions:  shape (nr_of_atoms_per_molecules, 3)
        - atomic_embedding:  shape (nr_of_atoms_in_systems, nr_atom_basis)


        Returns
        -------
        torch.Tensor
            Calculated energies; shape (nr_systems,).
        """

        # Compute the representation for each atom
        representation = self.ani_representation_module(inputs)
        a = 7

        return {
            "scalar_representation": x,
            "atomic_subsystem_indices": inputs["atomic_subsystem_indices"],
        }
