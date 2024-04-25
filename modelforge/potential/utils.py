from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Tuple

import numpy as np
import torch
import torch.nn as nn
from loguru import logger as log
from pint import Quantity
from typing import Union


@dataclass
class NeuralNetworkData:
    pair_indices: torch.Tensor
    d_ij: torch.Tensor
    r_ij: torch.Tensor
    atomic_numbers: torch.Tensor
    number_of_atoms: int
    positions: torch.Tensor
    atomic_subsystem_indices: torch.Tensor
    total_charge: torch.Tensor


@dataclass
class NNPInput:
    """
    A dataclass to structure the inputs for neural network potentials.

    Attributes
    ----------
    atomic_numbers : torch.Tensor
        A 1D tensor containing atomic numbers for each atom in the system(s).
        Shape: [num_atoms], where `num_atoms` is the total number of atoms across all systems.
    positions : torch.Tensor
        A 2D tensor of shape [num_atoms, 3], representing the XYZ coordinates of each atom.
    atomic_subsystem_indices : torch.Tensor
        A 1D tensor mapping each atom to its respective subsystem or molecule.
        This allows for calculations involving multiple molecules or subsystems within the same batch.
        Shape: [num_atoms].
    total_charge : torch.Tensor
        A tensor with the total charge of molecule.
        Shape: [num_systems], where `num_systems` is the number of molecules.
    """

    atomic_numbers: torch.Tensor
    positions: Union[torch.Tensor, Quantity]
    atomic_subsystem_indices: torch.Tensor
    total_charge: torch.Tensor

    def to(
        self,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ):
        """Move all tensors in this instance to the specified device/dtype."""

        if device:
            self.atomic_numbers = self.atomic_numbers.to(device)
            self.positions = self.positions.to(device)
            self.atomic_subsystem_indices = self.atomic_subsystem_indices.to(device)
            self.total_charge = self.total_charge.to(device)
        if dtype:
            self.positions = self.positions.to(dtype)
        return self

    def __post_init__(self):
        # Set dtype and convert units if necessary
        self.atomic_numbers = self.atomic_numbers.to(torch.int32)
        self.atomic_subsystem_indices = self.atomic_subsystem_indices.to(torch.int32)
        self.total_charge = self.total_charge.to(torch.int32)

        # Unit conversion for positions
        if isinstance(self.positions, Quantity):
            positions = self.positions.to(unit.nanometer).m
            self.positions = torch.tensor(
                positions, dtype=torch.float32, requires_grad=True
            )

        # Validate inputs
        self._validate_inputs()

    def _validate_inputs(self):
        if self.atomic_numbers.dim() != 1:
            raise ValueError("atomic_numbers must be a 1D tensor")
        if self.positions.dim() != 2 or self.positions.size(1) != 3:
            raise ValueError("positions must be a 2D tensor with shape [num_atoms, 3]")
        if self.atomic_subsystem_indices.dim() != 1:
            raise ValueError("atomic_subsystem_indices must be a 1D tensor")
        if self.total_charge.dim() != 1:
            raise ValueError("total_charge must be a 1D tensor")

        # Optionally, check that the lengths match if required
        if len(self.positions) != len(self.atomic_numbers):
            raise ValueError(
                "The size of atomic_numbers and the first dimension of positions must match"
            )
        if len(self.positions) != len(self.atomic_subsystem_indices):
            raise ValueError(
                "The size of atomic_subsystem_indices and the first dimension of positions must match"
            )

    def as_namedtuple(self):
        """Export the dataclass fields and values as a named tuple."""

        from dataclasses import dataclass, fields
        import collections

        NNPInputTuple = collections.namedtuple(
            "NNPInputTuple", [field.name for field in fields(self)]
        )
        return NNPInputTuple(*[getattr(self, field.name) for field in fields(self)])


import torch


