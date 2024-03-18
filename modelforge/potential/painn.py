from torch._tensor import Tensor
import torch.nn as nn
from loguru import logger as log
from typing import Dict, Callable, Tuple

from .models import BaseNeuralNetworkPotential
from .utils import Dense
import torch
import torch.nn.functional as F
from openff.units import unit


class PaiNN(BaseNeuralNetworkPotential):
    """PaiNN - polarizable interaction neural network

    References:
       Equivariant message passing for the prediction of tensorial properties and molecular spectra.
       ICML 2021, http://proceedings.mlr.press/v139/schutt21a.html

    """

    def __init__(
        self,
        max_Z: int = 100,
        number_of_atom_features: int = 64,
        number_of_radial_basis_functions: int = 16,
        cutoff: unit.Quantity = 5 * unit.angstrom,
        number_of_interaction_modules: int = 2,
        shared_interactions: bool = False,
        shared_filters: bool = False,
        epsilon: float = 1e-8,
    ):

        log.debug("Initializing PaiNN model.")
        self.number_of_interaction_modules = number_of_interaction_modules
        self.number_of_atom_features = number_of_atom_features
        self.only_unique_pairs = False  # NOTE: for pairlist
        self.shared_filters = shared_filters
        super().__init__(cutoff=cutoff)

        # embedding
        from modelforge.potential.utils import Embedding

        self.embedding_module = Embedding(max_Z, number_of_atom_features)

        # initialize the energy readout
        from .utils import FromAtomToMoleculeReduction

        self.readout_module = FromAtomToMoleculeReduction()

        # initialize representation block
        self.representation_module = PaiNNRepresentation(
            cutoff,
            number_of_radial_basis_functions,
            number_of_interaction_modules,
            number_of_atom_features,
            shared_filters,
            self.device,
        )

        # initialize the interaction and mixing networks
        self.interaction_modules = nn.ModuleList(
            PaiNNInteraction(number_of_atom_features, activation=F.silu)
            for _ in range(number_of_interaction_modules)
        )
        self.mixing_modules = nn.ModuleList(
            PaiNNMixing(number_of_atom_features, activation=F.silu, epsilon=epsilon)
            for _ in range(number_of_interaction_modules)
        )

        self.energy_layer = nn.Sequential(
            Dense(
                number_of_atom_features, number_of_atom_features, activation=nn.ReLU()
            ),
            Dense(
                number_of_atom_features,
                1,
            ),
        )

    def _model_specific_input_preparation(self, inputs: Dict[str, torch.Tensor]):
        # Perform atomic embedding

        inputs["atomic_embedding"] = self.embedding_module(inputs["atomic_numbers"])
        return inputs

    def _forward(
        self,
        inputs: Dict[str, torch.Tensor],
    ):
        """
        Compute atomic representations/embeddings.

        Parameters
        ----------
        input : Dict[str, torch.Tensor]
            Dictionary containing pairlist information.
        atomic_embedding : torch.Tensor
            Tensor containing atomic number embeddings.

        Returns
        -------
        Dict[str, torch.Tensor]
            Dictionary containing scalar and vector representations.
        """

        # initialize filters, q and mu
        transformed_input = self.representation_module(inputs)

        filter_list = transformed_input["filters"]
        q = transformed_input["q"]
        mu = transformed_input["mu"]
        dir_ij = transformed_input["dir_ij"]

        for i, (interaction_mod, mixing_mod) in enumerate(
            zip(self.interaction_modules, self.mixing_modules)
        ):
            q, mu = interaction_mod(
                q,
                mu,
                filter_list[i],
                dir_ij,
                inputs["pair_indices"],
            )
            q, mu = mixing_mod(q, mu)

        # Use squeeze to remove dimensions of size 1
        q = q.squeeze(dim=1)
        E_i = self.energy_layer(q).squeeze(1)

        return {
            "E_i": E_i,
            "mu": mu,
            "q": q,
            "atomic_subsystem_indices": inputs["atomic_subsystem_indices"],
        }


from openff.units import unit


