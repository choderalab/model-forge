from typing import Dict, List, Tuple

import torch
import torch.nn as nn
from loguru import logger as log

from modelforge.potential.utils import Dense

from .models import NNPInputTuple, PairlistData


class AimNet2Core(torch.nn.Module):
    def __init__(
        self,
        featurization: Dict[str, Dict[str, int]],
        number_of_radial_basis_functions: int,
        number_of_interaction_modules: int,
        activation_function_parameter: Dict[str, str],
        predicted_properties: List[str],
        predicted_dim: List[int],
        maximum_interaction_radius: float,
    ) -> None:
        """
        Core architecture of the AimNet2 model for molecular property
        prediction.

        Parameters
        ----------
        featurization : Dict[str, Dict[str, int]]
            Configuration dictionary specifying feature details for atomic
            embeddings.
        number_of_radial_basis_functions : int
            Number of radial basis functions used in the radial symmetry
            function.
        number_of_interaction_modules : int
            Number of interaction modules in the model, determining the depth of
            message passing.
        activation_function_parameter : Dict[str, str]
            Configuration of activation functions used across the model.
        predicted_properties : List[str]
            List of properties that the model is predicting (e.g., energy,
            forces).
        predicted_dim : List[int]
            The dimensionality of each predicted property.
        maximum_interaction_radius : float
            The cutoff radius for atomic interactions in the model.
        """

        super().__init__()

        log.debug("Initializing the AimNet2 architecture.")

        self.activation_function = activation_function_parameter["activation_function"]

        # Initialize representation block
        self.representation_module = AIMNet2Representation(
            maximum_interaction_radius,
            number_of_radial_basis_functions,
            featurization_config=featurization,
        )
        number_of_per_atom_features = int(
            featurization["atomic_number"]["number_of_per_atom_features"]
        )

        # Define interaction modules for message passing
        self.interaction_modules = torch.nn.ModuleList(
            [
                AIMNet2Interaction(
                    MessageModule(
                        number_of_per_atom_features, is_first_module=(i == 0)
                    ),
                    number_of_input_features=(
                        2 * (number_of_per_atom_features + 1)
                        if i > 0
                        else number_of_per_atom_features + 1
                    ),
                    number_of_per_atom_features=number_of_per_atom_features,
                    activation_function=self.activation_function,
                )
                for i in range(number_of_interaction_modules)
            ]
        )
        # Define output layers to calculate per-atom predictions
        self.output_layers = nn.ModuleDict()
        for property, dim in zip(predicted_properties, predicted_dim):
            self.output_layers[property] = nn.Sequential(
                Dense(
                    number_of_per_atom_features,
                    number_of_per_atom_features,
                    activation_function=self.activation_function,
                ),
                Dense(
                    number_of_per_atom_features,
                    int(dim),
                ),
            )
        from modelforge.potential.processing import ChargeConservation

        self.charge_conservation = ChargeConservation()

    def compute_properties(
        self,
        data: NNPInputTuple,
        pairlist: PairlistData,
    ) -> Dict[str, torch.Tensor]:
        """
        Calculate the requested properties for a given input batch.

        Parameters
        ----------
        data : NNPInput
            The input data for the model.
        pairlist: PairlistData
            The output from the pairlist module.
        Returns
        -------
        Dict[str, torch.Tensor]
            The calculated per-atom scalar representations and atomic subsystem
            indices.
        """

        representation = self.representation_module(data, pairlist)

        f_ij_cutoff = torch.mul(representation["f_ij"], representation["f_cutoff"])
        # Atomic embedding "a" Eqn. (3)
        atomic_embedding = representation["atomic_embedding"]
        partial_charges = torch.zeros(
            (atomic_embedding.shape[0], 1), device=atomic_embedding.device
        )

        # Perform message passing using interaction modules
        for interaction in self.interaction_modules:

            delta_a, delta_q = interaction(
                atomic_embedding,
                pairlist.pair_indices,
                f_ij_cutoff,
                pairlist.r_ij,
                partial_charges,
            )

            # Update atomic embeddings and partial charges
            atomic_embedding = atomic_embedding + delta_a
            partial_charges = partial_charges + delta_q

            partial_charges = self.charge_conservation(
                {
                    "per_atom_charge": partial_charges.squeeze(-1),
                    "per_molecule_charge": data.total_charge.to(dtype=torch.float32),
                    "atomic_subsystem_indices": data.atomic_subsystem_indices.to(
                        dtype=torch.int64
                    ),
                }
            )["per_atom_charge"].unsqueeze(-1)

        return {
            "per_atom_scalar_representation": atomic_embedding,
            "atomic_subsystem_indices": data.atomic_subsystem_indices,
            "atomic_numbers": data.atomic_numbers,
        }

    def forward(
        self,
        data: NNPInputTuple,
        pairlist_output: PairlistData,
    ) -> Dict[str, torch.Tensor]:
        """
        Implements the forward pass through the network.

        Parameters
        ----------
        data : NNPInput
            Contains input data for the batch obtained directly from the
            dataset, including atomic numbers, positions, and other relevant
            fields.
        pairlist_output : PairListOutputs
            Contains the indices for the selected pairs and their associated
            distances and displacement vectors.

        Returns
        -------
        Dict[str, torch.Tensor]
            The calculated per-atom properties and other properties from the
            forward pass.
        """
        # perform the forward pass implemented in the subclass
        results = self.compute_properties(data, pairlist_output)
        atomic_embedding = results["per_atom_scalar_representation"]

        # Compute all specified outputs
        for output_name, output_layer in self.output_layers.items():
            results[output_name] = output_layer(atomic_embedding).squeeze(-1)
        return results