@dataclass(frozen=False)
class Metadata:
    """
    A NamedTuple to structure the inputs for neural network potentials.

    Parameters
    ----------
    """

    E: torch.Tensor
    atomic_subsystem_counts: torch.Tensor
    atomic_subsystem_indices_referencing_dataset: torch.Tensor
    number_of_atoms: int
    F: torch.Tensor = torch.tensor([], dtype=torch.float32)

    def to(
        self, device: Optional[torch.device] = None, dtype: Optional[torch.dtype] = None
    ):
        """Move all tensors in this instance to the specified device."""
        if device:
            self.E = self.E.to(device)
            self.F = self.F.to(device)
            self.atomic_subsystem_counts = self.atomic_subsystem_counts.to(device)
            self.atomic_subsystem_indices_referencing_dataset = (
                self.atomic_subsystem_indices_referencing_dataset.to(device)
            )
        if dtype:
            self.E = self.E.to(dtype)
            self.F = self.F.to(dtype)
        return self


@dataclass
class BatchData:
    nnp_input: NNPInput
    metadata: Metadata

    def to(
        self,
        device: Optional[torch.device] = None,
        dtype: Optional[torch.dtype] = None,
    ):
        self.nnp_input = self.nnp_input.to(device=device, dtype=dtype)
        self.metadata = self.metadata.to(device=device, dtype=dtype)
        return self


def shared_config_prior():
    import ray
    from ray import tune

    return {
        "lr": tune.loguniform(1e-5, 1e-1),
        "weight_decay": tune.loguniform(1e-5, 1e-1),
        "batch_size": tune.choice([32, 64, 128, 256, 512]),
    }


