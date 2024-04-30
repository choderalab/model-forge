import torch
from modelforge.dataset.dataset import DatasetStatistics
from loguru import logger as log


class FromAtomToMoleculeReduction(torch.nn.Module):

    def __init__(self):
        """
        Initializes the per-atom property readout module.
        Performs the reduction of 'per_atom' property to 'per_molecule' property.
        """
        super().__init__()

    def forward(
        self, x: torch.Tensor, atomic_subsystem_indices: torch.Tensor
    ) -> torch.Tensor:
        """

        Parameters
        ----------
        x: torch.Tensor, shape [nr_of_atoms, 1]. The per-atom property that will be reduced to per-molecule property.
        atomic_subsystem_indices: torch.Tensor, shape [nr_of_atoms]. The atomic subsystem indices

        Returns
        -------
        Tensor, shape [nr_of_moleculs, 1], the per-molecule property.
        """

        # Perform scatter add operation for atoms belonging to the same molecule
        indices = atomic_subsystem_indices.to(torch.int64)
        property_per_molecule_zeros = torch.zeros(
            len(atomic_subsystem_indices.unique()), dtype=x.dtype, device=x.device
        )

        property_per_molecule = property_per_molecule_zeros.scatter_add(0, indices, x)

        # Sum across feature dimension to get final tensor of shape (num_molecules, 1)
        # property_per_molecule = result.sum(dim=1, keepdim=True)
        return property_per_molecule


from dataclasses import dataclass, field
from typing import Dict, Iterator


@dataclass
class AtomicSelfEnergies:
    """
    AtomicSelfEnergies stores a mapping of atomic elements to their self energies.

    Provides lookup by atomic number or symbol, iteration over the mapping,
    and utilities to convert between atomic number and symbol.

    Intended as a base class to be extended with specific element-energy values.
    """

    # We provide a dictionary with {str:float} of element name to atomic self-energy,
    # which can then be accessed by atomic index or element name
    energies: Dict[str, float] = field(default_factory=dict)
    # Example mapping, replace or extend as necessary
    atomic_number_to_element: Dict[int, str] = field(
        default_factory=lambda: {
            1: "H",
            2: "He",
            3: "Li",
            4: "Be",
            5: "B",
            6: "C",
            7: "N",
            8: "O",
            9: "F",
            10: "Ne",
            11: "Na",
            12: "Mg",
            13: "Al",
            14: "Si",
            15: "P",
            16: "S",
            17: "Cl",
            18: "Ar",
            19: "K",
            20: "Ca",
            21: "Sc",
            22: "Ti",
            23: "V",
            24: "Cr",
            25: "Mn",
            26: "Fe",
            27: "Co",
            28: "Ni",
            29: "Cu",
            30: "Zn",
            31: "Ga",
            32: "Ge",
            33: "As",
            34: "Se",
            35: "Br",
            36: "Kr",
            37: "Rb",
            38: "Sr",
            39: "Y",
            40: "Zr",
            41: "Nb",
            42: "Mo",
            43: "Tc",
            44: "Ru",
            45: "Rh",
            46: "Pd",
            47: "Ag",
            48: "Cd",
            49: "In",
            50: "Sn",
            # Add more elements as needed
        }
    )
    _ase_tensor_for_indexing = None

    def __getitem__(self, key):
        if isinstance(key, int):
            # Convert atomic number to element symbol
            element = self.atomic_number_to_element.get(key)
            if element is None:
                raise KeyError(f"Atomic number {key} not found.")
            return self.energies.get(element)
        elif isinstance(key, str):
            # Directly access by element symbol
            if key not in self.energies:
                raise KeyError(f"Element {key} not found.")
            return self.energies[key]
        else:
            raise TypeError(
                "Key must be an integer (atomic number) or string (element name)."
            )

    def __iter__(self) -> Iterator[Dict[str, float]]:
        """Iterate over the energies dictionary."""
        for element, energy in self.energies.items():
            atomic_number = self.element_to_atomic_number(element)
            yield (atomic_number, energy)

    def __len__(self) -> int:
        """Return the number of element-energy pairs."""
        return len(self.energies)

    def element_to_atomic_number(self, element: str) -> int:
        """Return the atomic number for a given element symbol."""
        for atomic_number, elem_symbol in self.atomic_number_to_element.items():
            if elem_symbol == element:
                return atomic_number
        raise ValueError(f"Element symbol '{element}' not found in the mapping.")

    @property
    def atomic_number_to_energy(self) -> Dict[int, float]:
        """Return a dictionary mapping atomic numbers to their energies."""
        return {
            atomic_number: self[atomic_number]
            for atomic_number in self.atomic_number_to_element.keys()
            if self[atomic_number] is not None
        }

    @property
    def ase_tensor_for_indexing(self) -> torch.Tensor:
        if self._ase_tensor_for_indexing is None:
            max_z = max(self.atomic_number_to_element.keys()) + 1
            ase_tensor_for_indexing = torch.zeros(max_z)
            for idx in self.atomic_number_to_element:
                if self[idx]:
                    ase_tensor_for_indexing[idx] = self[idx]
                else:
                    ase_tensor_for_indexing[idx] = 0.0
            self._ase_tensor_for_indexing = ase_tensor_for_indexing

        return self._ase_tensor_for_indexing


