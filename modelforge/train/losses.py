# losses.py

"""
This module contains classes and functions for loss computation and error metrics
for training neural network potentials.
"""

from abc import ABC, abstractmethod
from typing import Dict, List
import torch
from torch import nn
from loguru import logger as log

from modelforge.dataset.dataset import NNPInput

__all__ = [
    "Error",
    "FromPerAtomToPerMoleculeSquaredError",
    "PerMoleculeSquaredError",
    "TotalChargeError",
    "DipoleMomentError",
    "Loss",
    "LossFactory",
    "create_error_metrics",
]


class Error(nn.Module, ABC):
    """
    Abstract base class for error calculation between predicted and true values.
    """

    def __init__(self, scale_by_number_of_atoms: bool = True):
        super().__init__()
        self.scale_by_number_of_atoms = (
            self._scale_by_number_of_atoms
            if scale_by_number_of_atoms
            else lambda error, atomic_subsystem_counts, prefactor=1: error
        )

    @abstractmethod
    def calculate_error(
        self,
        predicted: torch.Tensor,
        true: torch.Tensor,
    ) -> torch.Tensor:
        """
        Calculates the error between the predicted and true values.
        """
        raise NotImplementedError

    @staticmethod
    def calculate_squared_error(
        predicted_tensor: torch.Tensor, reference_tensor: torch.Tensor
    ) -> torch.Tensor:
        """
        Calculates the squared error between the predicted and true values.
        """
        squared_diff = (predicted_tensor - reference_tensor).pow(2)
        error = squared_diff.sum(dim=1, keepdim=True)
        return error

    @staticmethod
    def _scale_by_number_of_atoms(
        error, atomic_counts, prefactor: int = 1
    ) -> torch.Tensor:
        """
        Scales the error by the number of atoms in the atomic subsystems.

        Parameters
        ----------
        error : torch.Tensor
            The error to be scaled.
        atomic_counts : torch.Tensor
            The number of atoms in the atomic subsystems.
        prefactor : int
            Prefactor to adjust for the shape of the property (e.g., vector properties).

        Returns
        -------
        torch.Tensor
            The scaled error.
        """
        scaled_by_number_of_atoms = error / (prefactor * atomic_counts.unsqueeze(1))
        return scaled_by_number_of_atoms


class FromPerAtomToPerMoleculeSquaredError(Error):
    """
    Calculates the per-atom error and aggregates it to per-molecule mean squared error.
    """

    def calculate_error(
        self,
        per_atom_prediction: torch.Tensor,
        per_atom_reference: torch.Tensor,
    ) -> torch.Tensor:
        """Computes the per-atom squared error."""
        return self.calculate_squared_error(per_atom_prediction, per_atom_reference)

    def forward(
        self,
        per_atom_prediction: torch.Tensor,
        per_atom_reference: torch.Tensor,
        batch: NNPInput,
    ) -> torch.Tensor:
        """
        Computes the per-atom error and aggregates it to per-molecule mean squared error.

        Parameters
        ----------
        per_atom_prediction : torch.Tensor
            The predicted values.
        per_atom_reference : torch.Tensor
            The reference values provided by the dataset.
        batch : NNPInput
            The batch data containing metadata and input information.

        Returns
        -------
        torch.Tensor
            The aggregated per-molecule error.
        """

        # Compute per-atom squared error
        per_atom_squared_error = self.calculate_error(
            per_atom_prediction, per_atom_reference
        )

        # Initialize per-molecule squared error tensor
        per_molecule_squared_error = torch.zeros_like(
            batch.metadata.E, dtype=per_atom_squared_error.dtype
        )

        # Aggregate error per molecule
        per_molecule_squared_error = per_molecule_squared_error.scatter_add(
            0,
            batch.nnp_input.atomic_subsystem_indices.long().unsqueeze(1),
            per_atom_squared_error,
        )

        # Scale error by number of atoms
        per_molecule_square_error_scaled = self.scale_by_number_of_atoms(
            per_molecule_squared_error,
            batch.metadata.atomic_subsystem_counts,
            prefactor=per_atom_prediction.shape[-1],
        )

        return per_molecule_square_error_scaled.contiguous()


