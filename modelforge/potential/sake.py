from dataclasses import dataclass

from torch._tensor import Tensor
import torch.nn as nn
from loguru import logger as log
from typing import Dict, Type, Callable, Optional, Tuple
from openff.units import unit

from .models import BaseNeuralNetworkPotential, PairListOutputs
from .utils import Dense, scatter_softmax, CosineCutoff, NNPInput
import torch
import torch.nn.functional as F


class ExpNormalSmearing(torch.nn.Module):
    def __init__(self, cutoff_lower=0.0, cutoff_upper=5.0, n_rbf=50, trainable=True):
        super(ExpNormalSmearing, self).__init__()
        self.cutoff_lower = cutoff_lower
        self.cutoff_upper = cutoff_upper
        self.n_rbf = n_rbf
        self.trainable = trainable
        self.alpha = 5.0 / (cutoff_upper - cutoff_lower)

        means, betas = self._initial_params()
        if trainable:
            self.register_parameter("means", torch.nn.Parameter(means))
            self.register_parameter("betas", torch.nn.Parameter(betas))
        else:
            self.register_buffer("means", means)
            self.register_buffer("betas", betas)

        self.out_features = self.n_rbf

    def _initial_params(self):
        # initialize means and betas according to the default values in PhysNet
        # https://pubs.acs.org/doi/10.1021/acs.jctc.9b00181
        start_value = torch.exp(
            torch.scalar_tensor(-self.cutoff_upper + self.cutoff_lower)
        )
        means = torch.linspace(start_value, 1, self.n_rbf)
        betas = torch.tensor(
            [(2 / self.n_rbf * (1 - start_value)) ** -2] * self.n_rbf
        )
        return means, betas

    def reset_parameters(self):
        means, betas = self._initial_params()
        self.means.data.copy_(means)
        self.betas.data.copy_(betas)

    def forward(self, dist):
        return torch.exp(
            -self.betas *
            (torch.exp(
                self.alpha *
                (-dist.unsqueeze(-1) + self.cutoff_lower))
             - self.means) ** 2
        )


@dataclass
class SAKENeuralNetworkInput:
    """
    A dataclass designed to structure the inputs for SAKE neural network potentials, ensuring
    an efficient and structured representation of atomic systems for energy computation and
    property prediction within the SAKE framework.

    Attributes
    ----------
    atomic_numbers : torch.Tensor
        Atomic numbers for each atom in the system(s). Shape: [num_atoms].
    positions : torch.Tensor
        XYZ coordinates of each atom. Shape: [num_atoms, 3].
    atomic_subsystem_indices : torch.Tensor
        Maps each atom to its respective subsystem or molecule, useful for systems with multiple
        molecules. Shape: [num_atoms].
    pair_indices : torch.Tensor
        Indicates indices of atom pairs, essential for computing pairwise features. Shape: [2, num_pairs].
    number_of_atoms : int
        Total number of atoms in the batch, facilitating batch-wise operations.
    atomic_embedding : torch.Tensor
        Embeddings or features for each atom, potentially derived from atomic numbers or learned. Shape: [num_atoms, embedding_dim].

    Notes
    -----
    The `SAKENeuralNetworkInput` dataclass encapsulates essential inputs required by the SAKE neural network
    model for accurately predicting system energies and properties. It includes atomic positions, atomic types,
    and connectivity information, crucial for a detailed representation of atomistic systems.

    Examples
    --------
    >>> sake_input = SAKENeuralNetworkInput(
    ...     atomic_numbers=torch.tensor([1, 6, 6, 8]),
    ...     positions=torch.tensor([[0.0, 0.0, 0.0], [0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [1.0, 0.0, 0.0]]),
    ...     atomic_subsystem_indices=torch.tensor([0, 0, 0, 0]),
    ...     pair_indices=torch.tensor([[0, 1], [0, 2], [1, 2]]).T,
    ...     number_of_atoms=4,
    ...     atomic_embedding=torch.randn(4, 5)  # Example atomic embeddings
    ... )
    """

    pair_indices: torch.Tensor
    number_of_atoms: int
    positions: torch.Tensor
    atomic_numbers: torch.Tensor
    atomic_subsystem_indices: torch.Tensor
    atomic_embedding: torch.Tensor