class PaiNNRepresentation(nn.Module):
    """PaiNN representation module"""

    def __init__(
        self,
        cutoff: unit = 5 * unit.angstrom,
        number_of_radial_basis_functions: int = 16,
        nr_interaction_blocks: int = 3,
        nr_atom_basis: int = 8,
        shared_filters: bool = False,
        device: torch.device = torch.device("cpu"),
    ):
        super().__init__()

        # cutoff
        from modelforge.potential import CosineCutoff

        self.cutoff_module = CosineCutoff(cutoff, device)

        # radial symmetry function
        from .utils import SchnetRadialSymmetryFunction

        self.radial_symmetry_function_module = SchnetRadialSymmetryFunction(
            number_of_radial_basis_functions=number_of_radial_basis_functions,
            max_distance=cutoff,
            dtype=torch.float32,
        )

        # initialize the filter network
        if shared_filters:
            filter_net = Dense(
                number_of_radial_basis_functions,
                3 * nr_atom_basis,
            )

        else:
            filter_net = Dense(
                number_of_radial_basis_functions,
                nr_interaction_blocks * nr_atom_basis * 3,
                activation=None,
            )

        self.filter_net = filter_net

        self.shared_filters = shared_filters
        self.nr_interaction_blocks = nr_interaction_blocks
        self.nr_atom_basis = nr_atom_basis

    def forward(self, inputs: Dict[str, torch.Tensor]):
        """
        Transforms the input data for the PAInn potential model.

        Parameters
        ----------
        inputs (Dict[str, torch.Tensor]): A dictionary containing the input tensors.
            - "d_ij" (torch.Tensor): Pairwise distances between atoms. Shape: (n_pairs, 1).
            - "r_ij" (torch.Tensor): Displacement vector between atoms. Shape: (n_pairs, 3).
            - "atomic_embedding" (torch.Tensor): Embeddings of atomic numbers. Shape: (n_atoms, embedding_dim).

        Returns:
        ----------
        Dict[str, torch.Tensor]:
            A dictionary containing the transformed input tensors.
            - "mu" (torch.Tensor)
                Zero-initialized tensor for atom features. Shape: (n_atoms, 3, nr_atom_basis).
            - "dir_ij" (torch.Tensor)
                Direction vectors between atoms. Shape: (n_pairs, 1, distance).
            - "q" (torch.Tensor): Reshaped atomic number embeddings. Shape: (n_atoms, 1, embedding_dim).
        """

        # compute pairwise distances
        d_ij = inputs["d_ij"]
        r_ij = inputs["r_ij"]
        dir_ij = r_ij / d_ij  # shape (nr_of_pairs, 3)

        f_ij = self.radial_symmetry_function_module(d_ij)

        fcut = self.cutoff_module(d_ij)  # nr_of_pairs, nr_of_radial_basis_functions

        filters = self.filter_net(f_ij) * fcut

        if self.shared_filters:
            filter_list = [filters] * self.nr_interaction_blocks
        else:
            filter_list = torch.split(filters, 3 * self.nr_atom_basis, dim=-1)

        # generate q and mu
        atomic_embedding = inputs["atomic_embedding"]
        q = atomic_embedding[:, None]  # nr_of_atoms, 1, nr_atom_basis
        q_shape = q.shape
        mu = torch.zeros(
            (q_shape[0], 3, q_shape[2]), device=q.device
        )  # nr_of_atoms, 3, nr_atom_basis

        return {"filters": filter_list, "dir_ij": dir_ij, "q": q, "mu": mu}