def triple_by_molecule(
    atom_pairs: torch.Tensor,
) -> Tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Input: indices for pairs of atoms that are close to each other.
    each pair only appear once, i.e. only one of the pairs (1, 2) and
    (2, 1) exists.

    NOTE: this function is taken from https://github.com/aiqm/torchani/blob/17204c6dccf6210753bc8c0ca4c92278b60719c9/torchani/aev.py
            with little modifications.
    """

    def cumsum_from_zero(input_: torch.Tensor) -> torch.Tensor:
        cumsum = torch.zeros_like(input_)
        torch.cumsum(input_[:-1], dim=0, out=cumsum[1:])
        return cumsum

    # convert representation from pair to central-others
    ai1 = atom_pairs.view(-1)
    sorted_ai1, rev_indices = ai1.sort()

    # sort and compute unique key
    uniqued_central_atom_index, counts = torch.unique_consecutive(
        sorted_ai1, return_inverse=False, return_counts=True
    )

    # compute central_atom_index
    pair_sizes = torch.div(counts * (counts - 1), 2, rounding_mode="trunc")
    pair_indices = torch.repeat_interleave(pair_sizes)
    central_atom_index = uniqued_central_atom_index.index_select(0, pair_indices)

    # do local combinations within unique key, assuming sorted
    m = counts.max().item() if counts.numel() > 0 else 0
    n = pair_sizes.shape[0]
    intra_pair_indices = (
        torch.tril_indices(m, m, -1, device=ai1.device).unsqueeze(1).expand(-1, n, -1)
    )
    mask = (
        torch.arange(intra_pair_indices.shape[2], device=ai1.device)
        < pair_sizes.unsqueeze(1)
    ).flatten()
    sorted_local_index12 = intra_pair_indices.flatten(1, 2)[:, mask]
    sorted_local_index12 += cumsum_from_zero(counts).index_select(0, pair_indices)

    # unsort result from last part
    local_index12 = rev_indices[sorted_local_index12]

    # compute mapping between representation of central-other to pair
    n = atom_pairs.shape[1]
    sign12 = ((local_index12 < n).to(torch.int8) * 2) - 1
    return central_atom_index, local_index12 % n, sign12


class Embedding(nn.Module):
    def __init__(self, num_embeddings: int, embedding_dim: int):
        """
        Initialize the embedding module.

        Parameters
        ----------
        num_embeddings: int
        embedding_dim : int
            Dimensionality of the embedding.
        """
        super().__init__()
        self.embedding = nn.Embedding(num_embeddings, embedding_dim)

    @property
    def data(self):
        return self.embedding.weight.data

    @data.setter
    def data(self, data):
        self.embedding.weight.data = data

    @property
    def embedding_dim(self):
        """
        Get the dimensionality of the embedding.

        Returns
        -------
        int
            The dimensionality of the embedding.
        """
        return self.embedding.embedding_dim

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        """
        Embeddes the pr3ovided 1D tensor using the embedding layer.

        Parameters
        ----------
        x : torch.Tensor
            1D tensor to be embedded.

        Returns
        -------
        torch.Tensor
            with shape (num_embeddings, embedding_dim)
        """

        return self.embedding(x)


import torch.nn.functional as F
from torch.nn.init import xavier_uniform_, zeros_


class Dense(nn.Linear):
    """
    Fully connected linear layer with activation function.

    """

    def __init__(
        self,
        in_features: int,
        out_features: int,
        bias: bool = True,
        activation: Optional[nn.Module] = None,
        weight_init: Callable = xavier_uniform_,
        bias_init: Callable = zeros_,
    ):
        """
        Args:
            in_features: number of input feature :math:`x`.
            out_features: umber of output features :math:`y`.
            bias: If False, the layer will not adapt bias :math:`b`.
            activation: if None, no activation function is used.
            weight_init: weight initializer from current weight.
            bias_init: bias initializer from current bias.
        """
        self.weight_init = weight_init
        self.bias_init = bias_init
        super().__init__(in_features, out_features, bias)

        self.activation = activation
        if self.activation is None:
            self.activation = nn.Identity()

    def reset_parameters(self):
        self.weight_init(self.weight)
        if self.bias is not None:
            self.bias_init(self.bias)

    def forward(self, input: torch.Tensor):
        y = F.linear(input, self.weight, self.bias)
        y = self.activation(y)
        return y


from openff.units import unit


class CosineCutoff(nn.Module):
    def __init__(self, cutoff: unit.Quantity):
        """
        Behler-style cosine cutoff module.

        Parameters:
        ----------
        cutoff: unit.Quantity
            The cutoff distance.

        """
        super().__init__()
        cutoff = cutoff.to(unit.nanometer).m
        self.register_buffer("cutoff", torch.tensor([cutoff]))

    def forward(self, d_ij: torch.Tensor):
        """
        Compute the cosine cutoff for a distance tensor.
        NOTE: the cutoff function doesn't care about units as long as they are consisten,

        Parameters
        ----------
        d_ij : Tensor
            Pairwise distance tensor. Shape: [n_pairs, distance]

        Returns
        -------
        Tensor
            The cosine cutoff tensor. Shape: [..., N]
        """
        # Compute values of cutoff function
        input_cut = 0.5 * (
            torch.cos(d_ij * np.pi / self.cutoff) + 1.0
        )  # NOTE: ANI adds 0.5 instead of 1.
        # Remove contributions beyond the cutoff radius
        input_cut *= (d_ij < self.cutoff).float()
        return input_cut


from typing import Dict



class ShiftedSoftplus(nn.Module):
    def __init__(self):
        super().__init__()
        import math

        self.log_2 = math.log(2.0)

    def forward(self, x: torch.Tensor):
        """Compute shifted soft-plus activation function.

        y = \ln\left(1 + e^{-x}\right) - \ln(2)

        Parameters:
        -----------
        x:torch.Tensor
            input tensor

        Returns:
        -----------
        torch.Tensor: shifted soft-plus of input.

        """
        from torch.nn import functional

        return functional.softplus(x) - self.log_2


class AngularSymmetryFunction(nn.Module):
    """
    Initialize AngularSymmetryFunction module.

    """

    def __init__(
        self,
        max_distance: unit.Quantity,
        min_distance: unit.Quantity,
        number_of_gaussians_for_asf: int = 8,
        angle_sections: int = 4,
        trainable: bool = False,
        dtype: Optional[torch.dtype] = None,
    ) -> None:
        """
        Parameters
        ----
        number_of_gaussian: Number of gaussian functions to use for angular symmetry function.
        angular_cutoff: Cutoff distance for angular symmetry function.
        angular_start: Starting distance for angular symmetry function.
        ani_style: Whether to use ANI symmetry function style.
        """

        super().__init__()
        from loguru import logger as log

        self.number_of_gaussians_asf = number_of_gaussians_for_asf
        self.angular_cutoff = max_distance
        self.cosine_cutoff = CosineCutoff(self.angular_cutoff)
        _unitless_angular_cutoff = max_distance.to(unit.nanometer).m
        self.angular_start = min_distance
        _unitless_angular_start = min_distance.to(unit.nanometer).m

        # save constants
        EtaA = angular_eta = 12.5 * 100  # FIXME hardcoded eta
        Zeta = 14.1000  # FIXME hardcoded zeta

        if trainable:
            self.EtaA = torch.tensor([EtaA], dtype=dtype)
            self.Zeta = torch.tensor([Zeta], dtype=dtype)
            self.Rca = torch.tensor([_unitless_angular_cutoff], dtype=dtype)

        else:
            self.register_buffer("EtaA", torch.tensor([EtaA], dtype=dtype))
            self.register_buffer("Zeta", torch.tensor([Zeta], dtype=dtype))
            self.register_buffer(
                "Rca", torch.tensor([_unitless_angular_cutoff], dtype=dtype)
            )

        # ===============
        # # calculate shifts
        # ===============
        import math

        # ShfZ
        angle_start = math.pi / (2 * angle_sections)
        ShfZ = (torch.linspace(0, math.pi, angle_sections + 1) + angle_start)[:-1]

        # ShfA
        ShfA = torch.linspace(
            _unitless_angular_start,
            _unitless_angular_cutoff,
            number_of_gaussians_for_asf + 1,
        )[:-1]

        # register shifts
        if trainable:
            self.ShfZ = ShfZ
            self.ShfA = ShfA
        else:
            self.register_buffer("ShfZ", ShfZ)
            self.register_buffer("ShfA", ShfA)

        log.debug(
            f"""
