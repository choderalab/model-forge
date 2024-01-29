from abc import ABC, abstractmethod
from typing import Dict

import lightning as pl
import torch
import torch.nn as nn
from torch.optim import AdamW

from loguru import logger as log


class PairList(nn.Module):
    """
    A module to handle pair list calculations for neighbor atoms.

    Attributes
    ----------
    cutoff : float
        The cutoff distance for neighbor calculations.

    Methods
    -------
    compute_r_ij(atom_pairs: torch.Tensor, positions: torch.Tensor) -> torch.Tensor
        Compute the displacement vector between atom pairs.
    forward(mask: torch.Tensor, positions: torch.Tensor) -> Dict[str, torch.Tensor]
        Forward pass for PairList.
    """

    def __init__(self, cutoff: float = 5.0):
        """
        Initialize PairList.

        Parameters
        ----------
        cutoff : float, optional
            Cutoff distance for neighbor calculations, default is 5.0.
        """
        super().__init__()
        from .utils import neighbor_pairs_nopbc

        self.calculate_neighbors = neighbor_pairs_nopbc
        self.cutoff = cutoff

    def calculate_r_ij(
        self, pair_indices: torch.Tensor, positions: torch.Tensor
    ) -> torch.Tensor:
        """Compute displacement vector between atom pairs.

        Parameters
        ----------
        pair_indices : torch.Tensor, shape [2, n_pairs]
            Atom indices for pairs of atoms
        positions : torch.Tensor, shape [nr_systems, nr_atoms, 3]
            Atom positions.

        Returns
        -------
        torch.Tensor, shape [n_pairs, 3]
            Displacement vector between atom pairs.
        """
        # Select the pairs of atom coordinates from the positions
        selected_positions = positions.index_select(0, pair_indices.view(-1)).view(
            2, -1, 3
        )
        return selected_positions[1] - selected_positions[0]

    def calculate_d_ij(self, r_ij):
        # Calculate the euclidian distance between the atoms in the pair
        return r_ij.norm(2, -1).unsqueeze(-1)

    def forward(
        self, positions: torch.Tensor, atomic_subsystem_indices: torch.Tensor
    ) -> Dict[str, torch.Tensor]:
        """
        Forward pass for PairList.

        Parameters
        ----------
        positions : torch.Tensor, shape [nr_systems, nr_atoms, 3]
            Position tensor.
        atomic_subsystem_indices : torch.Tensor, shape [nr_atoms]
        Returns
        -------
        dict : Dict[str, torch.Tensor], containing atom index pairs, distances, and displacement vectors.
            - 'pair_indices': torch.Tensor, shape (2, n_pairs)
            - 'r_ij' : torch.Tensor, shape (1, n_pairs)
            - 'd_ij' : torch.Tenso, shape (3, n_pairs)

        """
        pair_indices = self.calculate_neighbors(
            positions, atomic_subsystem_indices, self.cutoff
        )
        r_ij = self.calculate_r_ij(pair_indices, positions)

        return {
            "pair_indices": pair_indices,
            "d_ij": self.calculate_d_ij(r_ij),
            "r_ij": r_ij,
        }


class LightningModuleMixin(pl.LightningModule):
    """
    A mixin for PyTorch Lightning training.

    Methods
    -------
    training_step(batch: Dict[str, torch.Tensor], batch_idx: int) -> torch.Tensor
        Perform a single training step.
    configure_optimizers() -> AdamW
        Configures the optimizer for training.
    """

    def training_step(
        self, batch: Dict[str, torch.Tensor], batch_idx: int
    ) -> torch.Tensor:
        """
        Perform a single training step.

        Parameters
        ----------
        batch : Dict[str, torch.Tensor]
            Batch data. Expected to include 'E', a tensor with shape [batch_size, 1].
        batch_idx : int
            Batch index.

        Returns
        -------
        torch.Tensor, shape [batch_size, 1]
            Loss tensor.
        """

        E_hat = self.forward(batch).flatten()
        loss = self.loss_function(E_hat, batch["E_label"])
        self.log("train_loss", loss)
        return loss

    def configure_optimizers(self) -> AdamW:
        """
        Configures the optimizer for training.

        Returns
        -------
        AdamW
            The AdamW optimizer.
        """

        return self.optimizer(self.parameters(), lr=self.learning_rate)


