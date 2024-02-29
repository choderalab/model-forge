from typing import Dict

import lightning as pl
import torch
import torch.nn as nn
from torch.optim import AdamW

from loguru import logger as log

from abc import ABC, abstractmethod
import torch


class Pairlist(nn.Module):
    """
    A module to handle pair list calculations for atoms.
    This returns a pair list of atom indices and the displacement vectors between them.

    Methods
    -------
    compute_r_ij(atom_pairs: torch.Tensor, positions: torch.Tensor) -> torch.Tensor
        Compute the displacement vector between atom pairs.
    forward(mask: torch.Tensor, positions: torch.Tensor) -> Dict[str, torch.Tensor]
        Forward pass for PairList.
    """

    def __init__(self, only_unique_pairs: bool = False):
        """
        Initialize PairList.

        Parameters
        ----------
        only_unique_pairs : bool, optional
            If set to True, only unique pairs of atoms are considered, default is False.
        """
        super().__init__()
        from .utils import pair_list

        self.calculate_pairs = pair_list
        self.only_unique_pairs = only_unique_pairs

    def calculate_r_ij(
        self, pair_indices: torch.Tensor, positions: torch.Tensor
    ) -> torch.Tensor:
        """Compute displacement vector between atom pairs.

        Parameters
        ----------
        pair_indices : torch.Tensor, shape [2, n_pairs]
            Atom indices for pairs of atoms
        positions : torch.Tensor, shape [atoms, 3]
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
        positions : torch.Tensor, shape [nr_atoms, 3]
            Position tensor.
        atomic_subsystem_indices : torch.Tensor, shape [nr_atoms]
        Returns
        -------
        dict : Dict[str, torch.Tensor], containing atom index pairs, distances, and displacement vectors.
            - 'pair_indices': torch.Tensor, shape (2, n_pairs)
            - 'r_ij' : torch.Tensor, shape (3, n_pairs)
            - 'd_ij' : torch.Tensor, shape (1, n_pairs)

        """
        assert positions.ndim == 2
        pair_indices = self.calculate_pairs(
            atomic_subsystem_indices,
            only_unique_pairs=self.only_unique_pairs,
        )
        r_ij = self.calculate_r_ij(pair_indices, positions)

        return {
            "pair_indices": pair_indices,
            "d_ij": self.calculate_d_ij(r_ij),
            "r_ij": r_ij,
        }


from openff.units import unit