RadialSymmetryFunction: 
Rca={_unitless_angular_cutoff} 
ShfZ={ShfZ}, 
ShFa={ShfA}, 
eta={EtaA}"""
        )

        # The length of angular subaev of a single species
        self.angular_sublength = self.ShfA.numel() * self.ShfZ.numel()

    def forward(self, r_ij: torch.Tensor) -> torch.Tensor:
        # calculate the angular sub aev
        sub_aev = self.compute_angular_sub_aev(r_ij)
        return sub_aev

    def compute_angular_sub_aev(self, vectors12: torch.Tensor) -> torch.Tensor:
        """Compute the angular subAEV terms of the center atom given neighbor pairs.

        This correspond to equation (4) in the ANI paper. This function just
        compute the terms. The sum in the equation is not computed.
        The input tensor have shape (conformations, atoms, N), where N
        is the number of neighbor atom pairs within the cutoff radius and
        output tensor should have shape
        (conformations, atoms, ``self.angular_sublength()``)

        """
        vectors12 = vectors12.unsqueeze(-1).unsqueeze(-1).unsqueeze(-1).unsqueeze(-1)
        distances12 = vectors12.norm(2, dim=-5)

        # 0.95 is multiplied to the cos values to prevent acos from
        # returning NaN.
        cos_angles = 0.95 * torch.nn.functional.cosine_similarity(
            vectors12[0], vectors12[1], dim=-5
        )
        angles = torch.acos(cos_angles)

        fcj12 = self.cosine_cutoff(distances12)
        factor1 = ((1 + torch.cos(angles - self.ShfZ)) / 2) ** self.Zeta
        factor2 = torch.exp(
            -self.EtaA * (distances12.sum(0) / 2 - self.ShfA) ** 2
        ).unsqueeze(-1)
        factor2 = factor2.squeeze(4).squeeze(3)
        ret = 2 * factor1 * factor2 * fcj12.prod(0)
        # At this point, ret now have shape
        # (conformations, atoms, N, ?, ?, ?, ?) where ? depend on constants.
        # We then should flat the last 4 dimensions to view the subAEV as one
        # dimension vector
        return ret.flatten(start_dim=-4)


from abc import ABC, abstractmethod


class RadialBasisFunction(ABC):
    @abstractmethod
    def compute(self, distances, centers, scale_factors):
        pass


class GaussianRadialBasisFunction(RadialBasisFunction):
    def compute(
        self,
        distances: torch.Tensor,
        centers: torch.Tensor,
        scale_factors: torch.Tensor,
    ) -> torch.Tensor:
        diff = distances - centers
        return torch.exp((-1 * scale_factors) * diff**2)


class DoubleExponentialRadialBasisFunction(RadialBasisFunction):
    def compute(
        self,
        distances: torch.Tensor,
        centers: torch.Tensor,
        scale_factors: torch.Tensor,
    ) -> torch.Tensor:
        diff = distances - centers
        return torch.exp(-torch.abs(diff / scale_factors))


class RadialSymmetryFunction(nn.Module):
    def __init__(
        self,
        number_of_radial_basis_functions: int,
        max_distance: unit.Quantity,
        min_distance: unit.Quantity = 0.0 * unit.nanometer,
        dtype: Optional[torch.dtype] = None,
        trainable: bool = False,
        radial_basis_function: RadialBasisFunction = GaussianRadialBasisFunction(),
    ):
        """RadialSymmetryFunction class.

        Initializes and contains the logic for computing radial symmetry functions.

        Parameters
        ---------
        number_of_radial_basis_functions: int
            Number of radial basis functions to use.
        max_distance: unit.Quantity
            Maximum distance to consider for symmetry functions.
        min_distance: unit.Quantity
            Minimum distance to consider.
        dtype:
            Data type for computations.
        trainable: bool, default False
            Whether parameters are trainable.
        radial_basis_function: RadialBasisFunction, default GaussianRadialBasisFunction()

        Subclasses must implement the forward() method to compute the actual
        symmetry function output given an input distance matrix.
        """

        super().__init__()
        self.number_of_radial_basis_functions = number_of_radial_basis_functions
        self.max_distance = max_distance
        self.min_distance = min_distance
        self.dtype = dtype
        self.trainable = trainable
        self.radial_basis_function = radial_basis_function
        self.initialize_parameters()
        # The length of radial subaev of a single species
        self.radial_sublength = self.radial_basis_centers.numel()

    def initialize_parameters(self):
        # convert to nanometer
        _unitless_max_distance = self.max_distance.to(unit.nanometer).m
        _unitless_min_distance = self.min_distance.to(unit.nanometer).m

        # calculate radial basis centers
        radial_basis_centers = self.calculate_radial_basis_centers(
            _unitless_min_distance,
            _unitless_max_distance,
            self.number_of_radial_basis_functions,
            self.dtype,
        )
        # calculate scale factors
        radial_scale_factor = self.calculate_radial_scale_factor(
            _unitless_min_distance,
            _unitless_max_distance,
            self.number_of_radial_basis_functions,
        )

        # either add as parameters or register buffers
        if self.trainable:
            self.radial_basis_centers = radial_basis_centers
            self.radial_scale_factor = radial_scale_factor
            self.prefactor = nn.Parameter(torch.tensor([1.0]))
        else:
            self.register_buffer("radial_basis_centers", radial_basis_centers)
            self.register_buffer("radial_scale_factor", radial_scale_factor)
            self.register_buffer("prefactor", torch.tensor([1.0]))

    def calculate_radial_basis_centers(
        self,
        _unitless_min_distance,
        _unitless_max_distance,
        number_of_radial_basis_functions,
        dtype,
    ):
        # the default approach to calculate radial basis centers
        # can be overwritten by subclasses
        centers = torch.linspace(
            _unitless_min_distance,
            _unitless_max_distance,
            number_of_radial_basis_functions,
            dtype=dtype,
        )
        return centers

    def calculate_radial_scale_factor(
        self,
        _unitless_min_distance,
        _unitless_max_distance,
        number_of_radial_basis_functions,
    ):
        # the default approach to calculate radial scale factors (each of them are scaled by the same value)
        # can be overwritten by subclasses
        scale_factors = torch.full(
            (number_of_radial_basis_functions,),
            (_unitless_min_distance - _unitless_max_distance)
            / number_of_radial_basis_functions,
        )
        scale_factors = scale_factors * -15_000
        return scale_factors

    def forward(self, d_ij: torch.Tensor) -> torch.Tensor:
        """
        Compute the radial symmetry function values for each distance in d_ij.

        Parameters
        ----------
        d_ij: torch.Tensor
            pairwise distances with shape [N, 1] where N is the number of pairs.

        Returns:
        torch.Tensor,
            tensor of radial symmetry function values with shape [N, num_basis_functions].
        """
        features = self.radial_basis_function.compute(
            d_ij, self.radial_basis_centers, self.radial_scale_factor
        )
        return self.prefactor * features


class SchnetRadialSymmetryFunction(RadialSymmetryFunction):
    def __init__(
        self,
        number_of_radial_basis_functions: int,
        max_distance: unit.Quantity,
        min_distance: unit.Quantity = 0.0 * unit.nanometer,
        dtype: Optional[torch.dtype] = None,
        trainable: bool = False,
        radial_basis_function: RadialBasisFunction = GaussianRadialBasisFunction(),
    ):
        """RadialSymmetryFunction class.

        Initializes and contains the logic for computing radial symmetry functions.

        Parameters
        ---------
        """

        super().__init__(
            number_of_radial_basis_functions,
            max_distance,
            min_distance,
            dtype,
            trainable,
            radial_basis_function,
        )
        self.prefactor = torch.tensor([1.0])

    def calculate_radial_scale_factor(
        self,
        _unitless_min_distance,
        _unitless_max_distance,
        number_of_radial_basis_functions,
    ):

        scale_factors = torch.linspace(
            _unitless_min_distance,
            _unitless_max_distance,
            number_of_radial_basis_functions,
        )

        widths = (
            torch.abs(scale_factors[1] - scale_factors[0])
            * torch.ones_like(scale_factors)
        ).to(self.dtype)

        scale_factors = 0.5 / torch.square_(widths)
        return scale_factors


class AniRadialSymmetryFunction(RadialSymmetryFunction):
    def __init__(
        self,
        number_of_radial_basis_functions: int,
        max_distance: unit.Quantity,
        min_distance: unit.Quantity = 0.0 * unit.nanometer,
        dtype: Optional[torch.dtype] = None,
        trainable: bool = False,
        radial_basis_function: RadialBasisFunction = GaussianRadialBasisFunction(),
    ):
        """RadialSymmetryFunction class.

        Initializes and contains the logic for computing radial symmetry functions.

        Parameters
        ---------
        """

        super().__init__(
            number_of_radial_basis_functions,
            max_distance,
            min_distance,
            dtype,
            trainable,
            radial_basis_function,
        )
        self.prefactor = torch.tensor([0.25])

    def calculate_radial_basis_centers(
        self,
        _unitless_min_distance,
        _unitless_max_distance,
        number_of_radial_basis_functions,
        dtype,
    ):
        centers = torch.linspace(
            _unitless_min_distance,
            _unitless_max_distance,
            number_of_radial_basis_functions + 1,
            dtype=dtype,
        )[:-1]
        log.debug(f"{centers=}")
        return centers

    def calculate_radial_scale_factor(
        self,
        _unitless_min_distance,
        _unitless_max_distance,
        number_of_radial_basis_functions,
    ):
        # ANI uses a predefined scaling factor
        scale_factors = torch.full((number_of_radial_basis_functions,), (19.7 * 100))
        return scale_factors


class SAKERadialSymmetryFunction(RadialSymmetryFunction):
    def calculate_radial_basis_centers(
        self,
        _unitless_min_distance,
        _unitless_max_distance,
        number_of_radial_basis_functions,
        dtype,
    ):
        # initialize means and betas according to the default values in PhysNet
        # https://pubs.acs.org/doi/10.1021/acs.jctc.9b00181

        start_value = torch.exp(
            torch.scalar_tensor(
                -_unitless_max_distance + _unitless_min_distance, dtype=dtype
            )
        )
        centers = torch.linspace(
            start_value, 1, number_of_radial_basis_functions, dtype=dtype
        )
        return centers

    def calculate_radial_scale_factor(
        self,
        _unitless_min_distance,
        _unitless_max_distance,
        number_of_radial_basis_functions,
    ):
        start_value = torch.exp(
            torch.scalar_tensor(-_unitless_max_distance + _unitless_min_distance)
        )
        radial_scale_factor = torch.tensor(
            [(2 / number_of_radial_basis_functions * (1 - start_value)) ** -2]
            * number_of_radial_basis_functions
        )
        return radial_scale_factor


class SAKERadialBasisFunction(RadialBasisFunction):

    def __init__(self, max_distance, min_distance):
        super().__init__()
        self._unitless_min_distance = min_distance.to(unit.nanometer).m
        self.alpha = (
            (5.0 * unit.nanometer / (max_distance - min_distance)).to_base_units().m
        )  # check units

    def compute(
        self,
        distances: torch.Tensor,
        centers: torch.Tensor,
        scale_factors: torch.Tensor,
    ) -> torch.Tensor:
        return torch.exp(
            -scale_factors
            * (
                torch.exp(
                    self.alpha
                    * (-distances.unsqueeze(-1) + self._unitless_min_distance)
                )
                - centers
            )
            ** 2
        )


def pair_list(
    atomic_subsystem_indices: torch.Tensor,
    only_unique_pairs: bool = False,
) -> torch.Tensor:
    """Compute all pairs of atoms and their distances.

    Parameters
    ----------
    atomic_subsystem_indices : torch.Tensor, shape (nr_atoms_per_systems)
        Atom indices to indicate which atoms belong to which molecule
    only_unique_pairs : bool, optional
        If True, only unique pairs are returned (default is False).
        Otherwise, all pairs are returned.
    """
    # generate index grid
    n = len(atomic_subsystem_indices)

    # get device that passed tensors lives on, initialize on the same device
    device = atomic_subsystem_indices.device

    if only_unique_pairs:
        i_indices, j_indices = torch.triu_indices(n, n, 1, device=device)
    else:
        # Repeat each number n-1 times for i_indices
        i_indices = torch.repeat_interleave(
            torch.arange(n, device=device), repeats=n - 1
        )

        # Correctly construct j_indices
        j_indices = torch.cat(
            [
                torch.cat(
                    (
                        torch.arange(i, device=device),
                        torch.arange(i + 1, n, device=device),
                    )
                )
                for i in range(n)
            ]
        )

    # filter pairs to only keep those belonging to the same molecule
    same_molecule_mask = (
        atomic_subsystem_indices[i_indices] == atomic_subsystem_indices[j_indices]
    )

    # Apply mask to get final pair indices
    i_final_pairs = i_indices[same_molecule_mask]
    j_final_pairs = j_indices[same_molecule_mask]

    # concatenate to form final (2, n_pairs) tensor
    pair_indices = torch.stack((i_final_pairs, j_final_pairs))

    return pair_indices.to(device)


from openff.units import unit


def neighbor_list_with_cutoff(
    coordinates: torch.Tensor,  # in nanometer
    atomic_subsystem_indices: torch.Tensor,
    cutoff: unit.Quantity,
    only_unique_pairs: bool = False,
) -> torch.Tensor:
    """Compute all pairs of atoms and their distances.

    Parameters
    ----------
    coordinates : torch.Tensor, shape (nr_atoms_per_systems, 3), in nanometer
    atomic_subsystem_indices : torch.Tensor, shape (nr_atoms_per_systems)
        Atom indices to indicate which atoms belong to which molecule
    cutoff : unit.Quantity
        The cutoff distance.
    """
    positions = coordinates.detach()
    pair_indices = pair_list(
        atomic_subsystem_indices, only_unique_pairs=only_unique_pairs
    )

    # create pair_coordinates tensor
    pair_coordinates = positions[pair_indices.T]
    pair_coordinates = pair_coordinates.view(-1, 2, 3)

    # Calculate distances
    distances = (pair_coordinates[:, 0, :] - pair_coordinates[:, 1, :]).norm(
        p=2, dim=-1
    )

    # Find pairs within the cutoff
    cutoff = cutoff.to(unit.nanometer).m
    in_cutoff = (distances <= cutoff).nonzero(as_tuple=False).squeeze()

    # Get the atom indices within the cutoff
    pair_indices_within_cutoff = pair_indices[:, in_cutoff]

    return pair_indices_within_cutoff


def scatter_softmax(
    src: torch.Tensor,
    index: torch.Tensor,
    dim: int,
    dim_size: Optional[int] = None,
    device: Optional[torch.device] = None,
) -> torch.Tensor:
    """
    Softmax operation over all values in :attr:`src` tensor that share indices
    specified in the :attr:`index` tensor along a given axis :attr:`dim`.

    For one-dimensional tensors, the operation computes

    .. math::
        \mathrm{out}_i = {\textrm{softmax}(\mathrm{src})}_i =
        \frac{\exp(\mathrm{src}_i)}{\sum_j \exp(\mathrm{src}_j)}

    where :math:`\sum_j` is over :math:`j` such that
    :math:`\mathrm{index}_j = i`.

    Args:
        src (Tensor): The source tensor.
        index (LongTensor): The indices of elements to scatter.
        dim (int, optional): The axis along which to index.
            (default: :obj:`-1`)
        dim_size: The number of classes, i.e. the number of unique indices in `index`.

    :rtype: :class:`Tensor`

    Adapted from: https://github.com/rusty1s/pytorch_scatter/blob/c31915e1c4ceb27b2e7248d21576f685dc45dd01/torch_scatter/composite/softmax.py
    """
    if not torch.is_floating_point(src):
        raise ValueError(
            "`scatter_softmax` can only be computed over tensors "
            "with floating point data types."
        )

    assert dim >= 0, f"dim must be non-negative, got {dim}"
    assert (
        dim < src.dim()
    ), f"dim must be less than the number of dimensions of src {src.dim()}, got {dim}"

    out_shape = [
        other_dim_size if (other_dim != dim) else dim_size
        for (other_dim, other_dim_size) in enumerate(src.shape)
    ]

    zeros = torch.zeros(out_shape, dtype=src.dtype, device=device)
    max_value_per_index = zeros.scatter_reduce(
        dim, index, src, "amax", include_self=False
    )
    max_per_src_element = max_value_per_index.gather(dim, index)

    recentered_scores = src - max_per_src_element
    recentered_scores_exp = recentered_scores.exp()

    sum_per_index = torch.zeros(out_shape, dtype=src.dtype, device=device).scatter_add(
        dim, index, recentered_scores_exp
    )
    normalizing_constants = sum_per_index.gather(dim, index)

    return recentered_scores_exp.div(normalizing_constants)