# Abstract Base Class
class AbstractBaseNNP(nn.Module, ABC):
    """
    Abstract base class for neural network potentials.

    Methods
    -------
    forward(inputs: Dict[str, torch.Tensor]) -> torch.Tensor
        Abstract method for forward pass in neural network potentials.
    input_checks(inputs: Dict[str, torch.Tensor])
        Perform input checks to validate the input dictionary.
    """

    def __init__(self):
        """
        Initialize the AbstractBaseNNP class.
        """
        super().__init__()

    @abstractmethod
    def forward(self, inputs: Dict[str, torch.Tensor]) -> torch.Tensor:
        """
        Abstract method for neighborlist calculation and forward pass in neural network potentials.

        Parameters
        ----------
        inputs : Dict[str, torch.Tensor]
            - 'atomic_numbers', shape (nr_of_atoms_in_batch, *, *), 0 indicates non-interacting atoms that will be masked
            - 'total_charge', shape (nr_of_atoms_in_batch, 1)
            - 'positions', shape (nr_of_atoms_in_batch, 3)
            - 'boxvectors', shape (3, 3)

        Returns
        -------
        torch.Tensor
            Calculated output; shape is implementation-dependent.

        """
        pass

    @abstractmethod
    def _forward(self, inputs: Dict[str, torch.Tensor]):
        """
        Abstract method for forward pass in neural network potentials.
        This method is called by `forward`.

        Parameters
        ----------
        inputs: Dict[str, torch.Tensor]
            - pairlist, shape (n_paris,2)
            - r_ij, shape (n_pairs, 1)
            - d_ij, shape (n_pairs, 3)
            - 'atomic_subsystem_indices' (optional), shape nr_of_atoms_in_batch
            - positions, shape (nr_of_atoms_in_batch, 3)

        """
        pass

    def input_checks(self, inputs: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        Perform input checks to validate the input dictionary.

        Parameters
        ----------
        inputs : Dict[str, torch.Tensor]
            Inputs containing necessary data for the forward pass.
            The exact keys and shapes are implementation-dependent.

        Raises
        ------
        ValueError
            If the input dictionary is missing required keys or has invalid shapes.

        """
        required_keys = ["atomic_numbers", "positions", "atomic_subsystem_indices"]
        for key in required_keys:
            if key not in inputs:
                raise ValueError(f"Missing required key: {key}")

        if inputs["atomic_numbers"].dim() != 2:
            raise ValueError("Shape mismatch: 'atomic_numbers' should be a 2D tensor.")

        if inputs["positions"].dim() != 2:
            raise ValueError("Shape mismatch: 'positions' should be a 2D tensor.")

        if inputs["positions"].dtype != self._dtype:
            log.debug(
                f"Precision mismatch: dtype of positions tensor is {inputs['positions'].dtype}, "
                f"but dtype of model parameters is {self._dtype}. "
                f"Setting dtype of positions tensor to {self._dtype}."
            )
            inputs["positions"] = inputs["positions"].to(self._dtype)
        return inputs


class BaseNNP(AbstractBaseNNP):
    """
    Abstract base class for neural network potentials.

    Attributes
    ----------
    embedding : nn.Module
    cutoff : float
        Cutoff distance for neighbor calculations.

    Methods
    -------
    forward(inputs: Dict[str, torch.Tensor]) -> torch.Tensor
        Abstract method for forward pass in neural network potentials.
    """

    def __init__(
        self,
        embedding: nn.Module,
        cutoff: float = 5.0,
    ):
        """
        Initialize the NNP class.

        Parameters
        ----------
        cutoff : float, optional
            Cutoff distance (in Angstrom) for neighbor calculations, default is 5.0.
        """
        from .models import PairList
        from .utils import SlicedEmbedding

        super().__init__()
        self.calculate_distances_and_pairlist = PairList(cutoff)
        self.embedding = embedding
        assert isinstance(
            self.embedding, SlicedEmbedding
        ), "embedding must be SlicedEmbedding"
        assert embedding.embedding_dim > 0, "embedding_dim must be > 0"
        self.nr_atom_basis = embedding.embedding_dim
        self._dtype = None  # set at runtime

    def _prepare_input(self, inputs: Dict[str, torch.Tensor]):
        atomic_numbers = inputs["atomic_numbers"]
        # .squeeze(
        #    dim=1
        # )  # shape (nr_of_atoms_in_batch, 3)
        positions = inputs["positions"]  # shape (nr_of_atoms_in_batch, 3)
        atomic_subsystem_indices = inputs["atomic_subsystem_indices"]
        nr_of_atoms_in_batch = inputs["atomic_numbers"].shape[0]

        r = self.calculate_distances_and_pairlist(positions, atomic_subsystem_indices)
        atomic_numbers_embedding = self.embedding(atomic_numbers)
        assert atomic_numbers_embedding.shape == (
            nr_of_atoms_in_batch,
            self.nr_atom_basis,
        )

        inputs = {
            "pair_indices": r["pair_indices"],
            "d_ij": r["d_ij"],
            "r_ij": r["r_ij"],
            "atomic_numbers_embedding": atomic_numbers_embedding,
            "positions": positions,
            "atomic_numbers": atomic_numbers,
            "atomic_subsystem_indices": atomic_subsystem_indices,
        }
        return inputs

    def _set_dtype(self):
        dtypes = list({p.dtype for p in self.parameters()})
        assert len(dtypes) == 1
        self._dtype = dtypes[0]
        log.debug(f"Setting dtype to {self._dtype}.")

    def forward(self, inputs: Dict[str, torch.Tensor]) -> torch.Tensor:
        """
        Abstract method for forward pass in neural network potentials.

        Parameters
        ----------
        inputs : Dict[str, torch.Tensor]
            Inputs containing atomic numbers ('atomic_numbers'), coordinates ('positions') and pairlist ('pairlist').
            - 'atomic_numbers': int; shape (nr_of_atoms_in_batch, 1), 0 indicates non-interacting atoms that will be masked
            - 'total_charge' : int; shape (n_system)
            - 'positions': float; shape (nr_of_atoms_in_batch, 3)

        Returns
        -------
        torch.Tensor
            Calculated energies; float; shape (n_systems).

        """
        self._set_dtype()
        inputs = self.input_checks(inputs)
        inputs = self._prepare_input(inputs)
        return self._forward(inputs)


class SingleTopologyAlchemicalBaseNNPModel(AbstractBaseNNP):
    """
    Subclass for handling alchemical energy calculations.

    Methods
    -------
    forward(inputs: Dict[str, torch.Tensor]) -> torch.Tensor
        Calculate the alchemical energy for a given input batch.
    """

    def forward(self, inputs: Dict[str, torch.Tensor]):
        """
        Calculate the alchemical energy for a given input batch.

        Parameters
        ----------
        inputs : Dict[str, torch.Tensor]
            Inputs containing atomic numbers ('atomic_numbers'), coordinates ('positions') and pairlist ('pairlist').
            - 'atomic_numbers': shape (nr_of_atoms_in_batch, *, *), 0 indicates non-interacting atoms that will be masked
            - 'total_charge' : shape (nr_of_atoms_in_batch)
            - (only for alchemical transformation) 'alchemical_atomic_number': shape (nr_of_atoms_in_batch)
            - (only for alchemical transformation) 'lamb': float
            - 'positions': shape (nr_of_atoms_in_batch, 3)

        Returns
        -------
        torch.Tensor
            Calculated energies; shape (n_systems,).
        """

        # emb = nn.Embedding(1,200)
        # lamb_emb = (1 - lamb) * emb(input['Z1']) + lamb * emb(input['Z2'])	def __init__():
        self.input_checks(inputs)
        raise NotImplementedError
