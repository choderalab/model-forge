import os
import pytest

import platform

ON_MACOS = platform.system() == "Darwin"

IN_GITHUB_ACTIONS = os.getenv("GITHUB_ACTIONS") == "true"
from modelforge.potential import _Implemented_NNPs
from modelforge.dataset import _ImplementedDatasets
from modelforge.potential import NeuralNetworkPotentialFactory


@pytest.mark.skipif(ON_MACOS, reason="Skipping this test on MacOS GitHub Actions")
@pytest.mark.parametrize("model_name", _Implemented_NNPs.get_all_neural_network_names())
@pytest.mark.parametrize("dataset_name", ["ANI2x"])
@pytest.mark.parametrize("include_force", [False, True])
def test_train_with_lightning(model_name, dataset_name, include_force):
    """
    Test the forward pass for a given model and dataset.
    """

    from modelforge.train.training import return_toml_config, perform_training

    config = return_toml_config(
        f"modelforge/tests/data/training_defaults/{model_name.lower()}_{dataset_name.lower()}.toml"
    )

    # Extract parameters
    potential_parameters = config["potential"].get("potential_parameters", {})
    training_parameters = config["training"].get("training_parameters", {})

    training_parameters['include_force'] = include_force

    trainer = perform_training(
        model_name=model_name,
        nr_of_epochs=2,
        dataset_name=dataset_name,
        potential_parameters=potential_parameters,
        training_parameters=training_parameters,
        save_dir="test_training",
        experiment_name="test_train_with_lightning",
        num_nodes=1,
    )
    # save checkpoint
    trainer.save_checkpoint("test.chp")

    model = NeuralNetworkPotentialFactory.create_nnp(
        use="training",
        model_type=model_name,
        model_parameters=potential_parameters,
        training_parameters=training_parameters,
    )

    model = model.load_from_checkpoint("test.chp")
    assert type(model) is not None


@pytest.mark.parametrize("model_name", ["SchNet"])
@pytest.mark.parametrize("dataset_name", _ImplementedDatasets.get_all_dataset_names())
def test_loss(model_name, dataset_name, datamodule_factory):
    from loguru import logger as log

    dm = datamodule_factory(dataset_name=dataset_name)
    model = NeuralNetworkPotentialFactory.create_nnp("inference", model_name)

    from modelforge.train.training import EnergyAndForceLoss
    import torch

    loss = EnergyAndForceLoss(model)

    r = loss.compute_loss(next(iter(dm.train_dataloader())))

    if dataset_name != "QM9":
        loss = EnergyAndForceLoss(model, include_force=True)
        r = loss.compute_loss(next(iter(dm.train_dataloader())))


@pytest.mark.skipif(ON_MACOS, reason="Skipping this test on MacOS GitHub Actions")
@pytest.mark.parametrize("model_name", _Implemented_NNPs.get_all_neural_network_names())
@pytest.mark.parametrize("dataset_name", ["QM9"])
def test_hypterparameter_tuning_with_ray(model_name, dataset_name, datamodule_factory):

    dm = datamodule_factory(dataset_name=dataset_name)
    model = NeuralNetworkPotentialFactory.create_nnp("training", model_name)

    model.tune_with_ray(
        train_dataloader=dm.train_dataloader(),
        val_dataloader=dm.val_dataloader(),
        number_of_ray_workers=1,
        number_of_epochs=1,
        number_of_samples=1,
    )