class PaiNNInteraction(nn.Module):
    """
    PaiNN Interaction Block for Modeling Equivariant Interactions of Atomistic Systems.

    """

    def __init__(self, nr_atom_basis: int, activation: Callable):
        """
        Parameters
        ----------
        nr_atom_basis : int
            Number of features to describe atomic environments.
        activation : Callable
            Activation function to use.

        Attributes
        ----------
        nr_atom_basis : int
            Number of features to describe atomic environments.
        interatomic_net : nn.Sequential
            Neural network for interatomic interactions.
        """
        super().__init__()
        self.nr_atom_basis = nr_atom_basis

        # Initialize the intra-atomic neural network
        self.interatomic_net = nn.Sequential(
            Dense(nr_atom_basis, nr_atom_basis, activation=activation),
            Dense(nr_atom_basis, 3 * nr_atom_basis, activation=None),
        )

    def forward(
        self,
        q: torch.Tensor,  # shape [nr_of_atoms, nr_atom_basis]
        mu: torch.Tensor,  # shape [nr_of_atoms, nr_atom_basis]
        W_ij: torch.Tensor,  # shape
        dir_ij: torch.Tensor,
        pairlist: torch.Tensor,
    ) -> Tuple[torch.Tensor, torch.Tensor]:
        """Compute interaction output.

        Parameters
        ----------
        q : torch.Tensor
            Scalar input values of shape [nr_of_atoms, nr_atom_basis].
        mu : torch.Tensor
            Vector input values of shape [nr_of_atoms, nr_atom_basis].
        Wij : torch.Tensor
            Filter of shape [n_interactions].
        dir_ij : torch.Tensor
            Directional vector between atoms i and j.
        pairlist : torch.Tensor, shape (2, n_pairs)

        Returns
        -------
        Tuple[torch.Tensor, torch.Tensor]
            Updated scalar and vector representations (q, mu).
        """
        # inter-atomic
        idx_i, idx_j = pairlist[0], pairlist[1]

        x = self.interatomic_net(q)
        nr_of_atoms = q.shape[0]

        xj = x[idx_j]
        muj = mu[idx_j]  # shape (nr_of_pairs, nr_atom_basis)
        W_ij = W_ij.unsqueeze(1)
        x = W_ij * xj

        dq, dmuR, dmumu = torch.split(x, self.nr_atom_basis, dim=-1)
        from torch_scatter import scatter_add

        dq = scatter_add(
            dq, idx_i, dim=0
        )  # dq: (nr_of_pairs, nr_atom_basis); idx_i: (nr_of_pairs)
        ##########
        dmu = (
            dmuR * dir_ij[..., None] + dmumu * muj
        )  # shape (nr_of_pairs, 3, nr_atom_basis)
        dmu = scatter_add(dmu, idx_i, dim=0)  # nr_of_atoms, 3, nr_atom_basis

        q = q + dq
        mu = mu + dmu

        return q, mu


class PaiNNMixing(nn.Module):
    r"""PaiNN interaction block for mixing on atom features."""

    def __init__(self, nr_atom_basis: int, activation: Callable, epsilon: float = 1e-8):
        """
        Parameters
        ----------
        nr_atom_basis : int
            Number of features to describe atomic environments.
        activation : Callable
            Activation function to use.
        epsilon : float, optional
            Stability constant added in norm to prevent numerical instabilities. Default is 1e-8.

        Attributes
        ----------
        nr_atom_basis : int
            Number of features to describe atomic environments.
        intra_atomic_net : nn.Sequential
            Neural network for intra-atomic interactions.
        mu_channel_mix : nn.Sequential
            Neural network for mixing mu channels.
        epsilon : float
            Stability constant for numerical stability.
        """
        super().__init__()
        self.nr_atom_basis = nr_atom_basis

        # initialize the intra-atomic neural network
        self.intra_atomic_net = nn.Sequential(
            Dense(2 * nr_atom_basis, nr_atom_basis, activation=activation),
            Dense(nr_atom_basis, 3 * nr_atom_basis, activation=None),
        )
        # initialize the mu channel mixing network
        self.mu_channel_mix = Dense(nr_atom_basis, 2 * nr_atom_basis, bias=False)
        self.epsilon = epsilon

    def forward(self, q: torch.Tensor, mu: torch.Tensor):
        """
        compute intratomic mixing

        Parameters
        ----------
        q : torch.Tensor
            Scalar input values.
        mu : torch.Tensor
            Vector input values.

        Returns
        -------
        Tuple[torch.Tensor, torch.Tensor]
            Updated scalar and vector representations (q, mu).
        """
        mu_mix = self.mu_channel_mix(mu)
        mu_V, mu_W = torch.split(mu_mix, self.nr_atom_basis, dim=-1)
        mu_Vn = torch.sqrt(torch.sum(mu_V**2, dim=-2, keepdim=True) + self.epsilon)

        ctx = torch.cat([q, mu_Vn], dim=-1)
        x = self.intra_atomic_net(ctx)

        dq_intra, dmu_intra, dqmu_intra = torch.split(x, self.nr_atom_basis, dim=-1)
        dmu_intra = dmu_intra * mu_W

        dqmu_intra = dqmu_intra * torch.sum(mu_V * mu_W, dim=1, keepdim=True)

        q = q + dq_intra + dqmu_intra
        mu = mu + dmu_intra
        return q, mu
