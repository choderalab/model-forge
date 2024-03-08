from typing import List

from modelforge.curation.curation_baseclass import *
from retry import retry
from tqdm import tqdm


class SPICEOpenFFCuration(DatasetCuration):
    """
    Fetches the SPICE 1.1.4 dataset from MOLSSI QCArchive and processes it into a curated hdf5 file.

    All QM datapoints retrieved wer generated using B3LYP-D3BJ/DZVP level of theory.
    This is the default theory used for force field development by the Open Force Field Initiative.
    This data appears as two separate records in QCArchive: ('spec_2'  and 'spec_6'),
    where 'spec_6' provides the dispersion corrections for energy and gradient.

    This includes the following collections from qcarchive:

    "SPICE Solvated Amino Acids Single Points Dataset v1.1",
    "SPICE Dipeptides Single Points Dataset v1.2",
    "SPICE DES Monomers Single Points Dataset v1.1",
    "SPICE DES370K Single Points Dataset v1.0",
    "SPICE PubChem Set 1 Single Points Dataset v1.2",
    "SPICE PubChem Set 2 Single Points Dataset v1.2",
    "SPICE PubChem Set 3 Single Points Dataset v1.2",
    "SPICE PubChem Set 4 Single Points Dataset v1.2",
    "SPICE PubChem Set 5 Single Points Dataset v1.2",
    "SPICE PubChem Set 6 Single Points Dataset v1.2",

    It does not include the following datasets that are part of the official 1.1.4 release of SPICE (calculated
    at the ωB97M-D3(BJ)/def2-TZVPPD level of theory), as the openff level of theory was not used for these datasets:

    "SPICE Ion Pairs Single Points Dataset v1.1",
    "SPICE DES370K Single Points Dataset Supplement v1.0",

    Reference to original SPICE publication:
    Eastman, P., Behara, P.K., Dotson, D.L. et al. SPICE,
    A Dataset of Drug-like Molecules and Peptides for Training Machine Learning Potentials.
    Sci Data 10, 11 (2023). https://doi.org/10.1038/s41597-022-01882-6


    Parameters
    ----------
    hdf5_file_name, str, required
        name of the hdf5 file generated for the SPICE dataset
    output_file_dir: str, optional, default='./'
        Path to write the output hdf5 files.
    local_cache_dir: str, optional, default='./spice_dataset'
        Location to save downloaded dataset.
    convert_units: bool, optional, default=True
        Convert from the source units (e.g., angstrom, bohr, hartree)
        to [nanometer, kJ/mol] (i.e., target units)
    release_version: str, optional, default='1.1.4'
        Version of the SPICE dataset to fetch from the MOLSSI QCArchive.
    Examples
    --------
    >>> spice_openff_data = SPICEOpenFFCuration(hdf5_file_name='spice114_openff_dataset.hdf5',
    >>>                             local_cache_dir='~/datasets/spice114_openff_dataset')
    >>> spice_openff_data.process()

    """

    def __init__(
        self,
        hdf5_file_name: str,
        output_file_dir: str,
        local_cache_dir: str,
        convert_units: bool = True,
        release_version: str = "1.1.4",
    ):
        super().__init__(
            hdf5_file_name=hdf5_file_name,
            output_file_dir=output_file_dir,
            local_cache_dir=local_cache_dir,
            convert_units=convert_units,
        )
        self.release_version = release_version

    def _init_dataset_parameters(self):
        self.qcarchive_server = "ml.qcarchive.molssi.org"

        self.molecule_names = {}

        # dictionary of properties and their input units (i.e., those from QCArchive)
        # and desired output units; unit conversion is performed if convert_units = True
        self.qm_parameters = {
            "geometry": {
                "u_in": unit.bohr,
                "u_out": unit.nanometer,
            },
            "dft_total_energy": {
                "u_in": unit.hartree,
                "u_out": unit.kilojoule_per_mole,
            },
            "dispersion_corrected_dft_total_energy": {
                "u_in": unit.hartree,
                "u_out": unit.kilojoule_per_mole,
            },
            "dft_total_gradient": {
                "u_in": unit.hartree / unit.bohr,
                "u_out": unit.kilojoule_per_mole / unit.angstrom,
            },
            "dispersion_corrected_dft_total_gradient": {
                "u_in": unit.hartree / unit.bohr,
                "u_out": unit.kilojoule_per_mole / unit.angstrom,
            },
            "mbis_charges": {
                "u_in": unit.elementary_charge,
                "u_out": unit.elementary_charge,
            },
            "scf_dipole": {
                "u_in": unit.elementary_charge * unit.bohr,
                "u_out": unit.elementary_charge * unit.nanometer,
            },
            "dispersion_correction_energy": {
                "u_in": unit.hartree,
                "u_out": unit.kilojoule_per_mole,
            },
            "dispersion_correction_gradient": {
                "u_in": unit.hartree / unit.bohr,
                "u_out": unit.kilojoule_per_mole / unit.angstrom,
            },
            "reference_energy": {
                "u_in": unit.hartree,
                "u_out": unit.kilojoule_per_mole,
            },
            "formation_energy": {
                "u_in": unit.hartree,
                "u_out": unit.kilojoule_per_mole,
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
        single_mol, e.g., reference energy
        series_atom, e.g., charges
        series_mol, e.g., dft energy, dipole moment, etc.
        These ultimately appear under the "format" attribute in the hdf5 file.

        Examples
        >>> series = {'name': 'single_rec', 'atomic_numbers': 'single_atom',
                      ... 'n_configs': 'single_rec', 'geometry': 'series_atom', 'energy': 'series_mol'}
        """

        self._record_entries_series = {
            "name": "single_rec",
            "dataset_name": "single_rec",
            "source": "single_rec",
            "atomic_numbers": "single_atom",
            "n_configs": "single_rec",
            "reference_energy": "single_rec",
            "molecular_formula": "single_rec",
            "canonical_isomeric_explicit_hydrogen_mapped_smiles": "single_rec",
            "geometry": "series_atom",
            "dft_total_energy": "series_mol",
            "dft_total_gradient": "series_atom",
            "formation_energy": "series_mol",
            "mbis_charges": "series_atom",
            "scf_dipole": "series_atom",
        }

    # we will use the retry package to allow us to resume download if we lose connection to the server
    @retry(delay=1, jitter=1, backoff=2, tries=50, logger=logger, max_delay=10)
    def _fetch_singlepoint_from_qcarchive(
        self,
        dataset_name: str,
        specification_name: str,
        local_database_name: str,
        local_path_dir: str,
        force_download: bool,
        unit_testing_max_records: Optional[int] = None,
        pbar: Optional[tqdm] = None,
    ):
        """
        Fetches a singlepoint dataset from the MOLSSI QCArchive and stores it in a local sqlite database.

        Parameters
        ----------
        dataset_name: str, required
            Name of the dataset to fetch from the QCArchive
        specification_name: str, required
            Name of the specification to fetch from the QCArchive
        local_database_name: str, required
            Name of the local sqlite database to store the dataset
        local_path_dir: str, required
            Path to the directory to store the local sqlite database
        force_download: bool, required
            If True, this will force the software to download the data again, even if present.
        unit_testing_max_records: Optional[int], optional, default=None
            If set to an integer, 'n', the routine will only process the first 'n' records, useful for unit tests.
            Note, conformers of the same molecule are saved in separate records, and thus the number of molecules
            that end up in the 'data' list after _process_downloaded is called  may be less than unit_testing_max_records.
        pbar: Optional[tqdm], optional, default=None
            Progress bar to track the download process.

        pbar

        Returns
        -------

        """
        from sqlitedict import SqliteDict
        from loguru import logger
        from qcportal import PortalClient

        dataset_type = "singlepoint"
        client = PortalClient(self.qcarchive_server)

        ds = client.get_dataset(dataset_type=dataset_type, dataset_name=dataset_name)

        entry_names = ds.entry_names
        if unit_testing_max_records is None:
            unit_testing_max_records = len(entry_names)
        with SqliteDict(
            f"{local_path_dir}/{local_database_name}",
            tablename=specification_name,
            autocommit=True,
        ) as spice_db:
            # defining the db_keys as a set is faster for
            # searching to see if a key exists
            db_keys = set(spice_db.keys())
            to_fetch = []
            if force_download:
                for name in entry_names[0:unit_testing_max_records]:
                    to_fetch.append(name)
            else:
                for name in entry_names[0:unit_testing_max_records]:
                    if name not in db_keys:
                        to_fetch.append(name)
            if pbar is not None:
                pbar.total = pbar.total + len(to_fetch)
                pbar.refresh()

            # We need a different routine to fetch entries vs records with a give specification
            if len(to_fetch) > 0:
                if specification_name == "entry":
                    logger.debug(
                        f"Fetching {len(to_fetch)} entries from dataset {dataset_name}."
                    )
                    for entry in ds.iterate_entries(
                        to_fetch, force_refetch=force_download
                    ):
                        spice_db[entry.dict()["name"]] = entry
                        if pbar is not None:
                            pbar.update(1)

                else:
                    logger.debug(
                        f"Fetching {len(to_fetch)} records for {specification_name} from dataset {dataset_name}."
                    )
                    for record in ds.iterate_records(
                        to_fetch,
                        specification_names=[specification_name],
                        force_refetch=force_download,
                    ):
                        spice_db[record[0]] = record[2]
                        if pbar is not None:
                            pbar.update(1)

    def _calculate_reference_energy(self, smiles: str) -> float:
        """
        Calculate the reference energy for a given molecule, as defined by the SMILES string.

        This routine is taken from
        https://github.com/openmm/spice-dataset/blob/df7f5a2c8bf1ce0db225715a81f32897cc3a8988/downloader/downloader-openff-default.py
        Reference energies for individual atoms are computed with Psi4 1.5 wB97M-D3BJ/def2-TZVPPD.

        Parameters
        ----------
        smiles: str, required
            SMILES string describing the molecule of interest.

        Returns
        -------
        Returns the reference energy of for the atoms in the molecule (in hartrees)
        """

        from rdkit import Chem
        import numpy as np

        # Reference energies, in hartrees, computed with Psi4 1.5 wB97M-D3BJ/def2-TZVPPD.

        atom_energy = {
            "Br": {-1: -2574.2451510945853, 0: -2574.1167240829964},
            "C": {-1: -37.91424135791358, 0: -37.87264507233593, 1: -37.45349214963933},
            "Ca": {2: -676.9528465198214},
            "Cl": {-1: -460.3350243496703, 0: -460.1988762285739},
            "F": {-1: -99.91298732343974, 0: -99.78611622985483},
            "H": {-1: -0.5027370838721259, 0: -0.4987605100487531, 1: 0.0},
            "I": {-1: -297.8813829975981, 0: -297.76228914445625},
            "K": {1: -599.8025677513111},
            "Li": {1: -7.285254714046546},
            "Mg": {2: -199.2688420040449},
            "N": {
                -1: -54.602291095426494,
                0: -54.62327513368922,
                1: -54.08594142587869,
            },
            "Na": {1: -162.11366478783253},
            "O": {-1: -75.17101657391741, 0: -75.11317840410095, 1: -74.60241514396725},
            "P": {0: -341.3059197024934, 1: -340.9258392474849},
            "S": {-1: -398.2405387031612, 0: -398.1599636677874, 1: -397.7746615977658},
        }
        default_charge = {}
        for symbol in atom_energy:
            energies = [
                (energy, charge) for charge, energy in atom_energy[symbol].items()
            ]
            default_charge[symbol] = sorted(energies)[0][1]

        rdmol = Chem.MolFromSmiles(smiles, sanitize=False)
        total_charge = sum(atom.GetFormalCharge() for atom in rdmol.GetAtoms())
        symbol = [atom.GetSymbol() for atom in rdmol.GetAtoms()]
        charge = [default_charge[s] for s in symbol]
        delta = np.sign(total_charge - sum(charge))
        while delta != 0:
            best_index = -1
            best_energy = None
            for i in range(len(symbol)):
                s = symbol[i]
                e = atom_energy[s]
                new_charge = charge[i] + delta

                if new_charge in e:
                    if best_index == -1 or e[new_charge] - e[charge[i]] < best_energy:
                        best_index = i
                        best_energy = e[new_charge] - e[charge[i]]

            charge[best_index] += delta
            delta = np.sign(total_charge - sum(charge))

        return sum(atom_energy[s][c] for s, c in zip(symbol, charge))

    def _process_downloaded(
        self,
        local_path_dir: str,
        filenames: List[str],
        dataset_names: List[str],
    ):
        """
        Processes a downloaded dataset: extracts relevant information.

        Parameters
        ----------
        local_path_dir: str, required
            Path to the directory that contains the raw hdf5 datafile
        filenames: List[str], required
            Names of the raw sqlite files to process,
        dataset_names: List[str], required
            List of names of the sqlite datasets to process.

        Examples
        --------
        >>> spice_openff_data = SPICE12OpenFFCuration(hdf5_file_name='spice_pubchem_12_openff_dataset.hdf5',
        >>>                             local_cache_dir='~/datasets/spice12_openff_dataset')
        >>> spice_openff_data._process_downloaded(local_path_dir='~/datasets/spice12_openff_dataset',
        >>>                                      filenames=['spice_pubchem_set1_v1.2.sqlite'],
        >>>                                      dataset_names=['SPICE PubChem Set 1 Single Points Dataset v1.2'])
        """
        from tqdm import tqdm
        import numpy as np
        from sqlitedict import SqliteDict
        from loguru import logger
        import qcelemental as qcel
        from numpy import newaxis

        for filename, dataset_name in zip(filenames, dataset_names):
            input_file_name = f"{local_path_dir}/{filename}"

            non_error_keys = []

            # identify the set of molecules that do not have errors
            with SqliteDict(
                input_file_name, tablename="spec_2", autocommit=False
            ) as spice_db_spec2:
                spec2_keys = list(spice_db_spec2.keys())

                with SqliteDict(
                    input_file_name, tablename="spec_6", autocommit=False
                ) as spice_db_spec6:
                    for key in spec2_keys:
                        if (
                            spice_db_spec2[key].status.value == "complete"
                            and spice_db_spec6[key].status.value == "complete"
                        ):
                            non_error_keys.append(key)

            # we need to sanitize the names of the molecule, as
            # some of the names have a dash in them.
            non_error_keys_sanitized = []
            original_name = {}
            for key in non_error_keys:
                s = "_"
                d = "-"
                temp = key.split("-")
                name = d.join([s.join(temp[0:-1]), temp[-1]])
                non_error_keys_sanitized.append(name)
                original_name[name] = key

            # sort the keys such that conformers are listed in numerical order
            # this is not strictly necessary, but will help to better retain
            # connection to the original QCArchive data
            sorted_keys = []

            # names of the  molecules are of form  {name}-{conformer_number}
            # first sort by numerical_id
            s = "_"
            pre_sort = sorted(non_error_keys_sanitized, key=lambda x: (x.split("-")[0]))
            # then sort each molecule by conformer_number
            current_val = pre_sort[0].split("-")[0]
            temp_list = []

            for val in pre_sort:
                name = val.split("-")[0]

                if name == current_val:
                    temp_list.append(val)
                else:
                    sorted_keys += sorted(
                        temp_list, key=lambda x: int(x.split("-")[-1])
                    )
                    temp_list = []
                    current_val = name
                    temp_list.append(val)

            sorted_keys += sorted(temp_list, key=lambda x: int(x.split("-")[-1]))
            # first read in molecules from entry
            with SqliteDict(
                input_file_name, tablename="entry", autocommit=False
            ) as spice_db:
                logger.debug(f"Processing {filename} entries.")
                for key in tqdm(sorted_keys):
                    val = spice_db[original_name[key]].dict()
                    name = key.split("-")[0]
                    # if we haven't processed a molecule with this name yet
                    # we will add to the molecule_names dictionary
                    if name not in self.molecule_names.keys():
                        self.molecule_names[name] = len(self.data)

                        data_temp = {}
                        data_temp["name"] = name
                        data_temp["source"] = input_file_name.replace(".sqlite", "")
                        atomic_numbers = []
                        for element in val["molecule"]["symbols"]:
                            atomic_numbers.append(
                                qcel.periodictable.to_atomic_number(element)
                            )
                        data_temp["atomic_numbers"] = np.array(atomic_numbers).reshape(
                            -1, 1
                        )
                        data_temp["molecular_formula"] = val["molecule"]["identifiers"][
                            "molecular_formula"
                        ]
                        data_temp[
                            "canonical_isomeric_explicit_hydrogen_mapped_smiles"
                        ] = val["molecule"]["extras"][
                            "canonical_isomeric_explicit_hydrogen_mapped_smiles"
                        ]
                        data_temp["n_configs"] = 1
                        data_temp["geometry"] = val["molecule"]["geometry"].reshape(
                            1, -1, 3
                        )
                        data_temp[
                            "reference_energy"
                        ] = self._calculate_reference_energy(
                            data_temp[
                                "canonical_isomeric_explicit_hydrogen_mapped_smiles"
                            ]
                        )
                        data_temp["dataset_name"] = dataset_name
                        self.data.append(data_temp)
                    else:
                        # if we have already encountered this molecule we need to append to the data
                        # since we are using numpy we will use vstack to append to the arrays
                        index = self.molecule_names[name]

                        self.data[index]["n_configs"] += 1
                        self.data[index]["geometry"] = np.vstack(
                            (
                                self.data[index]["geometry"],
                                val["molecule"]["geometry"].reshape(1, -1, 3),
                            )
                        )

            with SqliteDict(
                input_file_name, tablename="spec_2", autocommit=False
            ) as spice_db:
                logger.debug(f"Processing {filename} spec_2.")

                for key in tqdm(sorted_keys):
                    name = key.split("-")[0]
                    val = spice_db[original_name[key]].dict()

                    index = self.molecule_names[name]

                    # note, we will use the convention of names being lowercase
                    # and spaces denoted by underscore
                    quantity = "dft total energy"
                    quantity_o = "dft_total_energy"
                    if quantity_o not in self.data[index].keys():
                        self.data[index][quantity_o] = val["properties"][quantity]
                    else:
                        self.data[index][quantity_o] = np.vstack(
                            (self.data[index][quantity_o], val["properties"][quantity])
                        )

                    quantity = "dft total gradient"
                    quantity_o = "dft_total_gradient"
                    if quantity_o not in self.data[index].keys():
                        self.data[index][quantity_o] = np.array(
                            val["properties"][quantity]
                        ).reshape(1, -1, 3)
                    else:
                        self.data[index][quantity_o] = np.vstack(
                            (
                                self.data[index][quantity_o],
                                np.array(val["properties"][quantity]).reshape(1, -1, 3),
                            )
                        )

                    quantity = "mbis charges"
                    quantity_o = "mbis_charges"
                    if quantity_o not in self.data[index].keys():
                        self.data[index][quantity_o] = np.array(
                            val["properties"][quantity]
                        ).reshape(1, -1)[..., newaxis]

                    else:
                        self.data[index][quantity_o] = np.vstack(
                            (
                                self.data[index][quantity_o],
                                np.array(val["properties"][quantity]).reshape(1, -1)[
                                    ..., newaxis
                                ],
                            )
                        )

                    quantity = "scf dipole"
                    quantity_o = "scf_dipole"
                    if quantity_o not in self.data[index].keys():
                        self.data[index][quantity_o] = np.array(
                            val["properties"][quantity]
                        ).reshape(1, 3)
                    else:
                        self.data[index][quantity_o] = np.vstack(
                            (
                                self.data[index][quantity_o],
                                np.array(val["properties"][quantity]).reshape(1, 3),
                            )
                        )

            with SqliteDict(
                input_file_name, tablename="spec_6", autocommit=False
            ) as spice_db:
                logger.debug(f"Processing {filename} spec_6.")

                for key in tqdm(sorted_keys):
                    name = key.split("-")[0]
                    val = spice_db[original_name[key]].dict()
                    index = self.molecule_names[name]

                    # typecasting issue in there

                    quantity = "dispersion correction energy"
                    quantity_o = "dispersion_correction_energy"
                    # Note need to typecast here because of a bug in the
                    # qcarchive entry: see issue: https://github.com/MolSSI/QCFractal/issues/766
                    if quantity_o not in self.data[index].keys():
                        self.data[index][quantity_o] = np.array(
                            float(val["properties"][quantity])
                        ).reshape(1, 1)
                    else:
                        self.data[index][quantity_o] = np.vstack(
                            (
                                self.data[index][quantity_o],
                                np.array(float(val["properties"][quantity])).reshape(
                                    1, 1
                                ),
                            ),
                        )
                    quantity = "dispersion correction gradient"
                    quantity_o = "dispersion_correction_gradient"
                    if quantity_o not in self.data[index].keys():
                        self.data[index][quantity_o] = np.array(
                            val["properties"][quantity]
                        ).reshape(1, -1, 3)
                    else:
                        self.data[index][quantity_o] = np.vstack(
                            (
                                self.data[index][quantity_o],
                                np.array(val["properties"][quantity]).reshape(1, -1, 3),
                            )
                        )
        # assign units
        for datapoint in self.data:
            for key in datapoint.keys():
                if key in self.qm_parameters:
                    datapoint[key] = datapoint[key] * self.qm_parameters[key]["u_in"]
            # add in the formation energy defined as:
            # dft_total_energy + dispersion_correction_energy - reference_energy

            # the dispersion corrected energy and gradient can be calculated from the raw data
            datapoint["dft_total_energy"] = (
                datapoint["dft_total_energy"]
                + datapoint["dispersion_correction_energy"]
            )
            # we only want to write the dispersion corrected energy to the file to avoid confusion
            datapoint.pop("dispersion_correction_energy")

            datapoint["dft_total_gradient"] = (
                datapoint["dft_total_gradient"]
                + datapoint["dispersion_correction_gradient"]
            )
            # we only want to write the dispersion corrected gradient to the file to avoid confusion
            datapoint.pop("dispersion_correction_gradient")

            datapoint["formation_energy"] = (
                datapoint["dft_total_energy"]
                - np.array(datapoint["reference_energy"].m * datapoint["n_configs"])
                * datapoint["reference_energy"].u
            )

        if self.convert_units:
            self._convert_units()

    def process(
        self,
        force_download: bool = False,
        unit_testing_max_records: Optional[int] = None,
        n_threads=6,
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
            Note, that in SPICE, conformers are stored as separate records, and are combined within this routine.
            As such the number of molecules in 'data' may be less than unit_testing_max_records, if the records fetched
            are all conformers of the same molecule.
        n_threads, int, default=6
            Number of concurrent threads for retrieving data from QCArchive
        Examples
        --------
        >>> spice_openff_data = SPICEOpenFFCuration(hdf5_file_name='spice114_openff_dataset.hdf5',
        >>>                             local_cache_dir='~/datasets/spice114_openff_dataset')
        >>> spice_openff_data.process()

        """
        from concurrent.futures import ThreadPoolExecutor, as_completed

        if self.release_version == "1.1.4":
            # The SPICE dataset is available in the MOLSSI QCArchive
            # This will need to load from various datasets, as described on the spice-dataset github page
            # see https://github.com/openmm/spice-dataset/blob/1.1.4/downloader/config.yaml

            dataset_names = [
                "SPICE Solvated Amino Acids Single Points Dataset v1.1",
                "SPICE Dipeptides Single Points Dataset v1.2",
                "SPICE DES Monomers Single Points Dataset v1.1",
                "SPICE DES370K Single Points Dataset v1.0",
                # "SPICE DES370K Single Points Dataset Supplement v1.0", # this does not have spec 2 or spec 6
                "SPICE PubChem Set 1 Single Points Dataset v1.2",
                "SPICE PubChem Set 2 Single Points Dataset v1.2",
                "SPICE PubChem Set 3 Single Points Dataset v1.2",
                "SPICE PubChem Set 4 Single Points Dataset v1.2",
                "SPICE PubChem Set 5 Single Points Dataset v1.2",
                "SPICE PubChem Set 6 Single Points Dataset v1.2",
                # "SPICE Ion Pairs Single Points Dataset v1.1", #this does not have spec 6 data for dispersion corrections
            ]

        specification_names = ["spec_2", "spec_6", "entry"]

        # if we specify the number of records, restrict to only the first subset
        # so we don't do this 6 times.
        if unit_testing_max_records is not None:
            dataset_names = ["SPICE PubChem Set 1 Single Points Dataset v1.2"]
        threads = []
        local_database_names = []

        with tqdm() as pbar:
            pbar.total = 0
            with ThreadPoolExecutor(max_workers=n_threads) as e:
                for i, dataset_name in enumerate(dataset_names):
                    local_database_name = f"{dataset_name}.sqlite"
                    local_database_names.append(local_database_name)
                    for specification_name in specification_names:
                        threads.append(
                            e.submit(
                                self._fetch_singlepoint_from_qcarchive,
                                dataset_name=dataset_name,
                                specification_name=specification_name,
                                local_database_name=local_database_name,
                                local_path_dir=self.local_cache_dir,
                                force_download=force_download,
                                unit_testing_max_records=unit_testing_max_records,
                                pbar=pbar,
                            )
                        )
        logger.debug(f"Data fetched.")
        self._clear_data()
        self.molecule_names.clear()
        logger.debug(f"Processing downloaded dataset.")

        self._process_downloaded(
            self.local_cache_dir,
            local_database_names,
            dataset_names,
        )

        self._generate_hdf5()
