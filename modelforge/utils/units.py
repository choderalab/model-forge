"""
Module that handles unit system definitions/conversion.
"""

from typing import Union

from openff.units import unit

# define new context for converting energy (e.g., hartree)
# to energy/mol (e.g., kJ/mol)
__all__ = ["chem_context"]
chem_context = unit.Context("chem")
chem_context.add_transformation(
    "[force] * [length]",
    "[force] * [length]/[substance]",
    lambda unit, x: x * unit.avogadro_constant,
)
chem_context.add_transformation(
    "[force] * [length]/[substance]",
    "[force] * [length]",
    lambda unit, x: x / unit.avogadro_constant,
)
chem_context.add_transformation(
    "[force] * [length]/[length]",
    "[force] * [length]/[substance]/[length]",
    lambda unit, x: x * unit.avogadro_constant,
)
chem_context.add_transformation(
    "[force] * [length]/[substance]/[length]",
    "[force] * [length]/[length]",
    lambda unit, x: x / unit.avogadro_constant,
)

chem_context.add_transformation(
    "[force] * [length]/[length]/[length]",
    "[force] * [length]/[substance]/[length]/[length]",
    lambda unit, x: x * unit.avogadro_constant,
)
chem_context.add_transformation(
    "[force] * [length]/[substance]/[length]/[length]",
    "[force] * [length]/[length]/[length]",
    lambda unit, x: x / unit.avogadro_constant,
)


def _convert_str_to_unit(val: Union[unit.Quantity, str]) -> unit.Quantity:
    """Convert a string representation of an OpenFF unit to a unit.Quantity

    If the input is already a unit.Quantity, it is returned as is.
    Parameters
    ----------
    val : Union[unit.Quantity, str]
        The value to convert to a unit.Quantity

    Returns
    -------
    unit.Quantity
        The value and unit as a unit.Quantity

    Examples
    --------
    >>> _convert_str_to_unit("1.0 * kilocalorie / mole")
    Quantity(value=1.0, unit=kilocalorie/mole)
    >>> _convert_str_to_unit(unit.Quantity(1.0, unit.kilocalorie / unit.mole))
    Quantity(value=1.0, unit=kilocalorie/mole)

    """
    if isinstance(val, str):
        return unit.Quantity(val)
    return val


unit.add_context(chem_context)


def print_modelforge_unit_system():
    """
    Provide details about the used unit systems.
    """
    from loguru import logger as log

    log.info("Distance are in nanometer.")
    log.info("Energies are in kJ/mol")
    log.info("Forces are in kJ/mol/nm**2")