class SAKE(BaseNeuralNetworkPotential):
    """SAKE - spatial attention kinetic networks with E(n) equivariance.

    Reference:
        Wang, Yuanqing and Chodera, John D. ICLR 2023. https://openreview.net/pdf?id=3DIpIf3wQMC

    """

    def __init__(
            self,
            nr_atom_basis: int = 64,
            nr_interaction_blocks: int = 2,
            nr_heads: int = 4,
            radial_basis_module: nn.Module = ExpNormalSmearing(),
            cutoff=5 * unit.angstrom,
            epsilon: float = 1e-8,
    ):
        """
        Parameters
            ----------
            nr_atom_basis : int
                Number of features in atomic embeddings. Must be at least the maximum atomic number.
            nr_interaction_blocks : int
                Number of interaction blocks.
            radial_basis_module : torch.Module
                radial basis functions.
            nr_heads: int
                Number of heads for spatial attention.
            cutoff_module : torch.Module
                Cutoff function for the radial basis.
            epsilon : float, optional
                Stability constant to prevent numerical instabilities (default is 1e-8).
        """
        from .utils import FromAtomToMoleculeReduction

        log.debug("Initializing SAKE model.")
        super().__init__(cutoff=cutoff)
        print("self.cutoff", self.calculate_distances_and_pairlist.cutoff)
        self.nr_interaction_blocks = nr_interaction_blocks
        self.nr_heads = nr_heads
        self.radial_basis_module = radial_basis_module
        self.nr_atom_basis = nr_atom_basis

        self.only_unique_pairs = False  # NOTE: for pairlist

        self.cutoff_module = CosineCutoff(cutoff)
        self.energy_layer = nn.Sequential(
            Dense(self.nr_atom_basis, self.nr_atom_basis, activation=torch.nn.SiLU()),
            Dense(self.nr_atom_basis, self.nr_atom_basis, activation=torch.nn.SiLU()),
            Dense(self.nr_atom_basis, 1, activation=None),
        )
        self.readout_module = FromAtomToMoleculeReduction()

        # initialize the interaction networks
        self.interaction_modules = nn.ModuleList(
            SAKEInteraction(nr_atom_basis=self.nr_atom_basis,
                            nr_edge_basis=self.nr_atom_basis,
                            nr_edge_basis_hidden=self.nr_atom_basis,
                            nr_atom_basis_hidden=self.nr_atom_basis,
                            nr_atom_basis_spatial_hidden=self.nr_atom_basis,
                            nr_atom_basis_spatial=self.nr_atom_basis,
                            nr_atom_basis_velocity=self.nr_atom_basis,
                            nr_coefficients=(self.nr_heads * self.nr_atom_basis),
                            nr_heads=self.nr_heads,
                            activation=torch.nn.SiLU(),
                            radial_basis_module=self.radial_basis_module,
                            cutoff_module=self.cutoff_module,
                            epsilon=epsilon)
            for _ in range(self.nr_interaction_blocks)
        )

    def _model_specific_input_preparation(
            self, data: "NNPInput", pairlist_output: "PairListOutputs"
    ) -> SAKENeuralNetworkInput:
        # Perform atomic embedding

        number_of_atoms = data.atomic_numbers.shape[0]

        nnp_input = SAKENeuralNetworkInput(
            pair_indices=pairlist_output.pair_indices,
            number_of_atoms=number_of_atoms,
            positions=data.positions,
            atomic_numbers=data.atomic_numbers,
            atomic_subsystem_indices=data.atomic_subsystem_indices,
            atomic_embedding=F.one_hot(data.atomic_numbers.long(),
                                       num_classes=self.nr_atom_basis).float()
        )

        return nnp_input

    def _forward(
            self,
            data: SAKENeuralNetworkInput
    ):
        """
        Compute atomic representations/embeddings.

        Parameters
        ----------
        inputs: Dict[str, torch.Tensor]
            Dictionary containing pairlist information.

        Returns
        -------
        Dict[str, torch.Tensor]
            Dictionary containing scalar and vector representations.
        """

        # extract properties from pairlist
        h = data.atomic_embedding
        x = data.positions
        v = torch.zeros_like(x)

        for i, interaction_mod in enumerate(self.interaction_modules):
            h, x, v = interaction_mod(h, x, v, data.pair_indices)

        # Use squeeze to remove dimensions of size 1
        h = h.squeeze(dim=1)

        E_i = self.energy_layer(h).squeeze(1)

        return {
            "E_i": E_i,
            "atomic_subsystem_indices": data.atomic_subsystem_indices
        }