from modelforge.potential.utils import NeuralNetworkData


class EnergyScaling:
    """
    Provides a `EnergyScaling` class that handles the pre/postprocessing of energy calculations for neural network potentials.

    The `EnergyScaling` class is responsible for:
    - Calculating the molecular self energy from the atomic self energies.
    - Rescaling the energies using dataset statistics.
    - Combining the rescaled energies and molecular self energies to produce the final energy values.

    The class also provides methods to access and update the dataset statistics used for the postprocessing.
    """

    def __init__(self) -> None:
        """
        Initializes the `DatasetStatistics` object with default values for the scaling factors and atomic self energies.
        """

        from modelforge.dataset.dataset import DatasetStatistics

        self._dataset_statistics = DatasetStatistics(0.0, 1.0, AtomicSelfEnergies())

    def _calculate_molecular_self_energy(
        self, data: NeuralNetworkData, number_of_molecules: int
    ) -> torch.Tensor:
        """
        Calculates the molecular self energy.

        Parameters
        ----------
        data : NeuralNetworkData
            The input data for the model, including atomic numbers and subsystem indices.
        number_of_molecules : int
            The number of molecules in the batch.

        Returns
        -------
        torch.Tensor
            The tensor containing the molecular self energy for each molecule.
        """

        atomic_numbers = data.atomic_numbers
        atomic_subsystem_indices = data.atomic_subsystem_indices.to(
            dtype=torch.long, device=atomic_numbers.device
        )

        # atomic_number_to_energy
        atomic_self_energies = self.dataset_statistics.atomic_self_energies
        ase_tensor_for_indexing = atomic_self_energies.ase_tensor_for_indexing.to(
            device=atomic_numbers.device
        )

        # first, we need to use the atomic numbers to generate a tensor that
        # contains the atomic self energy for each atomic number
        ase_tensor = ase_tensor_for_indexing[atomic_numbers]

        # then, we use the atomic_subsystem_indices to scatter add the atomic self
        # energies in the ase_tensor to generate the molecular self energies
        ase_tensor_zeros = torch.zeros((number_of_molecules,)).to(
            device=atomic_numbers.device
        )
        ase_tensor = ase_tensor_zeros.scatter_add(
            0, atomic_subsystem_indices, ase_tensor
        )

        return ase_tensor

    def _rescale_energy(self, energies: torch.Tensor) -> torch.Tensor:
        """
        Rescales energies using the dataset statistics.

        Parameters
        ----------
        energies : torch.Tensor
            The tensor of energies to be rescaled.

        Returns
        -------
        torch.Tensor
            The rescaled energies.
        """

        return (
            energies * self.dataset_statistics.scaling_stddev
            + self.dataset_statistics.scaling_mean
        )

    def _energy_postprocessing(
        self, properties_per_molecule: torch.Tensor, inputs: NeuralNetworkData
    ) -> Dict[str, torch.Tensor]:
        """
        Postprocesses the energies by rescaling and adding molecular self energy.

        Parameters
        ----------
        properties_per_molecule : The properties computed per molecule.
        inputs : The original input data to the model.

        Returns
        -------
        Dict[str, torch.Tensor]
            The dictionary containing the postprocessed energy tensors.
        """

        # first, resale the energies
        processed_energy = {}
        processed_energy["raw_E"] = properties_per_molecule.clone().detach()
        properties_per_molecule = self._rescale_energy(properties_per_molecule)
        processed_energy["rescaled_E"] = properties_per_molecule.clone().detach()
        # then, calculate the molecular self energy
        molecular_ase = self._calculate_molecular_self_energy(
            inputs, properties_per_molecule.numel()
        )
        processed_energy["molecular_ase"] = molecular_ase.clone().detach()
        # add the molecular self energy to the rescaled energies
        processed_energy["E"] = properties_per_molecule + molecular_ase
        return processed_energy

    @property
    def dataset_statistics(self):
        """
        Property for accessing the model's dataset statistics.

        Returns
        -------
        DatasetStatistics
            The dataset statistics associated with the model.
        """

        return self._dataset_statistics

    @dataset_statistics.setter
    def dataset_statistics(self, value: "DatasetStatistics"):
        """
        Sets the dataset statistics for the model.

        Parameters
        ----------
        value : DatasetStatistics
            The new dataset statistics to be set for the model.
        """

        if not isinstance(value, DatasetStatistics):
            raise ValueError("Value must be an instance of DatasetStatistics.")

        self._dataset_statistics = value

    def update_dataset_statistics(self, **kwargs):
        """
        Updates specific fields of the model's dataset statistics.

        Parameters
        ----------
        **kwargs
            Fields and their new values to update in the dataset statistics.
        """

        for key, value in kwargs.items():
            if hasattr(self.dataset_statistics, key):
                setattr(self.dataset_statistics, key, value)
            else:
                log.warning(f"{key} is not a valid field of DatasetStatistics.")