class Neighborlist(Pairlist):
    def __init__(self, cutoff: unit.Quantity, only_unique_pairs: bool = False):
        """
        Initialize PairList.

        Parameters
        ----------
        cutoff : unit.Quantity
            Cutoff distance for neighbor calculations.
        only_unique_pairs : bool, optional
            If set to True, only unique pairs of atoms are considered, default is False.
        """
        super().__init__(only_unique_pairs=only_unique_pairs)
        from .utils import neighbor_list_with_cutoff

        self.calculate_pairs = neighbor_list_with_cutoff
        self.cutoff = cutoff

    def forward(
        self, positions: torch.Tensor, atomic_subsystem_indices: torch.Tensor
    ) -> Dict[str, torch.Tensor]:
        """
        Forward pass for Neighborlist.

        Parameters
        ----------
        positions : torch.Tensor, shape [nr_systems, nr_atoms, 3]
            Position tensor.
        atomic_subsystem_indices : torch.Tensor, shape [nr_atoms]
        Returns
        -------
        dict : Dict[str, torch.Tensor], containing atom index pairs, distances, and displacement vectors.
            - 'pair_indices': torch.Tensor, shape (2, n_pairs)
            - 'r_ij' : torch.Tensor, shape (3, n_pairs)
            - 'd_ij' : torch.Tensor, shape (1, n_pairs)

        """
        pair_indices = self.calculate_pairs(
            positions,
            atomic_subsystem_indices,
            cutoff=self.cutoff,
            only_unique_pairs=self.only_unique_pairs,
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

    def _log_batch_size(self, batch: Dict[str, torch.Tensor]):
        batch_size = int(len(batch["E_label"]))
        return batch_size

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
        batch_size = self._log_batch_size(batch)
        predictions = self.forward(batch)["energy_readout"].flatten()
        targets = batch["E_label"].flatten().to(torch.float32)
        loss = self.loss_function(predictions, targets)

        self.log(
            "train_loss",
            loss,
            on_epoch=True,
            on_step=True,
            prog_bar=True,
            batch_size=batch_size,
        )
        return loss

    def test_step(self, batch, batch_idx):
        from torch.nn import functional as F

        batch_size = self._log_batch_size(batch)

        predictions = self.forward(batch).flatten()
        targets = batch["E_label"].flatten()
        test_loss = F.mse_loss(predictions["energy_readout"], targets)
        self.log(
            "test_loss", test_loss, batch_size=batch_size, on_epoch=True, prog_bar=True
        )

    def validation_step(self, batch: Dict[str, torch.Tensor], batch_idx):
        from torch.nn import functional as F

        batch_size = self._log_batch_size(batch)
        predictions = self.forward(batch)["energy_readout"]
        targets = batch["E_label"]
        val_loss = F.mse_loss(predictions, targets)
        self.log(
            "val_loss", val_loss, batch_size=batch_size, on_epoch=True, prog_bar=True
        )

    def configure_optimizers(self) -> AdamW:
        """
        Configures the optimizer for training.

        Returns
        -------
        AdamW
            The AdamW optimizer.
        """

        return self.optimizer(self.parameters(), lr=self.learning_rate)


from modelforge.potential.postprocessing import PostprocessingPipeline, NoPostprocess


from abc import ABC, abstractmethod
from openff.units import unit


class BaseNNP(ABC, nn.Module):
    """
    Abstract Base class for neural network potentials.
    """

    def __init__(
        self,
        radial_cutoff: float,
        angular_cutoff: Optional[float] = None,
        postprocessing: PostprocessingPipeline = PostprocessingPipeline(
            [NoPostprocess()]
        ),
    ):
        """
        Initialize the NNP class.

        Parameters
        ----------
        radial_cutoff : float
            Cutoff distance for atom centered symmetry functions, in nanometer.
        angular_cutoff : float
            Cutoff distance for atom centered angular functions, in nanometer.
        """
        from .models import Pairlist

        super().__init__()
        self._radial_cutoff = radial_cutoff
        self._angular_cutoff = angular_cutoff
        self.calculate_distances_and_pairlist = _PairList()
        self._dtype = None  # set at runtime
        self._log_message_dtype = False
        self._log_message_units = False
        self._energy_postprocessing_pipeline = postprocessing
        self.dataset_stats = {}

    @abstractmethod
    def prepare_inputs(self, inputs: Dict[str, torch.Tensor]):
        # needs to be implemented by the subclass
        # if subclass needs any additional input preparation (e.g. embedding),
        # it should be done here
        pass

    @abstractmethod
    def _forward(self, inputs: Dict[str, torch.Tensor]):
        # needs to be implemented by the subclass
        # perform the forward pass implemented in the subclass
        pass

    @abstractmethod
    def _readout(self, input: Dict[str, torch.Tensor]):
        # needs to be implemented by the subclass
        # perform the readout operation implemented in the subclass
        pass

    def _energy_postprocessing(
        self, energy_readout: torch.Tensor, inputs: Dict[str, torch.Tensor]
    ):
        """
        Performs postprocessing of the energies.

        Parameters
        ----------
        energy_readout: torch.Tensor
        inputs: Dict[str, torch.Tensor]: with keys
                energy_readout: torch.Tensor, shape (nr_of_molecuels_in_batch)
                atomic_numbers: torch.Tensor, shape (nr_of_atoms_in_batch)
                atomic_subsystem_indices: torch.Tensor, shape (nr_of_atoms_in_batch)

        """
        pipeline = self._energy_postprocessing_pipeline

        postprocessing_data = {}
        postprocessing_data["energy_readout"] = energy_readout
        postprocessing_data["atomic_numbers"] = inputs["atomic_numbers"]
        postprocessing_data["atomic_subsystem_indices"] = inputs[
            "atomic_subsystem_indices"
        ]
        return pipeline(postprocessing_data)

    def _energy_postprocessing(
        self, energy_readout: torch.Tensor, inputs: Dict[str, torch.Tensor]
    ):
        """
        Performs postprocessing of the energies.

        Parameters
        ----------
        energy_readout: torch.Tensor
        inputs: Dict[str, torch.Tensor]: with keys
                energy_readout: torch.Tensor, shape (nr_of_molecuels_in_batch)
                atomic_numbers: torch.Tensor, shape (nr_of_atoms_in_batch)
                atomic_subsystem_indices: torch.Tensor, shape (nr_of_atoms_in_batch)

        """
        pipeline = self._energy_postprocessing_pipeline

        postprocessing_data = {}
        postprocessing_data["energy_readout"] = energy_readout
        postprocessing_data["atomic_numbers"] = inputs["atomic_numbers"]
        postprocessing_data["atomic_subsystem_indices"] = inputs[
            "atomic_subsystem_indices"
        ]
        return pipeline(postprocessing_data)

    def _prepare_inputs(self, inputs: Dict[str, torch.Tensor]):
        atomic_numbers = inputs["atomic_numbers"]  # shape (nr_of_atoms_in_batch, 1)
        positions = inputs["positions"]  # shape (nr_of_atoms_in_batch, 3)
        atomic_subsystem_indices = inputs["atomic_subsystem_indices"]
        nr_of_atoms_in_batch = inputs["atomic_numbers"].shape[0]

        r = self.calculate_distances_and_Pairlist(positions, atomic_subsystem_indices)

        return {
            "pair_indices": r["pair_indices"],
            "d_ij": r["d_ij"],
            "r_ij": r["r_ij"],
            "nr_of_atoms_in_batch": nr_of_atoms_in_batch,
            "positions": positions,
            "atomic_numbers": atomic_numbers,
            "atomic_subsystem_indices": atomic_subsystem_indices,
        }

    def _set_dtype(self):
        dtypes = list({p.dtype for p in self.parameters()})
        assert len(dtypes) == 1, f"Multiple dtypes: {dtypes}"

        if not self._log_message_dtype:
            log.debug(f"Setting dtype to {dtypes[0]}.")
            self._log_message_dtype = True

        if self._dtype is not None and self._dtype != dtypes[0]:
            log.warning(f"Setting dtype to {dtypes[0]}.")
            log.warning(f"This is new, be carful. You are resetting the dtype!")

        self._dtype = dtypes[0]

    def _input_checks(self, inputs: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """
        Perform input checks to validate the input dictionary.

        Parameters
        ----------
        inputs : Dict[str, torch.Tensor], with distance units attached
            Inputs containing necessary data for the forward pass.
            The exact keys and shapes are implementation-dependent.

        Raises
        ------
        ValueError
            If the input dictionary is missing required keys or has invalid shapes.

        """
        from openff.units import unit

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

        if isinstance(inputs["positions"], unit.Quantity):
            inputs["positions"] = inputs["positions"].to(unit.nanometer).m

        else:
            if not self._log_message_units:
                log.warning(
                    "Could not convert positions to nanometer. Assuming positions are already in nanometer."
                )
                self._log_message_units = True

        return inputs

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
        # adjust the dtype of the input tensors to match the model parameters
        self._set_dtype()
        # perform input checks
        inputs = self._input_checks(inputs)
        # prepare the input for the forward pass
        inputs = self.prepare_inputs(inputs)
        # perform the forward pass implemented in the subclass
        outputs = self._forward(inputs)
        # aggreate energies
        energy_readout = self._readout(outputs)
        # postprocess energies
        processed_energies = self._energy_postprocessing(energy_readout, inputs)
        # return energies
        return processed_energies


class SingleTopologyAlchemicalBaseNNPModel(BaseNNP):
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
        self._input_checks(inputs)
        raise NotImplementedError