class MessageModule(torch.nn.Module):
    def __init__(
        self,
        number_of_per_atom_features: int,
        is_first_module: bool = False,
    ):
        """
        Initialize the MessageModule which can behave like either the first or subsequent module.

        Parameters
        ----------
        number_of_per_atom_features : int
            The number of features per atom.
        is_first_module : bool, optional
            Whether this is the first message module or a subsequent one.
        """
        super().__init__()
        self.number_of_per_atom_features = number_of_per_atom_features
        self.is_first_module = is_first_module

        # Separate linear layers for embeddings and charges
        self.linear_transform_embeddings = nn.Linear(
            number_of_per_atom_features, number_of_per_atom_features
        )
        self.linear_transform_charges = nn.Linear(
            number_of_per_atom_features, number_of_per_atom_features
        )  # For partial charges

    def calculate_contributions(
        self,
        per_atom_feature_tensor: torch.Tensor,
        pair_indices: torch.Tensor,
        f_ij_cutoff: torch.Tensor,
        r_ij: torch.Tensor,
        use_charge_layer: bool = False,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Calculate the radial and vector contributions for the given features.

        Parameters
        ----------
        per_atom_feature_tensor : torch.Tensor
            Feature tensor (either atomic embeddings or repeated partial charges).
        pair_indices : torch.Tensor
            List of atom pairs.
        f_ij_cutoff : torch.Tensor
            Cutoff function applied to the radial symmetry functions.
        r_ij : torch.Tensor
            Displacement vectors between atom pairs.
        use_charge_layer : bool, optional
            Whether to apply the linear charge transformation.


        Returns
        -------
        Tuple[torch.Tensor, torch.Tensor]
            Radial and vector contributions.
        """

        idx_j = pair_indices[1]

        # Calculate the unit vector u_ij
        r_ij_norm = torch.norm(r_ij, dim=1, keepdim=True)  # Shape: (num_atom_pairs, 1)
        u_ij = r_ij / r_ij_norm  # Shape: (num_atom_pairs, 3)

        # Step 1: Radial Contributions Calculation (Equation 4)
        proto_v_r_a = (
            f_ij_cutoff * per_atom_feature_tensor[idx_j]
        )  # Shape: (num_atom_pairs, nr_of_features)

        # Initialize tensor to accumulate radial contributions for each atom
        radial_contributions = torch.zeros(
            (per_atom_feature_tensor.shape[0], self.number_of_per_atom_features),
            device=per_atom_feature_tensor.device,
            dtype=per_atom_feature_tensor.dtype,
        )  # Shape: (num_of_atoms, nr_of_features)

        # Accumulate the radial contributions using index_add_
        radial_contributions.index_add_(0, idx_j, proto_v_r_a)

        # Step 2: Vector Contributions Calculation (Equation 5)
        # First, calculate the directional component by multiplying g_ij with u_ij
        vector_prot_step1 = u_ij.unsqueeze(-1) * f_ij_cutoff.unsqueeze(
            -2
        )  # Shape: (num_atom_pairs, 3, nr_of_features)

        # Next, multiply this result by the input of atom j
        vector_prot_step2 = vector_prot_step1 * per_atom_feature_tensor[
            idx_j
        ].unsqueeze(
            1
        )  # Shape: (num_atom_pairs, 3, nr_of_features)

        # Optionally apply charge layer transformation
        if use_charge_layer:
            proto_v_r_a = self.linear_transform_charges(proto_v_r_a)
        else:
            proto_v_r_a = self.linear_transform_embeddings(proto_v_r_a)

        # Sum over the last dimension (nr_of_features) to reduce it
        vector_prot_step2 = vector_prot_step2.sum(dim=-1)  # Shape: (num_atom_pairs, 3)

        # Initialize tensor to accumulate vector contributions for each atom
        vector_contributions = torch.zeros(
            per_atom_feature_tensor.shape[0],
            3,
            device=per_atom_feature_tensor.device,
            dtype=vector_prot_step2.dtype,
        )  # Shape: (num_of_atoms, 3)

        # Accumulate the vector contributions using index_add_
        vector_contributions.index_add_(0, idx_j, vector_prot_step2)

        # Step 3: Compute the Euclidean Norm for each atom
        vector_norms = torch.norm(
            vector_contributions, p=2, dim=1
        )  # Shape: (num_of_atoms,)

        return radial_contributions, vector_norms

    def forward(
        self,
        atomic_embedding: torch.Tensor,
        partial_charges: torch.Tensor,
        pair_indices: torch.Tensor,
        f_ij_cutoff: torch.Tensor,
        r_ij: torch.Tensor,
    ) -> torch.Tensor:
        """
        Forward pass of the message module.

        Parameters
        ----------
        atomic_embedding : torch.Tensor
            The embedding of each atom.
        partial_charges : torch.Tensor
            The partial charges of each atom.
        pair_indices : torch.Tensor
            The list of atom pairs.
        f_ij_cutoff : torch.Tensor
            The cutoff function applied to the radial symmetry functions.
        r_ij : torch.Tensor
            The displacement vectors between atom pairs.

        Returns
        -------
        torch.Tensor
            Updated atomic embeddings and partial charges.
        """

        # Step 1: Calculate radial and vector contributions for atomic embeddings (Equation 4 and 5)
        radial_contributions_emb, vector_contributions_emb = (
            self.calculate_contributions(
                atomic_embedding,
                pair_indices,
                f_ij_cutoff,
                r_ij,
                use_charge_layer=False,
            )
        )

        if not self.is_first_module:
            # For subsequent message modules, calculate contributions for charges too
            radial_contributions_charge, vector_contributions_charge = (
                self.calculate_contributions(
                    partial_charges,
                    pair_indices,
                    f_ij_cutoff,
                    r_ij,
                    use_charge_layer=True,
                )
            )

            # Combine contributions
            feature_vector_emb = torch.cat(
                [radial_contributions_emb, vector_contributions_emb.unsqueeze(1)], dim=1
            )
            feature_vector_charge = torch.cat(
                [radial_contributions_charge, vector_contributions_charge.unsqueeze(1)],
                dim=1,
            )

            return torch.cat([feature_vector_emb, feature_vector_charge], dim=1)

        # For the first message module, only return the atomic embedding contributions
        feature_vector = torch.cat(
            [radial_contributions_emb, vector_contributions_emb.unsqueeze(1)], dim=1
        )
        return feature_vector


class AIMNet2Interaction(nn.Module):
    def __init__(
        self,
        message_module: torch.nn.Module,
        number_of_input_features: int,
        number_of_per_atom_features: int,
        activation_function: torch.nn.Module,
    ):
        """
        Initialize the AIMNet2Interaction module.

        Parameters
        ----------
        message_module : nn.Module
            The message passing module to be used.
        number_of_input_features : int
            The number of input features for the interaction.
        number_of_per_atom_features : int
            The number of features per atom.
        activation_function : nn.Module
            The activation function to be used in the interaction module.
        """
        super().__init__()
        self.message_module = message_module
        self.shared_layers = nn.Sequential(
            Dense(
                in_features=number_of_input_features,
                out_features=128,
                activation_function=activation_function,
            ),
            Dense(
                in_features=128,
                out_features=64,
                activation_function=activation_function,
            ),
        )
        self.delta_a_mlp = nn.Sequential(
            self.shared_layers,
            Dense(
                in_features=64,
                out_features=32,
                activation_function=activation_function,
            ),
            Dense(
                in_features=32,
                out_features=number_of_per_atom_features,
            ),
        )
        self.delta_q_mlp = nn.Sequential(
            self.shared_layers,
            Dense(
                in_features=64,
                out_features=32,
                activation_function=activation_function,
            ),
            Dense(
                in_features=32,
                out_features=1,
            ),
        )

    def forward(
        self,
        atomic_embedding: torch.Tensor,
        pair_indices: torch.Tensor,
        f_ij_cutoff: torch.Tensor,
        r_ij: torch.Tensor,
        partial_charges: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """
        Forward pass of the AIMNet2Interaction module.

        Parameters
        ----------
        atomic_embedding : torch.Tensor
            The embedding of each atom.
        pairlist : torch.Tensor
            The list of atom pairs.
        f_ij_cutoff : torch.Tensor
            The cutoff function applied to the radial symmetry functions.
        r_ij : torch.Tensor
            The displacement vectors between atom pairs.
        partial_charges : Optional[torch.Tensor], optional
            The partial point charges for atoms, by default None.

        Returns
        -------
        Tuple[torch.Tensor, torch.Tensor]
            Updated atomic embeddings and partial charges.
        """
        combined_message = self.message_module(
            atomic_embedding,
            partial_charges,
            pair_indices,
            f_ij_cutoff,
            r_ij,
        )

        delta_a = self.delta_a_mlp(combined_message)
        delta_q = self.delta_q_mlp(combined_message)

        return delta_a, delta_q


class AIMNet2Representation(nn.Module):
    def __init__(
        self,
        radial_cutoff: float,
        number_of_radial_basis_functions: int,
        featurization_config: Dict[str, Dict[str, int]],
    ):
        """
        Initialize the AIMNet2 representation layer.

        Parameters
        ----------
        radial_cutoff : float
            The cutoff distance for the radial symmetry function in nanometer.
        number_of_radial_basis_functions : int
            Number of radial basis functions to use.
        featurization_config : Dict[str, Union[List[str], int]]
            Configuration for the featurization process.
        """
        super().__init__()

        self.radial_symmetry_function_module = self._setup_radial_symmetry_functions(
            radial_cutoff, number_of_radial_basis_functions
        )
        # Initialize cutoff module
        from modelforge.potential import CosineAttenuationFunction
        from modelforge.potential.featurization import FeaturizeInput

        self.featurize_input = FeaturizeInput(featurization_config)
        self.cutoff_module = CosineAttenuationFunction(radial_cutoff)

    def _setup_radial_symmetry_functions(
        self, radial_cutoff: float, number_of_radial_basis_functions: int
    ):
        from modelforge.potential import SchnetRadialBasisFunction

        radial_symmetry_function = SchnetRadialBasisFunction(
            number_of_radial_basis_functions=number_of_radial_basis_functions,
            max_distance=radial_cutoff,
            dtype=torch.float32,
        )
        return radial_symmetry_function

    def forward(
        self,
        data: NNPInputTuple,
        pairlist_output: PairlistData,
    ) -> Dict[str, torch.Tensor]:
        """
        Generate the radial symmetry representation of the pairwise distances.

        Parameters
        ----------
        data : NNPInputTuple
            The input data including atomic positions and numbers.
        pairlist_output : PairlistData
            Pairwise distances between atoms and pair indices.

        Returns
        -------
        Dict[str, torch.Tensor]
            The radial basis functions and atomic embeddings.
        """

        # Convert distances to radial basis functions
        f_ij = self.radial_symmetry_function_module(pairlist_output.d_ij)
        # Apply cutoff function to radial basis
        f_cutoff = self.cutoff_module(pairlist_output.d_ij)

        return {
            "f_ij": f_ij,
            "f_cutoff": f_cutoff,
            "atomic_embedding": self.featurize_input(
                data
            ),  # add per-atom properties and embedding
        }
