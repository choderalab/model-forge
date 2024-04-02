from modelforge.curation.curation_baseclass import DatasetCuration
from modelforge.utils.units import *
from typing import Optional
from loguru import logger


class SPICE114Curation(DatasetCuration):
    """
    Routines to fetch  the spice 1.1.4 dataset from zenodo and process into a curated hdf5 file.

    Small-molecule/Protein Interaction Chemical Energies (SPICE).
    The SPICE dataset contains 1.1 million conformations for a diverse set of small molecules,
    dimers, dipeptides, and solvated amino acids. It includes 15 elements, charged and
    uncharged molecules, and a wide range of covalent and non-covalent interactions.
    It provides both forces and energies calculated at the ωB97M-D3(BJ)/def2-TZVPPD level of theory,
    using Psi4 1.4.1 along with other useful quantities such as multipole moments and bond orders.

    Reference:
    Eastman, P., Behara, P.K., Dotson, D.L. et al. SPICE,
    A Dataset of Drug-like Molecules and Peptides for Training Machine Learning Potentials.
    Sci Data 10, 11 (2023). https://doi.org/10.1038/s41597-022-01882-6

    Dataset DOI:
    https://doi.org/10.5281/zenodo.8222043

    Parameters
    ----------
    hdf5_file_name, str, required
        name of the hdf5 file generated for the SPICE dataset
    output_file_dir: str, optional, default='./'
        Path to write the output hdf5 files.
    local_cache_dir: str, optional, default='./spice_dataset'
        Location to save downloaded dataset.
    convert_units: bool, optional, default=True
        Convert from [e.g., angstrom, bohr, hartree] (i.e., source units)
        to [nanometer, kJ/mol] (i.e., target units)

    Examples
    --------
    >>> spice114_data = SPICE114Curation(hdf5_file_name='spice114_dataset.hdf5',
    >>>                             local_cache_dir='~/datasets/spice114_dataset')
    >>> spice114_data.process()

    """

    def _init_dataset_parameters(self):
        self.dataset_download_url = (
            "https://zenodo.org/records/8222043/files/SPICE-1.1.4.hdf5"
        )
        self.dataset_md5_checksum = "f27d4c81da0e37d6547276bf6b4ae6a1"
        # the spice dataset includes openff compatible unit definitions in the hdf5 file
        # these values were used to generate this dictionary
        self.qm_parameters = {
            "geometry": {
                "u_in": unit.bohr,
                "u_out": unit.nanometer,
            },
            "formation_energy": {
                "u_in": unit.hartree,
                "u_out": unit.kilojoule_per_mole,
            },
            "dft_total_energy": {
                "u_in": unit.hartree,
                "u_out": unit.kilojoule_per_mole,
            },
            "dft_total_gradient": {
                "u_in": unit.hartree / unit.bohr,
                "u_out": unit.kilojoule_per_mole / unit.angstrom,
            },
            "mbis_charges": {
                "u_in": unit.elementary_charge,
                "u_out": unit.elementary_charge,
            },
            "total_charge": {
                "u_in": unit.elementary_charge,
                "u_out": unit.elementary_charge,
            },
            "mbis_dipoles": {
                "u_in": unit.elementary_charge * unit.bohr,
                "u_out": unit.elementary_charge * unit.nanometer,
            },
            "mbis_quadrupoles": {
                "u_in": unit.elementary_charge * unit.bohr**2,
                "u_out": unit.elementary_charge * unit.nanometer**2,
            },
            "mbis_octupoles": {
                "u_in": unit.elementary_charge * unit.bohr**3,
                "u_out": unit.elementary_charge * unit.nanometer**3,
            },
            "scf_dipole": {
                "u_in": unit.elementary_charge * unit.bohr,
                "u_out": unit.elementary_charge * unit.nanometer,
            },
            "scf_quadrupole": {
                "u_in": unit.elementary_charge * unit.bohr**2,
                "u_out": unit.elementary_charge * unit.nanometer**2,
            },
            "mayer_indices": {
                "u_in": None,
                "u_out": None,
            },
            "wiberg_lowdin_indices": {
                "u_in": None,
                "u_out": None,
            },
        }

    def _init_record_entries_series(self):
        """
        Init the dictionary that defines the format of the data.

        For data efficiency, information for different conformers will be grouped together
        To make it clear to the dataset loader which pieces of information are common to all
        conformers or which quantities are series (i.e., have different values for each conformer).
        These labels will also allow us to define whether a given entry is per-atom, per-molecule,
        or is a scalar/string that applies to the entire record.
        Options include:
        single_rec, e.g., name, n_configs, smiles
        single_atom, e.g., atomic_numbers (these are the same for all conformers)
        series_atom, e.g., charges
        series_mol, e.g., dft energy, dipole moment, etc.
        These ultimately appear under the "format" attribute in the hdf5 file.

        Examples
        >>> series = {'name': 'single_rec', 'atomic_numbers': 'single_atom',
                      ... 'n_configs': 'single_rec', 'geometry': 'series_atom', 'energy': 'series_mol'}
        """

        self._record_entries_series = {
            "name": "single_rec",
            "atomic_numbers": "single_atom",
            "n_configs": "single_rec",
            "smiles": "single_rec",
            "subset": "single_rec",
            "total_charge": "single_rec",
            "geometry": "series_atom",
            "dft_total_energy": "series_mol",
            "dft_total_gradient": "series_atom",
            "formation_energy": "series_mol",
            "mayer_indices": "series_atom",
            "mbis_charges": "series_atom",
            "mbis_dipoles": "series_atom",
            "mbis_octupoles": "series_atom",
            "mbis_quadrupoles": "series_atom",
            "scf_dipole": "series_mol",
            "scf_quadrupole": "series_mol",
            "wiberg_lowdin_indices": "series_atom",
        }

    def _calculate_reference_charge(self, smiles: str) -> unit.Quantity:
        """
        Calculate the total charge of a molecule from its SMILES string.

        Parameters
        ----------
        smiles: str, required
            SMILES string of the molecule.

        Returns
        -------
        total_charge: unit.Quantity
        """

        from rdkit import Chem

        rdmol = Chem.MolFromSmiles(smiles, sanitize=False)
        total_charge = sum(atom.GetFormalCharge() for atom in rdmol.GetAtoms())
        return int(total_charge) * unit.elementary_charge

    def _process_downloaded(
        self,
        local_path_dir: str,
        name: str,
        unit_testing_max_records: Optional[int] = None,
    ):
        """
        Processes a downloaded dataset: extracts relevant information.

        Parameters
        ----------
        local_path_dir: str, required
            Path to the directory that contains the raw hdf5 datafile
        name: str, required
            Name of the raw hdf5 file,
        unit_testing_max_records: int, optional, default=None
            If set to an integer ('n') the routine will only process the first 'n' records; useful for unit tests.

        Examples
        --------
        """
        import h5py
        from tqdm import tqdm

        input_file_name = f"{local_path_dir}/{name}"

        need_to_reshape = {"formation_energy": True, "dft_total_energy": True}
        with h5py.File(input_file_name, "r") as hf:
            names = list(hf.keys())
            if unit_testing_max_records is None:
                n_max = len(names)
            else:
                n_max = unit_testing_max_records

            for i, name in tqdm(enumerate(names[0:n_max]), total=n_max):
                # Extract the total number of conformations for a given molecule
                n_configs = hf[name]["conformations"].shape[0]

                keys_list = list(hf[name].keys())

                # temp dictionary for ANI-1x and ANI-1ccx data
                ds_temp = {}

                ds_temp["name"] = f"{name}"
                ds_temp["smiles"] = hf[name]["smiles"][()][0].decode("utf-8")
                ds_temp["atomic_numbers"] = hf[name]["atomic_numbers"][()].reshape(
                    -1, 1
                )
                ds_temp["n_configs"] = n_configs

                # param_in is the name of the entry, param_data contains input (u_in) and output (u_out) units
                for param_in, param_data in self.qm_parameters.items():
                    # for consistency between datasets, we will all the particle positions "geometry"
                    param_out = param_in
                    if param_in == "geometry":
                        param_in = "conformations"

                    if param_in in keys_list:
                        temp = hf[name][param_in][()]
                        if param_in in need_to_reshape:
                            temp = temp.reshape(-1, 1)

                        param_unit = param_data["u_in"]
                        if param_unit is not None:
                            # check that units in the hdf5 file match those we have defined in self.qm_parameters
                            try:
                                assert (
                                    hf[name][param_in].attrs["units"]
                                    == param_data["u_in"]
                                )
                            except:
                                msg1 = f'unit mismatch: units in hdf5 file: {hf[name][param_in].attrs["units"]},'
                                msg2 = f'units defined in curation class: {param_data["u_in"]}.'

                                raise AssertionError(f"{msg1} {msg2}")

                            ds_temp[param_out] = temp * param_unit
                        else:
                            ds_temp[param_out] = temp
                ds_temp["total_charge"] = self._calculate_reference_charge(
                    ds_temp["smiles"]
                )
                self.data.append(ds_temp)
        if self.convert_units:
            self._convert_units()

        # From documentation: By default, objects inside group are iterated in alphanumeric order.
        # However, if group is created with track_order=True, the insertion order for the group is remembered (tracked)
        # in HDF5 file, and group contents are iterated in that order.
        # As such, we shouldn't need to do sort the objects to ensure reproducibility.
        # self.data = sorted(self.data, key=lambda x: x["name"])

    def process(
        self,
        force_download: bool = False,
        unit_testing_max_records: Optional[int] = None,
    ) -> None:
        """
        Downloads the dataset, extracts relevant information, and writes an hdf5 file.

        Parameters
        ----------
        force_download: bool, optional, default=False
            If the raw data_file is present in the local_cache_dir, the local copy will be used.
            If True, this will force the software to download the data again, even if present.
        unit_testing_max_records: int, optional, default=None
            If set to an integer, 'n', the routine will only process the first 'n' records, useful for unit tests.

        Examples
        --------
        >>> spice114_data = SPICE114Curation(hdf5_file_name='spice114_dataset.hdf5',
        >>>                             local_cache_dir='~/datasets/spice114_dataset')
        >>> spice114_data.process()

        """
        from modelforge.utils.remote import download_from_zenodo

        url = self.dataset_download_url

        # download the dataset
        self.name = download_from_zenodo(
            url=url,
            md5_checksum=self.dataset_md5_checksum,
            output_path=self.local_cache_dir,
            force_download=force_download,
        )

        self._clear_data()

        # process the rest of the dataset
        if self.name is None:
            raise Exception("Failed to retrieve name of file from zenodo.")
        self._process_downloaded(
            self.local_cache_dir, self.name, unit_testing_max_records
        )

        self._generate_hdf5()