class PerMoleculeSquaredError(Error):
    """
    Calculates the per-molecule mean squared error.
    """

    def calculate_error(
        self,
        per_molecule_prediction: torch.Tensor,
        per_molecule_reference: torch.Tensor,
    ) -> torch.Tensor:
        """Computes the per-molecule squared error."""
        return self.calculate_squared_error(
            per_molecule_prediction, per_molecule_reference
        )

    def forward(
        self,
        per_molecule_prediction: torch.Tensor,
        per_molecule_reference: torch.Tensor,
        batch: NNPInput,
    ) -> torch.Tensor:
        """
        Computes the per-molecule mean squared error.

        Parameters
        ----------
        per_molecule_prediction : torch.Tensor
            The predicted values.
        per_molecule_reference : torch.Tensor
            The true values.
        batch : NNPInput
            The batch data containing metadata and input information.

        Returns
        -------
        torch.Tensor
            The mean per-molecule error.
        """

        # Compute per-molecule squared error
        per_molecule_squared_error = self.calculate_error(
            per_molecule_prediction, per_molecule_reference
        )
        # Scale error by number of atoms
        per_molecule_square_error_scaled = self.scale_by_number_of_atoms(
            per_molecule_squared_error,
            batch.metadata.atomic_subsystem_counts,
        )

        return per_molecule_square_error_scaled


class TotalChargeError(Error):
    """
    Calculates the error for total charge.
    """

    def calculate_error(
        self,
        total_charge_predict: torch.Tensor,
        total_charge_true: torch.Tensor,
    ) -> torch.Tensor:
        """
        Computes the absolute difference between predicted and true total charges.
        """
        error = torch.abs(total_charge_predict - total_charge_true)
        return error  # Shape: [batch_size, 1]

    def forward(
        self,
        total_charge_predict: torch.Tensor,
        total_charge_true: torch.Tensor,
        batch: NNPInput,
    ) -> torch.Tensor:
        """
        Computes the error for total charge.

        Parameters
        ----------
        total_charge_predict : torch.Tensor
            The predicted total charges.
        total_charge_true : torch.Tensor
            The true total charges.
        batch : NNPInput
            The batch data.

        Returns
        -------
        torch.Tensor
            The error for total charges.
        """
        error = self.calculate_error(total_charge_predict, total_charge_true)
        return error  # No scaling needed


class DipoleMomentError(Error):
    """
    Calculates the error for dipole moment.
    """

    def calculate_error(
        self,
        dipole_predict: torch.Tensor,
        dipole_true: torch.Tensor,
    ) -> torch.Tensor:
        """
        Computes the squared difference between predicted and true dipole moments.
        """
        error = (
            (dipole_predict - dipole_true).pow(2).sum(dim=1, keepdim=True)
        )  # Shape: [batch_size, 1]
        return error

    def forward(
        self,
        dipole_predict: torch.Tensor,
        dipole_true: torch.Tensor,
        batch: NNPInput,
    ) -> torch.Tensor:
        """
        Computes the error for dipole moment.

        Parameters
        ----------
        dipole_predict : torch.Tensor
            The predicted dipole moments.
        dipole_true : torch.Tensor
            The true dipole moments.
        batch : NNPInput
            The batch data.

        Returns
        -------
        torch.Tensor
            The error for dipole moments.
        """
        error = self.calculate_error(dipole_predict, dipole_true)
        return error  # No scaling needed


