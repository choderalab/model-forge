import torch
from typing import Dict
from openff.units import unit


def load_atomic_self_energies(path: str) -> Dict[str, unit.Quantity]:
    import toml

    energy_statistic = toml.load(open(path, "r"))

    # attach kJ/mol units
    atomic_self_energies = {
        key: unit.Quantity(value)
        for key, value in energy_statistic["atomic_self_energies"].items()
    }

    return atomic_self_energies


def load_atomic_energies_stats(path: str) -> Dict[str, unit.Quantity]:
    import toml

    energy_statistic = toml.load(open(path, "r"))
    # convert values to tensor
    atomic_energies_stats = {
        key: unit.Quantity(value)
        for key, value in energy_statistic["atomic_energies_stats"].items()
    }

    return atomic_energies_stats


class FromAtomToMoleculeReduction(torch.nn.Module):

    def __init__(
        self,
        reduction_mode: str = "sum",
    ):
        """
        Initializes the per-atom property readout_operation module.
        Performs the reduction of 'per_atom' property to 'per_molecule' property.
        """
        super().__init__()
        self.reduction_mode = reduction_mode

    def forward(
        self, per_atom_property: torch.Tensor, index: torch.Tensor
    ) -> torch.Tensor:
        """

        Parameters
        ----------
        per_atom_property: torch.Tensor, shape [nr_of_atoms, 1]. The per-atom property that will be reduced to per-molecule property.
        atomic_subsystem_indices: torch.Tensor, shape [nr_of_atoms]. The atomic subsystem indices

        Returns
        -------
        Tensor, shape [nr_of_moleculs, 1], the per-molecule property.
        """
        indices = index.to(torch.int64)
        # Perform scatter add operation for atoms belonging to the same molecule
        property_per_molecule_zeros = torch.zeros(
            len(indices.unique()),
            dtype=per_atom_property.dtype,
            device=per_atom_property.device,
        )

        property_per_molecule = property_per_molecule_zeros.scatter_reduce(
            0, indices, per_atom_property, reduce=self.reduction_mode
        )
        return property_per_molecule


from dataclasses import dataclass, field
from typing import Dict, Iterator

from openff.units import unit
from modelforge.dataset.utils import _ATOMIC_NUMBER_TO_ELEMENT


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
    energies: Dict[str, unit.Quantity] = field(default_factory=dict)
    # Example mapping, replace or extend as necessary
    atomic_number_to_element: Dict[int, str] = field(
        default_factory=lambda: _ATOMIC_NUMBER_TO_ELEMENT
    )
    _ase_tensor_for_indexing = None

    def __getitem__(self, key):
        from modelforge.utils.units import chem_context

        if isinstance(key, int):
            # Convert atomic number to element symbol
            element = self.atomic_number_to_element.get(key)
            if element is None:
                raise KeyError(f"Atomic number {key} not found.")
            if self.energies.get(element) is None:
                return None
            return self.energies.get(element).to(unit.kilojoule_per_mole, "chem").m
        elif isinstance(key, str):
            # Directly access by element symbol
            if key not in self.energies:
                raise KeyError(f"Element {key} not found.")
            if self.energies[key] is None:
                return None

            return self.energies[key].to(unit.kilojoule_per_mole, "chem").m
        else:
            raise TypeError(
                "Key must be an integer (atomic number) or string (element name)."
            )

    def __iter__(self) -> Iterator[Dict[str, float]]:
        """Iterate over the energies dictionary."""
        from modelforge.utils.units import chem_context

        for element, energy in self.energies.items():
            atomic_number = self.element_to_atomic_number(element)
            yield (atomic_number, energy.to(unit.kilojoule_per_mole, "chem").m)

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


class ScaleValues(torch.nn.Module):

    def __init__(self, mean: float, stddev: float) -> None:

        super().__init__()
        self.register_buffer("mean", torch.tensor([mean]))
        self.register_buffer("stddev", torch.tensor([stddev]))

    def forward(self, values_to_be_scaled: torch.Tensor) -> torch.Tensor:
        """
        Rescales values using the provided mean and stddev.

        Parameters
        ----------
        values_to_be_scaled : torch.Tensor
            The tensor of energies to be rescaled.

        Returns
        -------
        torch.Tensor
            The rescaled values.
        """

        return values_to_be_scaled * self.stddev + self.mean


class CalculateAtomicSelfEnergy(torch.nn.Module):

    def __init__(self, atomic_self_energies) -> None:
        super().__init__()

        # if values in atomic_self_energies are strings convert them to kJ/mol
        if isinstance(list(atomic_self_energies.values())[0], str):
            atomic_self_energies = {
                key: unit.Quantity(value)
                for key, value in atomic_self_energies.items()
            }
        self.atomic_self_energies = AtomicSelfEnergies(atomic_self_energies)

    def forward(
        self,
        atomic_numbers: torch.Tensor,
        atomic_subsystem_indices: torch.Tensor,
    ) -> torch.Tensor:
        """
        Calculates the molecular self energy.

        Parameters
        ----------
        atomic_numbers : torch.Tensor
            The input data for the model, including atomic numbers and subsystem indices.
        Returns
        -------
        torch.Tensor
            The tensor containing the molecular self energy for each molecule.
        """

        atomic_subsystem_indices = atomic_subsystem_indices.to(
            dtype=torch.long, device=atomic_numbers.device
        )

        # atomic_number_to_energy
        ase_tensor_for_indexing = self.atomic_self_energies.ase_tensor_for_indexing.to(
            device=atomic_numbers.device
        )

        # first, we need to use the atomic numbers to generate a tensor that
        # contains the atomic self energy for each atomic number
        ase_tensor = ase_tensor_for_indexing[atomic_numbers]

        return ase_tensor