class SAKEInteraction(nn.Module):
    """
    Spatial Attention Kinetic Networks Layer.

    Wang and Chodera (2023) Sec. 5 Algorithm 1.
    """

    def __init__(self,
                 nr_atom_basis: int,
                 nr_edge_basis: int,
                 nr_edge_basis_hidden: int,
                 nr_atom_basis_hidden: int,
                 nr_atom_basis_spatial_hidden: int,
                 nr_atom_basis_spatial: int,
                 nr_atom_basis_velocity: int,
                 nr_coefficients: int,
                 nr_heads: int,
                 activation: Callable,
                 radial_basis_module: nn.Module,
                 cutoff_module: nn.Module, epsilon: float):
        """
        Parameters
        ----------
        nr_atom_basis : int
            Number of features in semantic atomic embedding (h).
        nr_edge_basis : int
            Number of edge features after edge update.
        nr_edge_basis_hidden : int
            Number of edge features after hidden layer within edge update.
        nr_atom_basis_hidden : int
            Number of features after hidden layer within node update.
        nr_atom_basis_spatial_hidden : int
            Number of features after hidden layer within spatial attention.
        nr_atom_basis_spatial : int
            Number of features after spatial attention.
        nr_atom_basis_velocity : int
            Number of features after hidden layer within velocity update.
        nr_coefficients : int
            Number of coefficients for spatial attention.
        activation : Callable
            Activation function to use.

        Attributes
        ----------
        nr_atom_basis : int
            Number of features to describe atomic environments.
        """
        super().__init__()
        self.nr_atom_basis = nr_atom_basis
        self.nr_edge_basis = nr_edge_basis
        self.nr_edge_basis_hidden = nr_edge_basis_hidden
        self.nr_atom_basis_hidden = nr_atom_basis_hidden
        self.nr_atom_basis_spatial_hidden = nr_atom_basis_spatial_hidden
        self.nr_atom_basis_spatial = nr_atom_basis_spatial
        self.nr_atom_basis_velocity = nr_atom_basis_velocity
        self.nr_coefficients = nr_coefficients
        self.nr_heads = nr_heads
        self.epsilon = epsilon
        self.radial_basis_module = radial_basis_module
        self.cutoff_module = cutoff_module

        self.node_mlp = nn.Sequential(
            Dense(self.nr_atom_basis + self.nr_heads * self.nr_edge_basis + self.nr_atom_basis_spatial,
                  self.nr_atom_basis_hidden, activation=activation),
            Dense(self.nr_atom_basis_hidden, self.nr_atom_basis, activation=activation)
        )

        self.post_norm_mlp = nn.Sequential(
            Dense(self.nr_coefficients, self.nr_atom_basis_spatial_hidden, activation=activation),
            Dense(self.nr_atom_basis_spatial_hidden, self.nr_atom_basis_spatial, activation=activation)
        )

        self.edge_mlp_in = nn.Linear(self.nr_atom_basis * 2, radial_basis_module.n_rbf)

        self.edge_mlp_out = nn.Sequential(
            Dense(self.nr_atom_basis * 2 + radial_basis_module.n_rbf + 1, self.nr_edge_basis_hidden,
                  activation=activation),
            nn.Linear(nr_edge_basis_hidden, nr_edge_basis),
        )

        self.semantic_attention_mlp = Dense(self.nr_edge_basis, self.nr_heads, activation=nn.CELU(alpha=2.0))

        self.velocity_mlp = nn.Sequential(
            Dense(self.nr_atom_basis, self.nr_atom_basis_velocity, activation=activation),
            Dense(self.nr_atom_basis_velocity, 1, activation=lambda x: 2.0 * F.sigmoid(x), bias=False)
        )

        self.x_mixing_mlp = Dense(self.nr_heads * self.nr_edge_basis, self.nr_coefficients, bias=False,
                                  activation=nn.Tanh())

        self.v_mixing_mlp = Dense(self.nr_coefficients, 1, bias=False)

    def update_edge(self, h_i_by_pair, h_j_by_pair, d_ij):
        """Compute intermediate edge features for semantic attention.

        Wang and Chodera (2023) Sec. 5 Eq. 7.

        Parameters
        ----------
        h_i_by_pair : torch.Tensor
            Node features of receivers, duplicated across pairs. Shape [nr_pairs, nr_atom_basis].
        h_j_by_pair : torch.Tensor
            Node features of senders, duplicated across pairs. Shape [nr_pairs, nr_atom_basis].
        d_ij : torch.Tensor
            Distance between senders and receivers. Shape [nr_pairs, ].

        Returns
        -------
        torch.Tensor
            Intermediate edge features. Shape [nr_pairs, nr_edge_basis].
        """
        h_ij_cat = torch.cat([h_i_by_pair, h_j_by_pair], dim=-1)
        h_ij_filtered = self.radial_basis_module(d_ij) * self.edge_mlp_in(h_ij_cat)
        return self.edge_mlp_out(
            torch.cat([h_ij_cat, h_ij_filtered, d_ij.unsqueeze(-1)], dim=-1)
        )

    def update_node(self, h, h_i_semantic, h_i_spatial):
        """Update node semantic features for the next layer.

        Wang and Chodera (2023) Sec. 2.2 Eq. 4.

        Parameters
        ----------
        h : torch.Tensor
            Input node semantic features. Shape [nr_of_atoms_in_systems, nr_atom_basis].
        h_i_semantic : torch.Tensor
            Node semantic attention. Shape [nr_atoms_in_systems, nr_heads * nr_edge_basis].
        h_i_spatial : torch.Tensor
            Node spatial attention. Shape [nr_atoms_in_systems, nr_atom_basis_spatial].

        Returns
        -------
        torch.Tensor
            Updated node features. Shape [nr_of_atoms_in_systems, nr_atom_basis].
        """

        return h + self.node_mlp(torch.cat([h, h_i_semantic, h_i_spatial], dim=-1))

    def update_velocity(self, v, h, combinations, idx_i):
        """Update node velocity features for the next layer.
        
        Wang and Chodera (2023) Sec. 5 Eq. 12.

        Parameters
        ----------
        v : torch.Tensor
            Input node velocity features. Shape [nr_of_atoms_in_systems, geometry_basis].
        h : torch.Tensor
            Input node semantic features. Shape [nr_of_atoms_in_systems, nr_atom_basis].
        combinations : torch.Tensor
            Linear combinations of mixed edge features. Shape [nr_pairs, nr_heads * nr_edge_basis].
        idx_i : torch.Tensor
            Indices of the receiver nodes. Shape [nr_pairs, ].

        Returns
        -------
        torch.Tensor
            Updated velocity features. Shape [nr_of_atoms_in_systems, geometry_basis].
        """
        v_ij = self.v_mixing_mlp(combinations.transpose(-1, -2)).squeeze(-1)
        expanded_idx_i = idx_i.view(-1, 1).expand_as(v_ij)
        dv = torch.zeros_like(v).scatter_reduce(0, expanded_idx_i, v_ij, "mean", include_self=False)
        return self.velocity_mlp(h) * v + dv

    def get_combinations(self, h_ij_semantic, dir_ij):
        """Compute linear combinations of mixed edge features.
        
        Summation term in Wang and Chodera (2023) Sec. 4 Eq. 6.

        Parameters
        ----------
        h_ij_semantic : torch.Tensor
            Edge semantic attention. Shape [nr_pairs, nr_heads * nr_edge_basis].
        dir_ij : torch.Tensor
            Normalized direction from receivers to senders. Shape [nr_pairs, geometry_basis].

        Returns
        -------
        torch.Tensor
            Linear combinations of mixed edge features. Shape [nr_pairs, nr_coefficients, geometry_basis].
        """
        # p: nr_pairs, x: geometry_basis, c: nr_coefficients
        return torch.einsum("px,pc->pcx", dir_ij, self.x_mixing_mlp(h_ij_semantic))

    def get_spatial_attention(self, combinations, idx_i, nr_atoms):
        """Compute spatial attention.

        Wang and Chodera (2023) Sec. 4 Eq. 6.

        Parameters
        ----------
        combinations : torch.Tensor
            Linear combinations of mixed edge features. Shape [nr_pairs, nr_coefficients, geometry_basis].
        idx_i : torch.Tensor
            Indices of the receiver nodes. Shape [nr_pairs, ].
        nr_atoms : in
            Number of atoms in all systems.

        Returns
        -------
        torch.Tensor
            Spatial attention. Shape [nr_atoms, nr_atom_basis_spatial].
        """
        expanded_idx_i = idx_i.view(-1, 1, 1).expand_as(combinations)
        out_shape = (nr_atoms, self.nr_coefficients, combinations.shape[-1])
        zeros = torch.zeros(out_shape, dtype=combinations.dtype, device=combinations.device)
        combinations_mean = zeros.scatter_reduce(0, expanded_idx_i, combinations, "mean", include_self=False)
        combinations_norm_square = (combinations_mean ** 2).sum(dim=-1)
        return self.post_norm_mlp(combinations_norm_square)

    def aggregate(self, h_ij_semantic, idx_i, nr_atoms):
        """Aggregate edge semantic attention over all senders connected to a receiver.

        Wang and Chodera (2023) Sec. 5 Algorithm 1,  step labelled "Neighborhood aggregation".

        Parameters
        ----------
        h_ij_semantic : torch.Tensor
            Edge semantic attention. Shape [nr_pairs, nr_heads * nr_edge_basis].
        idx_i : torch.Tensor
            Indices of the receiver nodes. Shape [nr_pairs, ].
        nr_atoms : int
            Number of atoms in all systems.

        Returns
        -------
        torch.Tensor
            Aggregated edge semantic attention. Shape [nr_atoms, nr_heads * nr_edge_basis].
        """
        expanded_idx_i = idx_i.view(-1, 1).expand_as(h_ij_semantic)
        out_shape = (nr_atoms, self.nr_heads * self.nr_edge_basis)
        zeros = torch.zeros(out_shape, dtype=h_ij_semantic.dtype, device=h_ij_semantic.device)
        return zeros.scatter_add(0, expanded_idx_i, h_ij_semantic)

    def get_semantic_attention(self, h_ij_edge, idx_i, d_ij, nr_atoms):
        """Compute semantic attention. Softmax is over all senders connected to a receiver.

        Wang and Chodera (2023) Sec. 5 Eq. 9-10.

        Parameters
        ----------
        h_ij_edge : torch.Tensor
            Edge features. Shape [nr_pairs, nr_edge_basis].
        idx_i : torch.Tensor
            Indices of the receiver nodes. Shape [nr_pairs, ].
        d_ij : torch.Tensor
            Distance between senders and receivers. Shape [nr_pairs, ].
        nr_atoms : int
            Number of atoms in all systems.

        Returns
        -------
        torch.Tensor
            Semantic attention. Shape [nr_pairs, nr_heads * nr_edge_basis].
        """
        h_ij_att_weights = self.semantic_attention_mlp(h_ij_edge)
        expanded_idx_i = idx_i.view(-1, 1).expand_as(h_ij_att_weights)
        h_ij_att_before_cutoff = scatter_softmax(h_ij_att_weights, expanded_idx_i, dim=0, dim_size=nr_atoms,
                                                 device=h_ij_edge.device)
        d_ij_att_weights = self.cutoff_module(d_ij)
        # p: nr_pairs, h: nr_heads
        combined_ij_att_prenorm = torch.einsum("ph,p->ph", h_ij_att_before_cutoff, d_ij_att_weights)
        zeros = torch.zeros_like(combined_ij_att_prenorm)
        combined_ij_att = combined_ij_att_prenorm / (
                zeros.scatter_add(0, expanded_idx_i, combined_ij_att_prenorm) + self.epsilon)
        # p: nr_pairs, f: nr_edge_basis, h: nr_heads
        return torch.reshape(torch.einsum("pf,ph->pfh", h_ij_edge, combined_ij_att),
                             (len(idx_i), self.nr_edge_basis * self.nr_heads))

    def forward(
            self,
            h: torch.Tensor,
            x: torch.Tensor,
            v: torch.Tensor,
            pairlist: torch.Tensor
    ) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
        """Compute interaction layer output.

        Parameters
        ----------
        h : torch.Tensor
            Input semantic (invariant) atomic embeddings. Shape [nr_of_atoms_in_systems, nr_atom_basis].
        x : torch.Tensor
            Input position (equivariant) atomic embeddings. Shape [nr_of_atoms_in_systems, geometry_basis].
        v : torch.Tensor
            Input velocity (equivariant) atomic embeddings. Shape [nr_of_atoms_in_systems, geometry_basis].
        pairlist : torch.Tensor, shape (2, nr_pairs)

        Returns
        -------
        Tuple[torch.Tensor, torch.Tensor, torch.Tensor]
            Updated scalar and vector representations (h, x, v) with same shapes as input.
        """
        idx_i, idx_j = pairlist
        nr_of_atoms_in_all_systems, _ = x.shape
        r_ij = x[idx_j] - x[idx_i]
        d_ij = torch.sqrt((r_ij ** 2).sum(dim=1) + self.epsilon)
        dir_ij = r_ij / (d_ij.unsqueeze(-1) + self.epsilon)

        h_ij_edge = self.update_edge(h[idx_j], h[idx_i], d_ij)
        h_ij_semantic = self.get_semantic_attention(h_ij_edge, idx_i, d_ij, nr_of_atoms_in_all_systems)
        del h_ij_edge
        h_i_semantic = self.aggregate(h_ij_semantic, idx_i, nr_of_atoms_in_all_systems)
        combinations = self.get_combinations(h_ij_semantic, dir_ij)
        del h_ij_semantic
        h_i_spatial = self.get_spatial_attention(combinations, idx_i, nr_of_atoms_in_all_systems)
        h_updated = self.update_node(h, h_i_semantic, h_i_spatial)
        del h_i_semantic, h_i_spatial
        v_updated = self.update_velocity(v, h, combinations, idx_i)
        del h, v
        x_updated = x + v_updated

        return h_updated, x_updated, v_updated