class Loss(nn.Module):

    _SUPPORTED_PROPERTIES = [
        "per_atom_energy",
        "per_molecule_energy",
        "per_atom_force",
        "total_charge",
        "dipole_moment",
    ]

    def __init__(self, loss_property: List[str], weights: Dict[str, float]):
        """
        Calculates the combined loss for energy and force predictions.

        Parameters
        ----------
        loss_property : List[str]
            List of properties to include in the loss calculation.
        weights : Dict[str, float]
            Dictionary containing the weights for each property in the loss calculation.

        Raises
        ------
        NotImplementedError
            If an unsupported loss type is specified.
        """
        super().__init__()
        from torch.nn import ModuleDict

        self.loss_property = loss_property
        self.weights = weights
        self.loss_functions = ModuleDict()

        for prop in loss_property:
            if prop not in self._SUPPORTED_PROPERTIES:
                raise NotImplementedError(f"Loss type {prop} not implemented.")
            log.info(f"Using loss function for {prop}")
            if prop == "per_atom_force":
                self.loss_functions[prop] = FromPerAtomToPerMoleculeSquaredError(
                    scale_by_number_of_atoms=True
                )
            elif prop == "per_atom_energy":
                self.loss_functions[prop] = PerMoleculeSquaredError(
                    scale_by_number_of_atoms=True
                )
            elif prop == "per_molecule_energy":
                self.loss_functions[prop] = PerMoleculeSquaredError(
                    scale_by_number_of_atoms=False
                )
            elif prop == "total_charge":
                self.loss_functions[prop] = TotalChargeError()
            elif prop == "dipole_moment":
                self.loss_functions[prop] = DipoleMomentError()
            else:
                raise NotImplementedError(f"Loss type {prop} not implemented.")

            self.register_buffer(prop, torch.tensor(self.weights[prop]))

    def forward(
        self,
        predict_target: Dict[str, torch.Tensor],
        batch: NNPInput,
    ) -> Dict[str, torch.Tensor]:
        """
        Calculates the combined loss for the specified properties.

        Parameters
        ----------
        predict_target : Dict[str, torch.Tensor]
            Dictionary containing predicted and true values for energy and forces.
        batch : NNPInput
            The batch data containing metadata and input information.

        Returns
        -------
        Dict[str, torch.Tensor]
            Individual per-sample loss terms and the combined total loss.
        """
        # Save the loss as a dictionary
        loss_dict = {}
        # Accumulate loss
        total_loss = torch.zeros_like(batch.metadata.E)

        # Iterate over loss properties
        for prop in self.loss_property:
            loss_fn = self.loss_functions[prop]
            if prop == "per_atom_energy":
                prop_ = "per_molecule_energy"
            else:
                prop_ = prop
            prop_loss = loss_fn(
                predict_target[f"{prop_}_predict"],
                predict_target[f"{prop_}_true"],
                batch,
            )
            # Accumulate weighted per-sample losses
            weighted_loss = self.weights[prop] * prop_loss
            total_loss += weighted_loss  # Note: total_loss is still per-sample
            loss_dict[prop] = prop_loss  # Store per-sample loss

        # Add total loss to results dict and return
        loss_dict["total_loss"] = total_loss

        return loss_dict


class LossFactory:
    """
    Factory class to create different types of loss functions.
    """

    @staticmethod
    def create_loss(loss_property: List[str], weight: Dict[str, float]) -> Loss:
        """
        Creates an instance of the specified loss type.

        Parameters
        ----------
        loss_property : List[str]
            List of properties to include in the loss calculation.
        weight : Dict[str, float]
            Dictionary containing the weights for each property in the loss calculation.

        Returns
        -------
        Loss
            An instance of the specified loss function.
        """
        return Loss(loss_property, weight)


from torch.nn import ModuleDict


def create_error_metrics(
    loss_properties: List[str],
    is_loss: bool = False,
) -> ModuleDict:
    """
    Creates a ModuleDict of MetricCollections for the given loss properties.

    Parameters
    ----------
    loss_properties : List[str]
        List of loss properties for which to create the metrics.
    is_loss : bool, optional
        If True, only the loss metric is created, by default False.

    Returns
    -------
    ModuleDict
        A dictionary where keys are loss properties and values are MetricCollections.
    """
    from torchmetrics import MetricCollection
    from torchmetrics.aggregation import MeanMetric
    from torchmetrics.regression import MeanAbsoluteError, MeanSquaredError

    if is_loss:
        metric_dict = ModuleDict(
            {prop: MetricCollection([MeanMetric()]) for prop in loss_properties}
        )
        metric_dict["total_loss"] = MetricCollection([MeanMetric()])
    else:
        metric_dict = ModuleDict(
            {
                prop: MetricCollection(
                    [MeanAbsoluteError(), MeanSquaredError(squared=False)]
                )
                for prop in loss_properties
            }
        )
    return metric_dict
